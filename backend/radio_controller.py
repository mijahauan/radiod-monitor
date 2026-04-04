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
from typing import Iterable, List, Optional

from ka9q import RadiodControl, generate_multicast_ip
from ka9q.types import Encoding

from .sources.base import Station

logger = logging.getLogger(__name__)


class RadioController:
    def __init__(self, radiod_host: str = "airspy-status.local"):
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

    async def connect(self):
        try:
            self.control = RadiodControl(self.radiod_host)
            logger.info(f"Connected to radiod at {self.radiod_host}")
        except Exception as e:
            logger.error(f"Failed to connect to radiod: {e}")
            raise

    def set_squelch(self, threshold_db: float):
        self.squelch_threshold = threshold_db
        logger.info(f"Squelch threshold set to {self.squelch_threshold} dB")
        if not self.control:
            return
        for ssrc in list(self.active_channels):
            try:
                self.control.set_squelch(
                    ssrc,
                    open_threshold=threshold_db,
                    close_threshold=threshold_db - 2.0,
                    snr_squelch=True,
                )
            except Exception as e:
                logger.warning(f"Failed to update squelch on SSRC {ssrc:08x}: {e}")

    def tune_center(self, center_freq_hz: float):
        """Tune the radiod receiver front end to the given center frequency."""
        if not self.control:
            return
        try:
            self.control.set_frequency(center_freq_hz)
            logger.info(f"Tuned front end to {center_freq_hz/1e6:.3f} MHz")
        except Exception as e:
            logger.warning(f"Failed to tune front end to {center_freq_hz/1e6:.3f} MHz: {e}")

    def apply_stations(self, stations: Iterable[Station], preset: str = "nfm"):
        """
        Ensure radiod channels exist for the given Station set, remove any
        channels no longer in it. Mutates self.active_channels.
        """
        if not self.control:
            logger.warning("apply_stations: no radiod connection")
            return

        self.preset = preset

        new_freqs: set = set()
        for st in stations:
            new_freqs.add(float(st.freq_hz))

        logger.info(
            f"Monitoring {len(new_freqs)} frequencies  preset={preset}  "
            f"dest={self.destination}"
        )

        # Remove channels no longer needed
        for ssrc, freq_hz in list(self.active_channels.items()):
            if freq_hz not in new_freqs:
                try:
                    self.control.remove_channel(ssrc)
                    logger.info(
                        f"Removed channel SSRC {ssrc:08x} ({freq_hz/1e6:.3f} MHz)"
                    )
                except Exception as e:
                    logger.warning(f"Failed to remove SSRC {ssrc:08x}: {e}")
                del self.active_channels[ssrc]

        # Ensure a channel exists for every requested frequency
        for freq_hz in new_freqs:
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

                try:
                    self.control.set_squelch(
                        ssrc,
                        open_threshold=self.squelch_threshold,
                        close_threshold=self.squelch_threshold - 2.0,
                        snr_squelch=True,
                    )
                except Exception as sq_err:
                    logger.warning(f"Failed to set squelch on SSRC {ssrc:08x}: {sq_err}")

                logger.info(
                    f"Channel SSRC {ssrc:08x} ready: {freq_hz/1e6:.3f} MHz → "
                    f"{channel.multicast_address}:{channel.port}"
                )
            except Exception as e:
                logger.error(f"Failed to ensure channel for {freq_hz/1e6:.3f} MHz: {e}")

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
