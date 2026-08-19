"""
AudioStreamer — manages one ManagedStream per monitored frequency and
fans raw Opus frames out to WebSocket listeners via asyncio Queues.

ManagedStream is created with `raw_payloads=True`, which is ka9q-python's
transport mode for framed encodings: `on_samples` receives a List[bytes],
one undecoded RTP payload per packet, with the resequencer bypassed
(a codec frame is opaque — it can be neither concatenated nor zero-filled).
With deliver_interval_packets=1 the callback fires once per frame, so each
element is exactly one encoded Opus frame for the browser's WebCodecs
AudioDecoder.

Two radiod behaviours this module has to defend against:

  * **The Opus grant is not implied by the create.** radiod applies the
    preset's default output encoding (s16be) and only honours OPUS when
    OUTPUT_ENCODING is asserted as a follow-up command.  If that is skipped
    the socket carries PCM while the browser has been told it is Opus, and
    WebCodecs fails on the first packet.  `_assert_opus()` asserts it and
    verifies the grant, and it re-asserts after every restore.

  * **The channel count is a property of the stream, not of the preset.**
    The `wfm` preset ships `mono = yes` in some installs and stereo in
    others, so a source-declared count is a guess.  Every Opus packet
    states the truth in its TOC byte (bit 2 = stereo), so the first frame
    of a stream is what configures the decoder — see `opus_channels()`.
    That header is sent to each listener before any audio.

Each frame is then sent to the browser as a single WebSocket binary
message.  No Ogg container, no tagging, no server-side decoding —
WebSocket/TCP preserves frame order and the browser decodes each message
as a self-contained Opus frame.

ManagedStream self-heals across radiod restarts. on_stream_restored
re-applies squelch and the Opus grant, because only hash-stable parameters
(frequency, preset, sample_rate, encoding, destination, gain=0.0) survive
the channel re-creation.
"""
import asyncio
import logging
from typing import Dict, List, Optional

from ka9q import ManagedStream, StreamQuality
from ka9q.types import Encoding

logger = logging.getLogger(__name__)

# Restoration is for surviving a radiod restart, not for waiting out a host
# that is gone. Unbounded retries against an unreachable radiod log ~10 lines
# per attempt forever — that is what grew backend.log to 485 MB. Bounded, the
# stream gives up after ~2 minutes and the user can re-arm it by clicking
# Listen again, which builds a fresh ManagedStream.
RESTORE_INTERVAL_SEC = 2.0
MAX_RESTORE_ATTEMPTS = 60

# RFC 6716 §3.4: a single Opus frame is at most 1275 bytes. A larger payload
# means the channel is not actually serving Opus.
MAX_OPUS_FRAME_BYTES = 1275


def opus_channels(frame: bytes) -> Optional[int]:
    """Channel count from an Opus frame's TOC byte — bit 2 is the stereo flag.

    RFC 6716 §3.1. This is what the encoder actually produced, which beats
    inferring it from a preset name or a radiod status field (there isn't
    one). Returns None for an empty frame.
    """
    if not frame:
        return None
    return 2 if (frame[0] >> 2) & 0x01 else 1


def _assert_opus(control, ssrc: int, freq_key: float) -> None:
    """Assert OPUS output encoding on a channel.

    Creating a channel with encoding=OPUS is not sufficient: radiod applies
    the preset's default (s16be) and honours OPUS only from a follow-up
    OUTPUT_ENCODING command. Skipping this puts PCM on a socket the browser
    decodes as Opus, which fails on the first packet.

    Deliberately not followed by verify_channel(): that costs a full
    discover_channels() poll (~1.5 s) on the path between the user clicking
    Listen and the first audio, and it only re-reads the same control plane
    that just accepted the command. The grant is checked on the wire instead
    — _broadcast() rejects any payload too large to be an Opus frame, which
    is a strictly stronger test (it inspects the actual bytes) and free.
    """
    try:
        control.set_output_encoding(ssrc, Encoding.OPUS)
    except Exception as e:
        logger.error(
            f"Could not set OPUS encoding on SSRC {ssrc:08x} "
            f"({freq_key/1e6:.3f} MHz): {e}"
        )


class AudioStreamer:
    def __init__(self):
        self.active_streams: Dict[float, ManagedStream] = {}
        self.listeners: Dict[float, List[asyncio.Queue]] = {}
        # Channel count observed on the wire, per frequency. Set from the
        # first Opus frame; sent to every listener before any audio.
        self.stream_channels: Dict[float, int] = {}
        self._non_opus_warned: set = set()

    async def add_listener(self, frequency_hz: float, queue: asyncio.Queue, controller):
        freq_key = float(frequency_hz)
        self.listeners.setdefault(freq_key, []).append(queue)
        logger.info(
            f"Listener added for {freq_key/1e6:.3f} MHz "
            f"(total: {len(self.listeners[freq_key])})"
        )

        if freq_key in self.active_streams:
            # Stream is already running: this listener missed the header that
            # went out with the first frame, so hand it the known value now.
            channels = self.stream_channels.get(freq_key)
            if channels:
                queue.put_nowait({"type": "config", "channels": channels})
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
            try:
                controller.control.set_squelch(channel.ssrc, **controller._squelch_args())
            except Exception as e:
                logger.warning(f"Failed to re-apply squelch after restore: {e}")
            # A re-created channel comes back on the preset's default
            # encoding, so the Opus grant has to be asserted again or the
            # browser starts receiving PCM labelled as Opus.
            _assert_opus(controller.control, channel.ssrc, freq_key)
            # A restore usually means radiod restarted, which also resets the
            # front end -- aim it back, but only if this stream is still the
            # one holding focus. A restoring stream for some other station
            # must not steal the window from whoever is actually listening.
            if controller.focused_freq_hz == freq_key:
                controller.focus_on(freq_key)

        dest = getattr(controller, "destination", None)
        preset = getattr(controller, "preset", "nfm")
        sample_rate = getattr(controller, "sample_rate", 48000)

        # Assert the Opus grant BEFORE the receiver starts. radiod serves the
        # preset's default encoding (s16be) from channel creation until
        # OUTPUT_ENCODING is accepted, so asserting it afterwards means the
        # first packets on the socket are PCM — which is then forwarded to the
        # browser as "Opus" and misread by the TOC sniffer. Creating and
        # asserting first makes the stream Opus from its very first packet;
        # ManagedStream then reuses this channel.
        try:
            channel = await asyncio.to_thread(
                controller.control.ensure_channel,
                frequency_hz=freq_key,
                preset=preset,
                sample_rate=sample_rate,
                gain=0.0,
                destination=dest,
                encoding=Encoding.OPUS,
                timeout=5.0,
            )
            await asyncio.to_thread(
                _assert_opus, controller.control, channel.ssrc, freq_key
            )
        except Exception as e:
            logger.error(
                f"Could not prepare Opus channel for {freq_key/1e6:.3f} MHz: {e}"
            )

        # Aim the front end at this station. The window is narrower than the
        # spectrum a searched station set can span, so a station outside it
        # produces no RTP at all -- this is what makes an arbitrary station
        # listenable, at the cost of taking the others out of the window.
        # Done before the receiver starts so the first packets are already
        # on-frequency.
        await asyncio.to_thread(controller.focus_on, freq_key)

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
            restore_interval_sec=RESTORE_INTERVAL_SEC,
            max_restore_attempts=MAX_RESTORE_ATTEMPTS,
            samples_per_packet=960,        # 20 ms at 48 kHz
            deliver_interval_packets=1,    # one frame per callback → lowest latency
            raw_payloads=True,             # deliver undecoded Opus frames
        )

        try:
            await asyncio.to_thread(stream.start)
            try:
                controller.control.set_squelch(
                    stream.channel.ssrc, **controller._squelch_args()
                )
            except Exception as e:
                logger.warning(f"Failed to configure stream for {freq_key/1e6:.3f} MHz: {e}")
            self.active_streams[freq_key] = stream
            ch = stream.channel
            logger.info(
                f"ManagedStream started for {freq_key/1e6:.3f} MHz  "
                f"SSRC={ch.ssrc:08x}  encoding={ch.encoding}  "
                f"channels={getattr(ch, 'channels', '?')}  "
                f"sample_rate={getattr(ch, 'sample_rate', '?')}  "
                f"dest={ch.multicast_address}:{ch.port}"
            )
        except Exception as e:
            logger.error(f"Failed to start stream for {freq_key/1e6:.3f} MHz: {e}")

    async def remove_listener(self, frequency_hz: float, queue: asyncio.Queue,
                               controller) -> bool:
        """Drop one listener. Returns True if that was the last one.

        Whether the radiod channel is left in place depends on who owns it.
        In fit mode the frequency is in controller.active_channels: it
        belongs to the monitored station set, apply_stations() will remove it
        when a search no longer wants it, and the activity monitor still
        needs it to report SNR -- so it stays. In directory mode nothing owns
        an on-demand channel once its last listener leaves: apply_stations()
        never created it, and (per the directory-mode fix that keeps only the
        *focused* frequency) won't clean it up either once focus moves on.
        Leaving it here would orphan one channel per station a user has
        listened to -- for wfm those are squelched wide open (-20 dB), so
        each keeps emitting Opus RTP to the multicast group with nobody
        listening. Remove it ourselves in that case.
        """
        freq_key = float(frequency_hz)
        listeners = self.listeners.get(freq_key, [])
        if queue in listeners:
            listeners.remove(queue)
        if listeners:
            return False
        self.listeners.pop(freq_key, None)
        self.stream_channels.pop(freq_key, None)
        self._non_opus_warned.discard(freq_key)
        stream = self.active_streams.pop(freq_key, None)
        if stream:
            await asyncio.to_thread(stream.stop)
            logger.info(
                f"ManagedStream stopped for {freq_key/1e6:.3f} MHz "
                f"(no more listeners)"
            )
            owned = controller is not None and any(
                abs(f - freq_key) < 1.0
                for f in controller.active_channels.values()
            )
            if not owned and controller is not None and controller.control:
                try:
                    await asyncio.to_thread(
                        controller.control.remove_channel, stream.channel.ssrc
                    )
                    logger.info(
                        f"Removed on-demand channel for {freq_key/1e6:.3f} MHz "
                        f"(not in the monitored station set)"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to remove on-demand channel for "
                        f"{freq_key/1e6:.3f} MHz: {e}"
                    )
        return True

    async def drop_unmonitored(self, monitored: set) -> int:
        """Stop streams for frequencies the current search no longer covers.

        Without this a mode switch leaves an orphaned ManagedStream fighting
        the control plane: apply_stations() removes the channel, ManagedStream
        sees the stream drop and calls ensure_channel to *re-create* it, and
        the next search removes it again. The pair never settle, radiod
        accumulates the re-created channels, and the log fills with restore
        attempts and "not in the monitored set" warnings from the front-end
        focus that follows each restore.

        The listener's WebSocket is left to notice on its own -- its queue
        simply stops filling, and closing it runs the normal teardown.
        """
        stale = [f for f in list(self.active_streams)
                 if not any(abs(f - m) < 1.0 for m in monitored)]
        for freq_key in stale:
            stream = self.active_streams.pop(freq_key, None)
            self.stream_channels.pop(freq_key, None)
            self._non_opus_warned.discard(freq_key)
            if stream:
                try:
                    await asyncio.to_thread(stream.stop)
                except Exception as e:
                    logger.debug(f"drop_unmonitored: {e}")
            logger.info(
                f"Stopped audio for {freq_key/1e6:.3f} MHz — no longer "
                f"in the monitored set"
            )
        return len(stale)

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
        self.stream_channels.clear()

    def _log_non_opus(self, freq_key: float, size: int) -> None:
        """Warn once per frequency that the channel is not serving Opus."""
        if freq_key in self._non_opus_warned:
            return
        self._non_opus_warned.add(freq_key)
        logger.error(
            f"{freq_key/1e6:.3f} MHz: dropping {size}-byte payload — too large "
            f"for an Opus frame, so radiod is serving PCM on this channel. "
            f"The OUTPUT_ENCODING grant did not take."
        )

    def _broadcast(self, freq_key: float, frames: List[bytes]):
        queues = self.listeners.get(freq_key)
        if not queues:
            return
        for frame in frames:
            if not frame:
                continue
            if len(frame) > MAX_OPUS_FRAME_BYTES:
                # RFC 6716 caps a single Opus frame at 1275 bytes. Anything
                # larger is radiod serving PCM on a channel we asked to be
                # Opus — forwarding it would silently feed the browser
                # undecodable audio, so drop it loudly instead.
                self._log_non_opus(freq_key, len(frame))
                continue
            if freq_key not in self.stream_channels:
                # First frame of the stream states the real channel count.
                # Every listener needs it before it can configure a decoder.
                channels = opus_channels(frame)
                if channels:
                    self.stream_channels[freq_key] = channels
                    logger.info(
                        f"{freq_key/1e6:.3f} MHz: radiod is emitting "
                        f"{'stereo' if channels == 2 else 'mono'} Opus"
                    )
                    header = {"type": "config", "channels": channels}
                    for queue in queues:
                        try:
                            queue.put_nowait(header)
                        except asyncio.QueueFull:
                            pass
            for queue in queues:
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    logger.debug(f"Queue full for {freq_key/1e6:.3f} MHz, dropping frame")


streamer = AudioStreamer()
