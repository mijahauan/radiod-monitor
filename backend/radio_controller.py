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

logger = logging.getLogger(__name__)

# Frequency for the one throwaway channel probe_frontend() needs: radiod
# reports the front-end limits alongside per-channel status, so there has to
# be a channel to ask about. Any frequency the receiver can reach will do --
# the limits are a property of the front end, not of the channel -- and this
# one is in range of every radiod this app talks to.
PROBE_FREQ_HZ = 10_000_000.0


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
        # Frequency the front end is currently aimed at, if any listener has
        # asked for one. Sticky: re-asserted after anything that disturbs
        # radiod's front-end placement (see focus_on / _reassert_focus).
        self.focused_freq_hz: Optional[float] = None
        # Frequencies of the stations the last search asked for. This is the
        # set the audio plane validates against -- not active_channels, which
        # fills in gradually while channels are still being created.
        self.monitored_freqs: set = set()
        # Measured usable IF window of the connected receiver, in Hz --
        # probed from radiod's FE_LOW_EDGE/FE_HIGH_EDGE on connect. The
        # controller needs this to know what the radio can actually cover.
        # None until probed; re-probed on every host change because the
        # answer is a property of the radio, not of this app.
        self.usable_bw_hz: Optional[float] = None
        self.fe_low_edge_hz: Optional[float] = None
        self.fe_high_edge_hz: Optional[float] = None
        # SSRC of the anchor channel that positions the front end (see
        # focus_on). One at most, moved rather than re-created.
        self._anchor_ssrc: Optional[int] = None
        self._apply_lock = threading.Lock()

    async def connect(self):
        try:
            self.control = RadiodControl(self.radiod_host)
            logger.info(f"Connected to radiod at {self.radiod_host}")
        except Exception as e:
            logger.error(f"Failed to connect to radiod: {e}")
            raise
        self.usable_bw_hz = None
        await asyncio.to_thread(self.probe_frontend)

    def probe_frontend(self) -> Optional[float]:
        """Ask radiod how much spectrum this receiver covers at once.

        radiod reports the front end's usable IF limits as FE_LOW_EDGE and
        FE_HIGH_EDGE -- the same Frontend.min_IF/max_IF that set_freq() tests
        a channel against before deciding to retune. Their span is the real
        answer: 660 kHz on this Airspy HF+ at 768 kHz sample rate, i.e. well
        under the raw rate, because radiod has already discounted filter
        rolloff. Deriving it from samprate would overstate it.

        Costs one throwaway channel: the limits ride along with per-channel
        status, so there has to be a channel to ask about. It is created at
        the frequency the front end is already on -- so radiod has no reason
        to retune anything -- and removed immediately.
        """
        if not self.control:
            return None
        probe_ssrc = None
        try:
            probe = self.control.ensure_channel(
                frequency_hz=PROBE_FREQ_HZ,
                preset="am",
                sample_rate=self.sample_rate,
                gain=0.0,
                destination=self.destination,
                encoding=Encoding.OPUS,
                timeout=5.0,
            )
            probe_ssrc = probe.ssrc
            status = self.control.poll_status(probe_ssrc, timeout=3.0)
            fe = getattr(status, "frontend", None) or status
            low = getattr(fe, "fe_low_edge", None)
            high = getattr(fe, "fe_high_edge", None)
            if low is not None and high is not None and high > low:
                self.usable_bw_hz = float(high - low)
                # Kept separately because the window is not always centred on
                # the LO: a real-sampling front end (RX888) reports something
                # like -4700..-600 kHz. focus_on() needs the actual edges, not
                # just the width.
                self.fe_low_edge_hz = float(low)
                self.fe_high_edge_hz = float(high)
                logger.info(
                    f"{getattr(fe, 'description', self.radiod_host)}: usable "
                    f"window {self.usable_bw_hz/1e3:.1f} kHz "
                    f"({low/1e3:+.1f}..{high/1e3:+.1f} kHz), "
                    f"input rate {getattr(fe, 'input_samprate', '?')} Hz"
                )
            else:
                logger.warning(
                    "radiod did not report FE_LOW_EDGE/FE_HIGH_EDGE; band "
                    "segments will fall back to a default width"
                )
        except Exception as e:
            logger.warning(f"Could not probe the front end: {e}")
        finally:
            if probe_ssrc is not None:
                try:
                    self.control.remove_channel(probe_ssrc)
                except Exception:
                    pass
        return self.usable_bw_hz

    # Threshold used to hold a squelch open that radiod will not let us
    # switch off. Low enough that any demodulated signal clears it.
    SQUELCH_WIDE_OPEN_DB = -20.0

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

    def focus_on(self, freq_hz: float) -> bool:
        """Pull the front end onto `freq_hz` by re-asserting that channel.

        The front end covers one window at a time -- 768 kHz on the airspyhf
        here -- while a source's band segment can be far wider (FmSource uses
        5 MHz). Only stations inside the current window produce any RTP at
        all; the rest sit at snr=-inf. Since radiod re-places the front end
        to cover a channel whose frequency is asserted, asserting the one the
        user chose is what makes it listenable.

        Measured with four channels spread over 93.9-107.1 MHz: first_lo sat
        at 106.881 MHz until set_frequency(93.900), which moved it to
        94.119 MHz, then set_frequency(102.300) moved it to 102.081 MHz.

        The cost is inherent to the hardware, not to this call: pulling the
        window onto one station takes the others out of it, so their markers
        go inactive while you listen.

        The focus is sticky because it does not survive on its own. radiod
        re-places the front end every time a channel's frequency is set, so
        the tail of a search still creating channels drags the window away
        from whatever was focused moments earlier -- measured: focus_on
        succeeded on 102.900 MHz and the front end still ended up at 100.481
        MHz once the remaining channels of a 34-station search were created.
        _reassert_focus() therefore re-applies it after apply_stations.
        """
        self.focused_freq_hz = freq_hz
        return self._set_focus(freq_hz)

    # Filter half-width radiod reserves for the anchor's own preset ("am",
    # ±5 kHz) plus set_freq's 1 kHz fudge. Used to work out where to put the
    # anchor so the LO lands where we want it.
    _ANCHOR_MARGIN_HZ = 6_000.0

    def _set_focus(self, freq_hz: float) -> bool:
        """Centre the front-end window on `freq_hz` using an anchor channel.

        Asking radiod to place the window is not the same as asking it to
        *centre* the window. `set_freq()` retunes only far enough to bring a
        channel's filter inside, parking it `filter.max_IF + fudge` from the
        edge -- 111 kHz for wfm. That is fine for a narrowband mode, but the
        wfm demodulator does not demodulate inside that filter: `wfm.c` runs a
        384 kHz composite path needing ±192 kHz around the channel, which then
        overruns the window edge by 81 kHz. The result is a demod that half
        works -- clear voice in brief fragments, snr flipping to -inf --
        which is exactly what a listener reports.

        Setting the channel's own frequency cannot fix it, because that is the
        call that parks it at the edge. What does work is placing a *second*,
        narrow channel such that radiod's edge-parking of that one leaves the
        LO on our station: an "am" channel (±5 kHz) at
        `freq + (high_edge - 6 kHz)` drags the LO to ≈ `freq`. Every other
        channel then recalculates its own IF against the new LO
        (`radio.c`: "Retuning the front end will cause all the other channels
        to recalculate their own IFs"), so the station lands at IF ≈ 0 with the
        whole composite inside the window -- and radiod never retunes again,
        because a channel at IF 0 is nowhere near an edge.

        Measured at 102.300 MHz: IF +219.2 kHz, snr=-inf, ~17 frames per 10 s
        before; IF +0.0 kHz, snr 6.5, 500 frames per 10 s after, with a
        voice/hiss ratio of 16-30 against 13.8 for a known-good NWR station.

        The idea came from ka9q-web, which runs a spectrum channel alongside
        its audio channel and so positions the front end without ever
        commanding the LO.
        """
        if not self.control:
            return False
        if not any(abs(f - freq_hz) < 1.0 for f in self.active_channels.values()):
            logger.warning(
                f"focus_on: {freq_hz/1e6:.3f} MHz is not in the monitored set"
            )
            return False
        high_edge = self.fe_high_edge_hz
        if high_edge is None:
            # Without the front-end edges we cannot compute the anchor
            # position. Fall back to re-asserting the station itself, which at
            # least brings it inside the window for narrowband modes.
            return self._assert_frequency(freq_hz)
        anchor_hz = freq_hz + (high_edge - self._ANCHOR_MARGIN_HZ)
        try:
            anchor = self.control.ensure_channel(
                frequency_hz=anchor_hz,
                preset="am",
                sample_rate=self.sample_rate,
                gain=0.0,
                destination=self.destination,
                encoding=Encoding.OPUS,
                timeout=5.0,
            )
        except Exception as e:
            logger.warning(f"focus_on: could not place anchor channel: {e}")
            return self._assert_frequency(freq_hz)
        # Squelch it shut: the anchor exists to position the front end, not to
        # be listened to, and an open one is just wasted packets on the wire.
        try:
            self.control.set_squelch(anchor.ssrc, enable=True,
                                     open_snr_db=80.0, close_snr_db=75.0)
        except Exception:
            pass
        self._drop_anchor(keep=anchor.ssrc)
        self._anchor_ssrc = anchor.ssrc
        logger.info(
            f"Front end centred on {freq_hz/1e6:.3f} MHz via anchor at "
            f"{anchor_hz/1e6:.3f} MHz (SSRC {anchor.ssrc:08x})"
        )
        return True

    def _assert_frequency(self, freq_hz: float) -> bool:
        """Last-resort focus: re-assert the station's own frequency."""
        ssrc = next(
            (s for s, f in self.active_channels.items() if abs(f - freq_hz) < 1.0),
            None,
        )
        if ssrc is None:
            return False
        try:
            self.control.set_frequency(ssrc, freq_hz)
            return True
        except Exception as e:
            logger.warning(f"focus_on {freq_hz/1e6:.3f} MHz failed: {e}")
            return False

    def _drop_anchor(self, keep: Optional[int] = None):
        """Remove the previous anchor channel, if any."""
        ssrc = self._anchor_ssrc
        if ssrc is None or ssrc == keep:
            return
        self._anchor_ssrc = None
        try:
            self.control.remove_channel(ssrc)
        except Exception:
            pass

    def _reassert_focus(self):
        """Re-aim the front end after channel churn has moved it."""
        freq = self.focused_freq_hz
        if freq is not None and any(
            abs(f - freq) < 1.0 for f in self.active_channels.values()
        ):
            self._set_focus(freq)

    def clear_focus(self):
        """Stop holding the front end on a station (last listener left)."""
        self.focused_freq_hz = None
        self._drop_anchor()

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

        Serialized with a lock so rapid band-segment switches don't race.
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
            if ssrc == self._anchor_ssrc:
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

        # No front-end tuning to a band centre: radiod places the front end
        # from the channels it was asked for and can only cover one window
        # at a time. focus_on() aims it at the station a listener chose.
        #
        # Every channel created above just moved the front end, so if a
        # listener is holding a station, aim it back at them.
        self._reassert_focus()

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
        dest_ip = self.destination.split(":")[0]
        ssrcs = set(self.active_channels)
        try:
            for ssrc, ch in discover_channels(self.radiod_host, 1.0).items():
                if dest_ip in (ch.multicast_address or ""):
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
            logger.info(f"Released {len(ssrcs)} channel(s) on {dest_ip}")
        self.active_channels.clear()
        self.control.close()
        self.control = None
