"""
RadioController — the control plane for radiod channel lifecycle.

Source-agnostic. It takes a preset (supplied by the active Source), a
center frequency to tune the receiver front end, and a set of Station
objects to monitor. For each station it calls ensure_channel on a
stable app-scoped multicast destination, which makes the SSRC a
deterministic hash of (freq, preset, sample_rate, encoding, destination,
gain=0.0) so the SSRC survives server restarts and is independently
reachable from AudioStreamer with the same arguments.

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
from .window import FrontEndWindow, DEFAULT_USABLE_BW_HZ

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
        # narrow-band modes, 2 for wfm). AudioStreamer reads this when
        # creating new ManagedStreams; the frontend reads it via
        # /api/sources to configure WebCodecs AudioDecoder.
        self.audio_channels: int = 1
        # Whether SNR-based squelch is meaningful for the active preset.
        # wfm does not publish SNR so SNR-squelched channels never open.
        # Set by apply_stations() from the active Source.
        self.snr_squelch_enabled: bool = True
        # Frequencies of the stations the last search asked for. This is the
        # set the audio plane validates against -- not active_channels, which
        # fills in gradually while channels are still being created.
        self.monitored_freqs: set = set()
        # Whether the last-applied station set fit inside the receiver's
        # window and therefore got live channels (vs. directory mode, where
        # no channels are created up front). See fits_window / apply_stations.
        self.activity_available: bool = True
        # Measured usable IF window of the connected receiver -- probed from
        # radiod's FE_LOW_EDGE/FE_HIGH_EDGE on connect and re-probed on every
        # host change, since it's a property of the radio, not of this app.
        # FrontEndWindow also owns placing the front end (the anchor
        # mechanism -- see backend/window.py).
        self.window = FrontEndWindow()

        # Where the anchor lives. It carries no audio, so it does not belong
        # on the audio group: sharing one destination is what let the VFO's
        # adopt-an-existing-channel scan pick up the anchor after a restart
        # and destroy the centring that wfm depends on. A separate group also
        # makes the anchor's exemption from the stale sweep structural rather
        # than a special case.
        self.anchor_destination: str = generate_multicast_ip("radiod-monitor-anchor")

        # The one channel the user listens through. Separate from the sensor
        # channels in active_channels, which exist only to report SNR for the
        # activity map and are never listened to.
        self.vfo = Vfo(control=None, window=self.window,
                       destination=self.destination,
                       anchor_destination=self.anchor_destination)

        self._apply_lock = threading.Lock()

    async def connect(self):
        try:
            self.control = RadiodControl(self.radiod_host)
            logger.info(f"Connected to radiod at {self.radiod_host}")
        except Exception as e:
            logger.error(f"Failed to connect to radiod: {e}")
            raise
        await asyncio.to_thread(
            self.window.probe, self.control, self.destination, self.sample_rate
        )

        self.vfo.control = self.control
        self.vfo.ssrc = None      # an SSRC belongs to one radiod, not to us
        self.vfo.preset = None

    # Threshold used to hold a squelch open that radiod will not let us
    # switch off. Low enough that any demodulated signal clears it.
    SQUELCH_WIDE_OPEN_DB = -20.0

    # Fraction of the receiver's usable window a monitored station set may
    # span. radiod parks channels near the window edge by design, and the wfm
    # demodulator cannot demodulate there (see CLAUDE.md), so leaving margin
    # is not cosmetic.
    WINDOW_FILL = 0.8

    def fits_window(self, freqs) -> bool:
        """True if every one of `freqs` can be monitored simultaneously.

        A monitored station needs a radiod channel inside the front end's
        window, and there is one window shared by every channel. So this is
        what decides whether the activity map can mean anything: with the
        whole set inside the window each channel reports real SNR, and
        without it the app monitors nothing and serves the station list as a
        directory instead.
        """
        values = [float(f) for f in freqs]
        if len(values) < 2:
            return True
        span = max(values) - min(values)
        window = self.window.usable_bw_hz or DEFAULT_USABLE_BW_HZ
        return span <= window * self.WINDOW_FILL

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
        kwargs = self._squelch_args()
        for ssrc in list(self.active_channels):
            try:
                self.control.set_squelch(ssrc, **kwargs)
            except Exception as e:
                logger.warning(f"Failed to update squelch on SSRC {ssrc:08x}: {e}")

    def apply_stations(
        self,
        stations: Iterable[Station],
        preset: str = "nfm",
        audio_channels: int = 1,
        snr_squelch: bool = True,
        sample_rate: int = 48000,
    ):
        """
        Converge the radiod channel set to exactly the given stations.
        Creates missing channels, removes any on our destination whose SSRC
        is not in the new set, and applies squelch to every channel in the
        new set. Also removes ghost channels from stale sessions that no
        longer match expected SSRC values — for example, channels created
        with different hash inputs or encoding. Re-entrant: updates
        channels no longer in it. Mutates self.active_channels.

        Serialized with a lock so rapid searches (squelch/radius/mode
        changes) don't race.
        """
        if not self.control:
            logger.warning("apply_stations: no radiod connection")
            return

        with self._apply_lock:
            self._apply_stations_locked(
                stations, preset, audio_channels, snr_squelch, sample_rate
            )

    def _apply_stations_locked(
        self,
        stations: Iterable[Station],
        preset: str,
        audio_channels: int,
        snr_squelch: bool,
        sample_rate: int,
    ):

        self.preset = preset
        self.audio_channels = audio_channels
        self.snr_squelch_enabled = snr_squelch
        self.sample_rate = sample_rate

        new_freqs: set = set()
        for st in stations:
            new_freqs.add(float(st.freq_hz))
        # Published immediately, before any channel exists. The UI offers a
        # station the moment results arrive, but creating a large station set
        # takes a while, so validating a Listen against created channels
        # rejects stations the user can legitimately see and click.
        self.monitored_freqs = set(new_freqs)

        # A set wider than the window cannot be monitored: channels outside it
        # report snr=-inf and produce no RTP. Rather than create channels that
        # cannot work, serve the list as a directory and let the audio plane
        # create the one channel a listener actually asks for.
        self.activity_available = self.fits_window(new_freqs)
        if not self.activity_available:
            logger.info(
                f"{len(new_freqs)} stations span more than the receiver's "
                f"window — activity unavailable; the VFO carries whatever the "
                f"listener selects"
            )
            new_freqs = set()

        logger.info(
            f"Monitoring {len(new_freqs)} frequencies  preset={preset}  "
            f"dest={self.destination}"
        )

        # Converge by *diffing*, not by wiping and rebuilding.
        #
        # The SSRC is a deterministic hash of exactly the parameters that
        # define a channel (frequency, preset, sample_rate, encoding,
        # destination, agc, gain, radiod identity), so the SSRC set we want is
        # computable before talking to radiod at all.  An existing channel
        # whose SSRC is in that set is correct *by construction* — there is
        # nothing to reconcile, so it is left completely alone.
        #
        # This is what makes the old blocking wait unnecessary.  Wiping
        # everything first meant immediately re-creating SSRCs that were still
        # being torn down, and radiod's removal is asynchronous — so the code
        # had to poll for up to 10 s for freq=0 zombies to disappear before it
        # dared re-create them, on every single search.  Here the removed set
        # and the created set are disjoint by definition: an SSRC we keep is
        # never removed, and an SSRC we remove is never re-created in the same
        # pass.  Nothing has to be waited on.
        #
        # It also stops a search from cutting off audio the user is listening
        # to: re-searching the same mode now preserves that station's channel
        # instead of destroying and rebuilding it.
        desired: dict = {}   # ssrc -> freq_hz
        for freq_hz in new_freqs:
            desired[allocate_ssrc(
                frequency_hz=freq_hz,
                preset=preset,
                sample_rate=self.sample_rate,
                agc=False,           # matches ensure_channel's agc_enable=0
                gain=0.0,
                destination=self.destination,
                encoding=Encoding.OPUS,
                radiod_host=self.control.status_address,
            )] = freq_hz

        try:
            existing = discover_channels(self.radiod_host, 1.0)
        except Exception as e:
            logger.warning(f"apply_stations: discover_channels failed: {e}")
            existing = {}

        dest_ip = self.destination.split(":")[0]
        ours = {
            ssrc: ch for ssrc, ch in existing.items()
            if dest_ip in (ch.multicast_address or "")
        }

        # Remove only what is genuinely stale — channels on our destination
        # that the new station set does not want.  Fire-and-forget: we never
        # re-create these SSRCs in this pass, so radiod can purge them on its
        # own schedule while we get on with creating the new ones.
        for ssrc, ch in ours.items():
            if ssrc in desired:
                continue
            if ssrc == self.window.anchor_ssrc:
                # The anchor holds the front end on whatever the listener
                # chose. It is deliberately not a station, so the diff would
                # otherwise delete it on every search and un-centre the window.
                continue
            try:
                self.control.remove_channel(ssrc)
                logger.info(
                    f"Removed stale channel SSRC {ssrc:08x} "
                    f"({ch.frequency/1e6:.3f} MHz)"
                )
            except Exception as e:
                logger.warning(f"Failed to remove SSRC {ssrc:08x}: {e}")

        self.active_channels.clear()

        # Keep the channels that are already right, and skip their round trips.
        # A channel counts as already right when radiod is serving it at the
        # expected frequency AND on OPUS — the frequency guards against a
        # zombie mid-teardown reusing the SSRC, and the encoding against the
        # preset default having been reasserted underneath us.  Both fields
        # come from the discovery we already did, so this costs nothing.
        reused = 0
        for ssrc, freq_hz in list(desired.items()):
            ch = ours.get(ssrc)
            if ch is None:
                continue
            if abs((ch.frequency or 0.0) - freq_hz) > 1.0:
                continue
            if ch.encoding != Encoding.OPUS:
                continue
            self.active_channels[ssrc] = freq_hz
            desired.pop(ssrc)
            reused += 1
            try:
                self.control.set_squelch(ssrc, **self._squelch_args())
            except Exception as sq_err:
                logger.warning(f"Failed to set squelch on SSRC {ssrc:08x}: {sq_err}")

        if reused:
            logger.info(
                f"Reused {reused} existing channel(s); "
                f"creating {len(desired)}"
            )

        # Create only the frequencies that are actually missing
        for freq_hz in desired.values():
            try:
                channel = self.control.ensure_channel(
                    frequency_hz=freq_hz,
                    preset=preset,
                    sample_rate=self.sample_rate,
                    gain=0.0,
                    destination=self.destination,
                    encoding=Encoding.OPUS,
                    timeout=5.0,
                    lifetime=CHANNEL_LIFETIME_FRAMES,
                )
                ssrc = channel.ssrc
                self.active_channels[ssrc] = freq_hz

                # radiod applies the preset's default output encoding on
                # create and honours OPUS only from a follow-up
                # OUTPUT_ENCODING command.  Without this the channel serves
                # s16be while the audio plane advertises Opus.
                #
                # Asserted but deliberately NOT verified here: verify_channel()
                # costs a full discover_channels() poll, and a search creates
                # one channel per station -- nine of them for NWS -- which adds
                # seconds to every search for a check that does not gate
                # anything on this path.  The audio plane re-asserts and *does*
                # verify in AudioStreamer._assert_opus, once per listener,
                # which is the only path whose bytes reach a decoder.
                try:
                    self.control.set_output_encoding(ssrc, Encoding.OPUS)
                except Exception as enc_err:
                    logger.warning(
                        f"Failed to assert OPUS on SSRC {ssrc:08x}: {enc_err}"
                    )

                try:
                    self.control.set_squelch(ssrc, **self._squelch_args())
                except Exception as sq_err:
                    logger.warning(f"Failed to set squelch on SSRC {ssrc:08x}: {sq_err}")

                logger.info(
                    f"Channel SSRC {ssrc:08x} ready: {freq_hz/1e6:.3f} MHz → "
                    f"{channel.multicast_address}:{channel.port}"
                )
            except Exception as e:
                logger.error(f"Failed to ensure channel for {freq_hz/1e6:.3f} MHz: {e}")

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
            for d in (self.destination, self.anchor_destination)
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
