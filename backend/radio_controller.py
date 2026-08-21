"""
RadioController — the control plane for radiod channel lifecycle.

Source-agnostic. It takes a preset (supplied by the active Source), a
center frequency to tune the receiver front end, and a set of Station
objects to monitor. For each station it calls ensure_channel on a
stable app-scoped multicast destination, which makes the SSRC a
deterministic hash of (freq, preset, sample_rate, encoding, destination,
gain=0.0) so the SSRC survives server restarts and is independently
reachable by anyone who recomputes the hash with the same arguments (the
VFO in backend/vfo.py does not do this -- it never computes an SSRC,
it holds whatever create_channel() allocates it).

Three multicast groups, not one, because `destination` is an input to that
hash: sensors on `radiod-monitor`, the listened-to VFO on
and `radiod-monitor-vfo`. Distinct groups are what keep the VFO's SSRC
from aliasing onto a sensor's, and what make the VFO exempt
from the stale-channel sweep structurally rather than by a special case.

Per-user settings (squelch) are applied after channel creation so they
don't perturb the SSRC hash.
"""
import asyncio
import logging
import threading
from typing import Iterable, List, Optional

from ka9q import (
    RadiodControl,
    allocate_ssrc,
    discover_channels,
    generate_multicast_ip,
)
from ka9q.types import Encoding

from .sources.base import Station
from .vfo import Vfo
from .window import FrontEndWindow

logger = logging.getLogger(__name__)

# LIFETIME to pass to ensure_channel(), in frames. radiod's Blocktime here is
# 20 ms/frame, so 50 frames = 1 s. This only governs how promptly radiod
# reaps a channel *after* we zero its frequency (radio.c:1415 -- reaping
# requires both freq==0 and lifetime expiry); it does not affect a live,
# tuned channel at all -- measured: a channel created with a 2 s lifetime
# survived 30 s untouched while tuned. Without a short lifetime here, radiod
# falls back to DEFAULT_LIFETIME (20 s), which is why removed channels (the
# anchor, stale stations, on-demand listener channels) used to linger for
# 15-25 s after we asked for them to go away.
CHANNEL_LIFETIME_FRAMES = 50


class RadioController:
    def __init__(self, radiod_host: str = "airspyhf-status.local"):
        self.radiod_host = radiod_host
        self.control: Optional[RadiodControl] = None
        self.active_channels: dict = {}  # ssrc -> freq_hz
        self.squelch_threshold: float = 10.0
        # Stable app-scoped multicast destination. Single slug shared by
        # all sources so switching modes is just a delta on the channel
        # set, not a full teardown.
        self.destination: str = generate_multicast_ip("radiod-monitor")
        # Active preset — set by apply_stations() from the current Source.
        self.preset: str = "nfm"
        self.sample_rate: int = 48000
        # Number of audio channels the current preset emits (1 for all
        # narrow-band modes, 2 for wfm). Only a hint: the frontend reads it
        # via /api/sources to seed WebCodecs AudioDecoder before any audio
        # arrives, but the VFO (backend/vfo.py) overrides it with ground
        # truth read from the first Opus frame's TOC byte once tuned.
        self.audio_channels: int = 1
        # Whether SNR-based squelch is meaningful for the active preset.
        # wfm does not publish SNR so SNR-squelched channels never open.
        # Set by apply_stations() from the active Source.
        self.snr_squelch_enabled: bool = True
        # Frequencies of the stations the last search asked for. This is the
        # set the audio plane validates against -- not active_channels, which
        # fills in gradually while channels are still being created.
        self.monitored_freqs: set = set()

        # The task that converges radiod's channel set onto the latest search.
        # A search publishes its station list to the browser immediately -- the
        # UI must be able to offer a station at once -- but the channels behind
        # it are built afterwards, off the event loop. A tune that lands in
        # that gap can be tuning against the PREVIOUS mode's channels: switch
        # from NWS to FM, click a station before convergence finishes, and the
        # seven NWS sensor channels are still on the air at 162 MHz pinning the
        # shared front-end window, so the FM station cannot come up no matter
        # where the anchor tries to put the window. The audio path awaits this
        # before tuning.
        self.converge_task = None

        # Set by the search path once stale channels are gone; the audio path
        # waits on it before tuning. Starts set so a tune before any search
        # is never blocked.
        self.removals_done = None
        # Whether the last-applied station set fit inside the receiver's
        # window and therefore got live channels (vs. directory mode, where
        # no channels are created up front). See fits_window / apply_stations.
        self.activity_available: bool = True
        # Measured usable IF window of the connected receiver -- probed from
        # radiod's FE_LOW_EDGE/FE_HIGH_EDGE on connect and re-probed on every
        # host change, since it's a property of the radio, not of this app.
        self.window = FrontEndWindow()

        # on the audio group: sharing one destination is what let the VFO's
        # adopt-an-existing-channel scan pick up the anchor after a restart
        # and destroy the centring that wfm depends on. A separate group also
        # makes the anchor's exemption from the stale sweep structural rather
        # than a special case.

        # Where the VFO lives. A THIRD group, and it has to be: `destination`
        # is one of the inputs to allocate_ssrc, and the sensor diff below
        # derives its wanted set from exactly the argument list
        # create_channel() auto-allocates from. On one destination, tuning the
        # VFO to a frequency that is also a monitored station yields the
        # identical SSRC -- the VFO's channel and the sensor channel become
        # one channel, so the sweep exemption never fires, the search
        # re-applies the user's squelch over the VFO's held-open one, and the
        # activity map reports one station's SNR on another's marker. Distinct
        # groups make that collision impossible by construction rather than by
        # a check someone has to remember. It also gives the VFO's
        # adopt-an-existing-channel scan a destination that contains nothing
        # but the VFO -- no sensors, no probe channel mid-purge.
        self.vfo_destination: str = generate_multicast_ip("radiod-monitor-vfo")

        # The anchor's own group. It carries no audio and must never be
        # mistaken for the VFO by a scan of the VFO's group.
        self.anchor_destination: str = generate_multicast_ip("radiod-monitor-anchor")

        # The one channel the user listens through. Separate from the sensor
        # channels in active_channels, which exist only to report SNR for the
        # activity map and are never listened to.
        self.vfo = Vfo(control=None, window=self.window,
                       destination=self.vfo_destination,
                       anchor_destination=self.anchor_destination)

        self._apply_lock = threading.Lock()

    async def connect(self):
        try:
            self.control = RadiodControl(self.radiod_host)
            logger.info(f"Connected to radiod at {self.radiod_host}")
        except Exception as e:
            logger.error(f"Failed to connect to radiod: {e}")
            raise

        # Learn the window edges once. centre_on needs them, and only the
        # presets in PRESETS_NEEDING_CENTRE ever ask for centring -- but the
        # edges have to be known before the first such tune. Costs one
        # throwaway channel on the anchor group at startup.
        await asyncio.to_thread(
            self.window.probe, self.control, self.anchor_destination,
            self.sample_rate
        )

        self.vfo.control = self.control
        self.vfo.ssrc = None      # an SSRC belongs to one radiod, not to us
        self.vfo.preset = None
        self.vfo._channel_freq_hz = None
        self.vfo._created.clear()
        self.vfo._per_station.clear()

        # Clear anything a previous run left on any of our groups. One live
        # channel in another band holds the front end there and no anchor can
        # pull it back. Done once, here, with a long listen -- so the first
        # tune of a session does not have to pay for a discovery round trip.
        await asyncio.to_thread(self._sweep_our_groups)

    # Threshold used to hold a squelch open that radiod will not let us
    # switch off. Low enough that any demodulated signal clears it.
    SQUELCH_WIDE_OPEN_DB = -20.0

    # Fraction of the receiver's usable window a monitored station set may
    # span. radiod parks channels near the window edge by design, and the wfm
    # demodulator cannot demodulate there (see CLAUDE.md), so leaving margin
    # is not cosmetic.
    WINDOW_FILL = 0.8


    def _squelch_args(self) -> dict:
        """
        Build set_squelch kwargs honoring the active source's snr_squelch flag.

        When a source asks for no SNR squelch, we do *not* send enable=False:
        radiod's wfm demodulator sets `chan->squelch.snr_enable = true`
        unconditionally when its thread starts (wfm.c), so the disable is
        silently reverted and the channel stays shut. Measured: wfm with
        enable=False emits zero RTP; the same channel with the squelch left
        enabled at a -20 dB threshold emits continuously (301 packets in 6 s).

        So the way to keep such a channel open is a threshold below anything
        the demod will report, not a flag radiod overrides.
        """
        if self.snr_squelch_enabled:
            return {
                "enable": True,
                "open_snr_db": self.squelch_threshold,
                "close_snr_db": self.squelch_threshold - 2.0,
            }
        return {
            "enable": True,
            "open_snr_db": self.SQUELCH_WIDE_OPEN_DB,
            "close_snr_db": self.SQUELCH_WIDE_OPEN_DB - 5.0,
        }

    def set_squelch(self, threshold_db: float):
        self.squelch_threshold = threshold_db
        logger.info(f"Squelch threshold set to {self.squelch_threshold} dB")
        if not self.control:
            return
        # There is exactly one channel to apply it to: the VFO. It is also
        # re-applied on every tune, so this only matters while one is playing.
        ssrc = self.vfo.ssrc
        if ssrc is None:
            return
        try:
            self.control.set_squelch(ssrc, **self._squelch_args())
        except Exception as e:
            logger.warning(f"Failed to update squelch on SSRC {ssrc:08x}: {e}")

    def apply_stations(
        self,
        stations: Iterable[Station],
        preset: str = "nfm",
        audio_channels: int = 1,
        snr_squelch: bool = True,
        sample_rate: int = 48000,
        on_removals_done=None,
    ):
        """Record what the user is looking at. Create nothing.

        This used to build one radiod channel per station so the map could
        show live SNR. On a receiver with a 660 kHz window that was never
        going to work for a band 20 MHz wide, and measurement showed it was
        actively harmful even when it did fit: while any channel is live in
        another part of the spectrum, the front end cannot be placed for the
        station the listener actually picked. Measured, one channel each --

            nfm 162.400 alone                  LO 162.4000  snr 8.51  251 frames
            fresh wfm 91.300, nfm still alive  LO  91.5192  snr None    0 frames

        -- where 91.5192 is 219.2 kHz off target, radiod having edge-parked the
        wfm channel instead of centring it. Removing the other channel
        afterwards does not repair it, because nothing re-runs placement.

        So the VFO is the only channel this app creates. The station list is a
        directory; clicking a station is what puts a channel on the air. What
        is lost is the activity map's live SNR, which the hardware could not
        honestly support in the first place.
        """
        self.preset = preset
        self.audio_channels = audio_channels
        self.snr_squelch_enabled = snr_squelch
        self.sample_rate = sample_rate
        self.monitored_freqs = {float(st.freq_hz) for st in stations}
        self.activity_available = False

        if not self.control:
            logger.warning("apply_stations: no radiod connection")
            if on_removals_done is not None:
                on_removals_done()
            return

        # Nothing to converge: no channels are created here, so a search is
        # pure bookkeeping and costs no radiod round trips at all. Leftovers
        # from older versions are swept once at connect().
        if on_removals_done is not None:
            try:
                on_removals_done()
            except Exception as e:
                logger.debug(f"on_removals_done: {e}")

        logger.info(
            f"{len(self.monitored_freqs)} stations listed  preset={preset}  "
            f"(directory only -- the VFO is the only channel)"
        )

    def _sweep_our_groups(self, listen: float = 5.0):
        """Remove anything left on ANY of our groups by a previous run.

        Run once at connect, with a generous listen. `discover_channels` is a
        fixed-duration listen for status multicast and can simply not hear a
        channel inside a short window -- a 1.0 s sweep logged nothing while
        live nfm channels at 162.4-162.55 sat there reading 8-9 dB, holding
        the front end in the wrong band. Nothing creates these any more, so
        paying five seconds once at startup is the right trade.
        """
        if not self.control:
            return
        dest_ips = tuple(d.split(":")[0] for d in
                         (self.destination, self.vfo_destination,
                          self.anchor_destination))
        try:
            found = discover_channels(self.radiod_host, listen)
        except Exception as e:
            logger.debug(f"startup sweep: discover failed: {e}")
            return
        stale = [s for s, ch in found.items()
                 if any(d in (ch.multicast_address or "") for d in dest_ips)]
        for ssrc in stale:
            try:
                self.control.remove_channel(ssrc)
                logger.info(f"Removed leftover channel SSRC {ssrc:08x}")
            except Exception as e:
                logger.debug(f"startup sweep: remove {ssrc:08x}: {e}")
        self.active_channels.clear()


    async def release_idle(self):
        """Give the radio back when nobody is using the app.

        Every channel shares one front-end window, so a VFO left parked on the
        last station a listener chose keeps dragging the window to it -- the
        app competing with itself for the receiver, and with any other client
        of this radiod. Sensor channels are just as pointless with no control
        socket connected to receive the activity they measure.

        This is not `close()`: the controller stays connected, and since the
        VFO is the only channel this app creates there is nothing to rebuild.

        The VFO's channel is deliberately NOT removed, and that is the whole
        subtlety. An earlier version of this method did remove it and forget
        its SSRC, reasoning that the 30 s idle grace was longer than radiod's
        ~20 s purge. That is backwards: the purge starts when the channel is
        REMOVED, so the grace period runs before it, not during it. A listener
        who came back and clicked a station within 20 s of the release got a
        freshly created channel with the same deterministic SSRC -- one radiod
        was still reaping -- and it produced no RTP. Observed on 162.400 MHz,
        a station that otherwise plays every time.

        Keeping the channel costs one idle demodulator and the window sitting
        where the last listener left it. Removing it costs a station that
        silently fails to play, which is the bug this whole redesign exists to
        eliminate.
        """
        if not self.control:
            return
        try:
            await self.vfo.stop()
        except Exception as e:
            logger.debug(f"release_idle: vfo stop: {e}")

        self.monitored_freqs = set()
        try:
            pass
        except Exception as e:
            logger.debug(f"release_idle: window release: {e}")
        logger.info(
            "Idle: dropped the anchor; the VFO channel stays (removing it "
            "would start a purge the next tune could land inside)"
        )

    async def close(self):
        """Release every channel this app owns on radiod.

        Sweeps the whole destination group rather than just active_channels.
        Those two sets drift: a channel outlives the dict when the process is
        killed between a create and the next search, when a host switch
        rebuilds the controller, or when apply_stations() fired a removal that
        radiod had not yet acted on. radiod keeps whatever it is not told to
        drop, so anything missed here shows up as an orphan in `control`
        forever -- which is exactly how they accumulated.
        """
        if not self.control:
            return

        try:
            await self.vfo.stop()
        except Exception as e:
            logger.debug(f"close: vfo stop: {e}")

        dest_ips = tuple(
            d.split(":")[0]
            for d in (self.destination, self.vfo_destination,
                      self.anchor_destination)
        )
        ssrcs = set(self.active_channels)
        try:
            for ssrc, ch in discover_channels(self.radiod_host, 1.0).items():
                if any(d in (ch.multicast_address or "") for d in dest_ips):
                    ssrcs.add(ssrc)
        except Exception as e:
            logger.warning(f"close: discover_channels failed, "
                           f"removing only tracked channels: {e}")
        for ssrc in ssrcs:
            try:
                self.control.remove_channel(ssrc)
            except Exception:
                pass
        if ssrcs:
            logger.info(f"Released {len(ssrcs)} channel(s) on {dest_ips}")
        self.active_channels.clear()
        self.control.close()
        self.control = None
        # The VFO holds its own reference to the same RadiodControl. Leaving it
        # pointing at a closed socket is worse than clearing it: connect() is
        # the only place that repairs it, so a host switch whose connect()
        # fails would leave every later tune issuing commands on a dead socket
        # and raising "Not connected to radiod" forever. Cleared, Vfo._tune_once
        # fails fast and the listener is told why.
        self.vfo.control = None
