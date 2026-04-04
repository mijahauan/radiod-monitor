"""
AudioStreamer — manages one ManagedStream per monitored frequency and
fans raw Opus frames out to WebSocket listeners via asyncio Queues.

ka9q-python's RadiodStream, when configured with Encoding.OPUS, delivers
one Opus frame per RTP packet as a `bytes` object in the on_samples list
(see ka9q/stream.py _parse_samples / _deliver_samples). With
deliver_interval_packets=1 the callback fires once per frame, so each
element of `samples` is exactly one encoded Opus frame suitable for the
browser's WebCodecs AudioDecoder.

Each frame is sent to the browser as a single WebSocket binary message.
No Ogg container, no tagging, no server-side decoding — WebSocket/TCP
preserves frame order and the browser decodes each message as a self-
contained Opus frame.

ManagedStream self-heals across radiod restarts. on_stream_restored
re-applies squelch because only hash-stable parameters (frequency,
preset, sample_rate, encoding, destination, gain=0.0) survive the
channel re-creation.
"""
import asyncio
import logging
from typing import Dict, List

from ka9q import ManagedStream, StreamQuality
from ka9q.types import Encoding

logger = logging.getLogger(__name__)


class AudioStreamer:
    def __init__(self):
        self.active_streams: Dict[float, ManagedStream] = {}
        self.listeners: Dict[float, List[asyncio.Queue]] = {}

    async def add_listener(self, frequency_hz: float, queue: asyncio.Queue, controller):
        freq_key = float(frequency_hz)
        self.listeners.setdefault(freq_key, []).append(queue)
        logger.info(
            f"Listener added for {freq_key/1e6:.3f} MHz "
            f"(total: {len(self.listeners[freq_key])})"
        )

        if freq_key in self.active_streams:
            return

        if not controller or not controller.control:
            logger.error(f"Cannot stream {freq_key/1e6:.3f} MHz: no radiod connection")
            return

        loop = asyncio.get_running_loop()

        def on_samples(samples: List[bytes], quality: StreamQuality):
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(self._broadcast, freq_key, samples)

        def on_dropped(reason: str):
            logger.warning(f"Stream dropped for {freq_key/1e6:.3f} MHz: {reason}")

        def on_restored(channel):
            logger.info(
                f"Stream restored for {freq_key/1e6:.3f} MHz: "
                f"SSRC {channel.ssrc:08x}"
            )
            sq = getattr(controller, "squelch_threshold", 10.0)
            try:
                controller.control.set_squelch(
                    channel.ssrc,
                    open_threshold=sq,
                    close_threshold=sq - 2.0,
                    snr_squelch=True,
                )
            except Exception as e:
                logger.warning(f"Failed to re-apply squelch after restore: {e}")

        dest = getattr(controller, "destination", None)
        preset = getattr(controller, "preset", "nfm")
        sample_rate = getattr(controller, "sample_rate", 48000)

        # Hash-stable parameters must match RadioController exactly so
        # ManagedStream re-attaches to the same deterministic SSRC.
        stream = ManagedStream(
            control=controller.control,
            frequency_hz=freq_key,
            preset=preset,
            sample_rate=sample_rate,
            gain=0.0,
            destination=dest,
            encoding=Encoding.OPUS,
            on_samples=on_samples,
            on_stream_dropped=on_dropped,
            on_stream_restored=on_restored,
            drop_timeout_sec=5.0,
            samples_per_packet=960,        # 20 ms at 48 kHz
            deliver_interval_packets=1,    # one frame per callback → lowest latency
        )

        try:
            await asyncio.to_thread(stream.start)
            sq = getattr(controller, "squelch_threshold", 10.0)
            try:
                controller.control.set_squelch(
                    stream.channel.ssrc,
                    open_threshold=sq,
                    close_threshold=sq - 2.0,
                    snr_squelch=True,
                )
            except Exception as e:
                logger.warning(f"Failed to configure stream for {freq_key/1e6:.3f} MHz: {e}")
            self.active_streams[freq_key] = stream
            logger.info(f"ManagedStream started for {freq_key/1e6:.3f} MHz")
        except Exception as e:
            logger.error(f"Failed to start stream for {freq_key/1e6:.3f} MHz: {e}")

    async def remove_listener(self, frequency_hz: float, queue: asyncio.Queue):
        freq_key = float(frequency_hz)
        listeners = self.listeners.get(freq_key, [])
        if queue in listeners:
            listeners.remove(queue)
        if not listeners:
            self.listeners.pop(freq_key, None)
            stream = self.active_streams.pop(freq_key, None)
            if stream:
                await asyncio.to_thread(stream.stop)
                logger.info(
                    f"ManagedStream stopped for {freq_key/1e6:.3f} MHz "
                    f"(no more listeners)"
                )

    async def stop_all(self):
        """Stop every active stream — used on host switch."""
        for freq_key in list(self.active_streams.keys()):
            stream = self.active_streams.pop(freq_key, None)
            if stream:
                try:
                    await asyncio.to_thread(stream.stop)
                except Exception as e:
                    logger.debug(f"stop_all: {e}")
        self.listeners.clear()

    def _broadcast(self, freq_key: float, frames: List[bytes]):
        queues = self.listeners.get(freq_key)
        if not queues:
            return
        for frame in frames:
            if not frame:
                continue
            for queue in queues:
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    logger.debug(f"Queue full for {freq_key/1e6:.3f} MHz, dropping frame")


streamer = AudioStreamer()
