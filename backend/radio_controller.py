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
        self._apply_lock = threading.Lock()

    async def connect(self):
        try:
            self.control = RadiodControl(self.radiod_host)
            logger.info(f"Connected to radiod at {self.radiod_host}")
        except Exception as e:
            logger.error(f"Failed to connect to radiod: {e}")
            raise

    def _squelch_args(self) -> dict:
        """
        Build set_squelch kwargs honoring the active source's snr_squelch flag.
        When SNR squelch is disabled (e.g. wfm, which doesn't publish SNR),
        we disable squelch entirely so the channel stays open and RTP packets
        flow continuously.
        """
        if self.snr_squelch_enabled:
            return {
                "enable": True,
                "open_snr_db": self.squelch_threshold,
                "close_snr_db": self.squelch_threshold - 2.0,
            }
        return {
            "enable": False,
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

    def tune_center(self, center_freq_hz: float):
        """Store the desired front-end center; actual LO tuning happens in
        _apply_stations_locked once we have an SSRC to route the command."""
        self._pending_center_hz = center_freq_hz

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

        # Tune the front-end LO now that we have at least one SSRC to route
        # the command through.
        center = getattr(self, '_pending_center_hz', None)
        if center and self.active_channels:
            route_ssrc = next(iter(self.active_channels))
            try:
                self.control.set_first_lo(route_ssrc, center)
                logger.info(f"Tuned front-end LO to {center/1e6:.3f} MHz")
            except Exception as e:
                logger.warning(f"Failed to tune front-end LO: {e}")
            self._pending_center_hz = None

    async def close(self):
        if self.control:
            for ssrc in list(self.active_channels.keys()):
                try:
                    self.control.remove_channel(ssrc)
                except Exception:
                    pass
            self.active_channels.clear()
            self.control.close()
            self.control = None
