"""The VFO: the one channel the user actually listens to.

It is created once and RETUNED, never recreated. That is the load-bearing
rule. Destroying a radiod channel starts a ~20 s asynchronous purge (radio.c
reaps a channel only once its frequency is zero, after Channel_idle_timeout);
re-creating the same SSRC inside that window hands back a channel radiod is
still tearing down, which never produces RTP. Every "switching is broken"
symptom in this project traces to that cycle.

The SSRC is never computed here. `create_channel()` allocates one and returns
it; this module stores that integer and hands it back to the library. The app's
vocabulary is frequency, preset, and sample rate -- transport identity belongs
to ka9q-python.

The stream is a `RadiodStream` bound to that SSRC rather than a
`ManagedStream` bound to a frequency, because only the former follows a
retune; see the task notes.

Tuning order matters as much as identity: the window is centred BEFORE the
frequency is set, because a demodulator that starts parked at the window edge
does not recover when the window later moves onto it.
"""
import asyncio
import logging
from typing import List, Optional

from ka9q import RadiodStream, StreamQuality, discover_channels
from ka9q.types import Encoding

logger = logging.getLogger(__name__)

# RFC 6716 §3.4: one Opus frame is at most 1275 bytes. Larger means the
# channel is serving PCM, i.e. the OUTPUT_ENCODING grant did not take.
MAX_OPUS_FRAME_BYTES = 1275

# How long to wait for RTP after a tune before deciding the demod did not come
# up, and how many times to restart it in place.
TUNE_SETTLE_SEC = 1.5
MAX_TUNE_ATTEMPTS = 2

# Squelch held open: wfm.c forces snr_enable on, so the only way to keep a
# broadcast channel flowing is a threshold nothing fails.
SQUELCH_OPEN_DB = -20.0
SQUELCH_CLOSE_DB = -25.0


def opus_channels(frame: bytes) -> Optional[int]:
    """Channel count from an Opus frame's TOC byte (RFC 6716 §3.1, bit 2)."""
    if not frame:
        return None
    return 2 if (frame[0] >> 2) & 0x01 else 1


class Vfo:
    """One retunable channel plus its listener fan-out."""

    def __init__(self, control, window, destination: str,
                 settle_sec: float = TUNE_SETTLE_SEC):
        self.control = control
        self.window = window
        self.destination = destination
        self.settle_sec = settle_sec
        self.ssrc: Optional[int] = None
        self.freq_hz: Optional[float] = None
        self.preset: Optional[str] = None
        self.sample_rate: Optional[int] = None
        self.channels: Optional[int] = None
        self.listeners: List[asyncio.Queue] = []
        self._stream = None
        self._frames_seen = 0
        self._non_opus_warned = False

    # -- listeners ---------------------------------------------------------
    async def add_listener(self, queue: asyncio.Queue) -> None:
        self.listeners.append(queue)
        if self.channels and self.freq_hz is not None:
            queue.put_nowait({"type": "tuned", "freq_hz": self.freq_hz,
                              "channels": self.channels})

    async def remove_listener(self, queue: asyncio.Queue) -> bool:
        if queue in self.listeners:
            self.listeners.remove(queue)
        if self.listeners:
            return False
        await self.stop()
        return True

    async def stop(self) -> None:
        """Last listener gone: stop the stream and drop the anchor.

        The channel itself stays. It costs radiod one idle demodulator and
        saves the next tune both a creation round-trip and the purge race.
        """
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                await asyncio.to_thread(stream.stop)
            except Exception as e:
                logger.debug(f"vfo stop: {e}")
        await asyncio.to_thread(self.window.release, self.control)
        self.freq_hz = None
        self.channels = None

    # -- tuning ------------------------------------------------------------
    async def tune(self, freq_hz: float, preset: str, sample_rate: int) -> dict:
        """Point the VFO at a station. Returns the message to send listeners."""
        for attempt in range(1, MAX_TUNE_ATTEMPTS + 1):
            self._frames_seen = 0
            await asyncio.to_thread(self._tune_once, freq_hz, preset,
                                    sample_rate, attempt > 1)
            if self._stream is None:
                await self._start_stream()
            await asyncio.sleep(self.settle_sec)
            if self._frames_seen > 0:
                self.freq_hz, self.sample_rate = freq_hz, sample_rate
                return {"type": "tuned", "freq_hz": freq_hz,
                        "channels": self.channels or 1}
            logger.info(
                f"No RTP {self.settle_sec}s after tuning {freq_hz/1e6:.3f} MHz "
                f"(attempt {attempt}/{MAX_TUNE_ATTEMPTS})"
            )
        self.freq_hz, self.sample_rate = freq_hz, sample_rate
        return {"type": "nosignal", "freq_hz": freq_hz}

    def _ensure_channel_exists(self, freq_hz: float, preset: str,
                               sample_rate: int) -> bool:
        """Make sure `self.ssrc` names a channel radiod currently has.

        Returns True if a NEW channel was created (the caller must then point
        a new stream at it). Three cases, in order of cost:

          1. We already hold an SSRC radiod still knows -- nothing to do.
          2. A channel is sitting on our destination from a previous run of
             this app -- adopt it. Adopting is strictly better than removing
             and re-creating, which would start the purge we exist to avoid.
          3. Nothing there -- create one and keep whatever SSRC the library
             allocates.
        """
        if self.ssrc is not None:
            if self.control.poll_channel(self.ssrc, timeout=2.0) is not None:
                return False
            logger.warning(
                f"radiod no longer has SSRC {self.ssrc} -- it restarted; "
                f"re-establishing the VFO"
            )
            self.ssrc = None
            self.preset = None

        try:
            found = discover_channels(self.control.status_address)
            # discover_channels normally returns Dict[SSRC, ChannelInfo]; a
            # test double may hand back the ChannelInfo list directly.
            candidates = found.values() if isinstance(found, dict) else found
            existing = [
                ch for ch in candidates
                if getattr(ch, "multicast_address", None) == self.destination
            ]
        except Exception as e:
            logger.debug(f"vfo adopt scan: {e}")
            existing = []
        if existing:
            self.ssrc = existing[0].ssrc
            self.preset = None  # unknown -- force a preset command below
            logger.info(f"Adopted existing channel SSRC {self.ssrc} as the VFO")
            return True

        self.ssrc = self.control.create_channel(
            frequency_hz=freq_hz, preset=preset, sample_rate=sample_rate,
            gain=0.0, destination=self.destination, encoding=Encoding.OPUS,
        )
        self.preset = preset
        logger.info(f"Created the VFO: SSRC {self.ssrc} on {self.destination}")
        return True

    def _tune_once(self, freq_hz: float, preset: str, sample_rate: int,
                   restart_demod: bool) -> None:
        """Blocking half of a tune. Centre first, then set frequency."""
        fresh = self._ensure_channel_exists(freq_hz, preset, sample_rate)
        if fresh and self._stream is not None:
            # The old stream is following an SSRC that no longer exists.
            try:
                self._stream.stop()
            except Exception as e:
                logger.debug(f"vfo stream stop: {e}")
            self._stream = None

        # Centre BEFORE tuning: a demod that starts at the edge never recovers.
        self.window.centre_on(self.control, freq_hz, self.destination,
                              sample_rate, ssrc_hint=self.ssrc)
        if preset != self.preset or restart_demod:
            # A preset command makes radiod restart the demodulator in place --
            # which is the retry, and is why the retry never destroys the VFO.
            self.control.set_preset(self.ssrc, preset)
            self.preset = preset
        self.control.set_frequency(self.ssrc, freq_hz)
        self.control.set_output_encoding(self.ssrc, Encoding.OPUS)
        try:
            self.control.set_squelch(self.ssrc, enable=True,
                                     open_snr_db=SQUELCH_OPEN_DB,
                                     close_snr_db=SQUELCH_CLOSE_DB)
        except Exception as e:
            logger.debug(f"vfo squelch: {e}")

    async def _start_stream(self) -> None:
        """Attach a RadiodStream to the VFO's SSRC.

        It filters on that SSRC, so it keeps delivering across every retune --
        the reason this is not a ManagedStream.
        """
        info = await asyncio.to_thread(self.control.poll_channel, self.ssrc)
        if info is None:
            logger.error(f"No status for SSRC {self.ssrc}; cannot start stream")
            return

        loop = asyncio.get_running_loop()

        def on_samples(samples: List[bytes], quality: StreamQuality):
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._broadcast, samples)

        stream = RadiodStream(
            channel=info, on_samples=on_samples,
            samples_per_packet=960, deliver_interval_packets=1,
            raw_payloads=True,
        )
        try:
            await asyncio.to_thread(stream.start)
            self._stream = stream
        except Exception as e:
            logger.error(f"Could not start the VFO stream: {e}")

    def _broadcast(self, frames: List[bytes]) -> None:
        for frame in frames:
            if not frame:
                continue
            if len(frame) > MAX_OPUS_FRAME_BYTES:
                if not self._non_opus_warned:
                    self._non_opus_warned = True
                    logger.error(
                        f"Dropping {len(frame)}-byte payload — too large for an "
                        f"Opus frame, so radiod is serving PCM on the VFO."
                    )
                continue
            self._frames_seen += 1
            if self.channels is None:
                self.channels = opus_channels(frame)
                header = {"type": "tuned", "freq_hz": self.freq_hz,
                          "channels": self.channels or 1}
                for q in self.listeners:
                    try:
                        q.put_nowait(header)
                    except asyncio.QueueFull:
                        pass
            for q in self.listeners:
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    pass
