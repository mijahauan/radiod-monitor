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

Tuning order matters as much as identity: the window is centred LAST, after
the frequency and preset are set. `set_preset` restarts the demodulator and
`wfm.c` re-runs `set_freq` at demod start, which re-parks the channel at the
window edge and drags the LO with it -- so centring any earlier does not
survive. Measured, three runs each, retuning 162.400 nfm -> 102.300 wfm:
centre-first IF -219.2 kHz every time, centre-last IF +0.0 kHz every time.
See `_tune_once`.

The VFO's channel, the window's anchor channel, and the activity-map sensor
channels live on THREE DIFFERENT multicast destinations. That is not tidiness:
`destination` is one of the inputs to the SSRC hash the library allocates
from, so a VFO sharing the sensors' group and tuned to a monitored station
gets the sensor's exact SSRC -- one channel doing two jobs, with the search's
squelch and retunes landing on the channel the user is listening through.
Distinct groups make the collision impossible, make the VFO and the anchor
exempt from the stale-channel sweep structurally, and leave the adopt scan
below looking at a group that contains nothing but the VFO. Sharing one
destination previously let that scan pick up the anchor and mistake it for the
VFO, which then "centred" the window on wherever the anchor happened to be
instead of the station the user asked for.
"""
import asyncio
import logging
import time
from typing import List, Optional

from ka9q import RadiodStream, StreamQuality, discover_channels
from ka9q.types import Encoding

logger = logging.getLogger(__name__)

# RFC 6716 §3.4: one Opus frame is at most 1275 bytes. Larger means the
# channel is serving PCM, i.e. the OUTPUT_ENCODING grant did not take.
MAX_OPUS_FRAME_BYTES = 1275

# How long to wait for RTP after a tune before deciding the demod did not come
# up, and how many times to restart it in place.
TUNE_SETTLE_SEC = 2.5
MAX_TUNE_ATTEMPTS = 2

# Squelch held open: wfm.c forces snr_enable on, so the only way to keep a
# broadcast channel flowing is a threshold nothing fails.
SQUELCH_OPEN_DB = -20.0
SQUELCH_CLOSE_DB = -25.0


def _short_reason(exc: BaseException) -> str:
    """One short line for the "reason" field of a nosignal message."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if not text:
        text = type(exc).__name__
    return text[:120]


def put_control(queue: "asyncio.Queue", msg: dict) -> None:
    """Enqueue a control dict, displacing audio rather than being dropped.

    Control messages share the listener queue with Opus frames. They are not
    interchangeable: the browser ignores every frame until a "tuned" message
    reconfigures its decoder, so a dropped "tuned" on a briefly stalled socket
    means permanent silence until the next click, while a dropped audio frame
    costs 20 ms nobody notices. On overflow, the item discarded to make room
    must be an audio frame, never an earlier control message -- the queue
    head is not good enough, because on a saturated queue it may itself be a
    control dict. So: pop items one at a time, holding aside every dict
    popped, until a `bytes` item is popped (discard it and stop) or the
    queue runs empty; then re-put the held-aside dicts in their original
    order, then the new message. Each pop shrinks the queue by one, so this
    always terminates within `queue.maxsize` iterations.
    """
    try:
        queue.put_nowait(msg)
        return
    except asyncio.QueueFull:
        pass

    held = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(item, bytes):
            break  # an audio frame -- discard it, room made
        held.append(item)

    for item in held:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(
                f"listener queue overflow restoring control message "
                f"{item.get('type')!r}"
            )
    try:
        queue.put_nowait(msg)
    except asyncio.QueueFull:
        logger.warning(
            f"listener queue still full; dropped control message "
            f"{msg.get('type')!r}"
        )


def opus_channels(frame: bytes) -> Optional[int]:
    """Channel count from an Opus frame's TOC byte (RFC 6716 §3.1, bit 2)."""
    if not frame:
        return None
    return 2 if (frame[0] >> 2) & 0x01 else 1


class Vfo:
    """One retunable channel plus its listener fan-out."""

    def __init__(self, control, window, destination: str,
                 anchor_destination: str, settle_sec: float = TUNE_SETTLE_SEC):
        self.control = control
        self.window = window
        self.destination = destination
        self.anchor_destination = anchor_destination
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
        # Whether the last _tune_once managed to place the window on the
        # target. False is a distinguishable cause of "no signal" and is
        # reported as such rather than papered over.
        self._centred = True
        self._lock = asyncio.Lock()

    # -- listeners ---------------------------------------------------------
    async def add_listener(self, queue: asyncio.Queue) -> None:
        self.listeners.append(queue)
        if self.channels and self.freq_hz is not None:
            put_control(queue, {"type": "tuned", "freq_hz": self.freq_hz,
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

        Takes `self._lock` itself rather than trusting callers to hold it --
        a caller outside `tune()` (a mode switch, `close()`) has no lock of
        its own to offer, and without this a Listen click landing inside that
        window could be stopped mid-tune and still be told "tuned". `tune()`
        never calls this, directly or transitively, so there is no deadlock.
        """
        async with self._lock:
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
        async with self._lock:
            if freq_hz != self.freq_hz:
                # A stale channel count from the old station must not survive
                # the retune -- WebCodecs throws if the header lies about it.
                self.channels = None
            self.freq_hz = freq_hz
            self._centred = True
            for attempt in range(1, MAX_TUNE_ATTEMPTS + 1):
                try:
                    await asyncio.to_thread(self._tune_once, freq_hz, preset,
                                            sample_rate, attempt > 1)
                except Exception as e:
                    # radiod refused, went away, or the control socket is
                    # closed. The spec is explicit: report nosignal with a
                    # reason and stop -- do not retry in a loop, and above all
                    # do not let this propagate, because the caller is one of
                    # two tasks sharing the audio WebSocket and an escaping
                    # exception tears the socket down with no explanation.
                    logger.warning(
                        f"Tuning {freq_hz/1e6:.3f} MHz failed: "
                        f"{type(e).__name__}: {e}"
                    )
                    self.sample_rate = sample_rate
                    result = {"type": "nosignal", "freq_hz": freq_hz,
                              "reason": _short_reason(e)}
                    self._broadcast_message(result)
                    return result
                # Zeroed only now: RTP from the PREVIOUS station can still be
                # arriving while _tune_once runs (it blocks in a worker
                # thread while the event loop keeps delivering old frames),
                # and counting those would make a dead new station look live.
                self._frames_seen = 0
                if self._stream is None:
                    await self._start_stream()
                await self._settle()
                if self._frames_seen > 0:
                    self.sample_rate = sample_rate
                    result = {"type": "tuned", "freq_hz": freq_hz,
                              "channels": self.channels or 1}
                    self._broadcast_message(result)
                    return result
                logger.info(
                    f"No RTP {self.settle_sec}s after tuning {freq_hz/1e6:.3f} MHz "
                    f"(attempt {attempt}/{MAX_TUNE_ATTEMPTS})"
                )
            self.sample_rate = sample_rate
            result = {"type": "nosignal", "freq_hz": freq_hz}
            if not self._centred:
                # centre_on returned False: edges unprobed, the anchor is
                # unreadable, or it could not be placed. Any of the three
                # leaves a wideband demod outside the window, which is the
                # single most likely reason no RTP followed.
                result["reason"] = "could not centre the receiver's window"
            self._broadcast_message(result)
            return result

    async def _settle(self) -> None:
        """Wait for RTP to appear after a retune, returning the moment it does.

        A fixed sleep was wrong in both directions. It reported success no
        earlier than `settle_sec` even when audio arrived in 20 ms, so the
        browser sat on "tuning..." and discarded a second and a half of frames
        it had already received (it ignores audio until "tuned" configures the
        decoder). And it reported failure on a live station whose squelch took
        marginally longer than the window to reopen -- measured against
        WXL45 on 162.400 MHz, where a NOSIGNAL was routinely followed by a
        retune to the same frequency yielding its first frame in 0.02 s,
        proving the station had been coming up all along.

        Polling costs one wakeup per 50 ms and makes both cases right: the
        common one returns as fast as the radio does, and the slow one gets
        the full budget before anyone calls it dead. A seam for tests.
        """
        deadline = time.monotonic() + self.settle_sec
        while time.monotonic() < deadline:
            if self._frames_seen > 0:
                return
            await asyncio.sleep(0.05)

    def _broadcast_message(self, msg: dict) -> None:
        """Enqueue a control dict (tuned/nosignal) to every listener queue."""
        for q in self.listeners:
            put_control(q, msg)

    def _ensure_channel_exists(self, freq_hz: float, preset: str,
                               sample_rate: int) -> bool:
        """Make sure `self.ssrc` names a channel radiod currently has.

        Returns True if a NEW channel was created (the caller must then point
        a new stream at it). Three cases, in order of cost:

          1. We already hold an SSRC radiod still knows -- nothing to do.
          2. A channel is sitting on our destination from a previous run of
             this app -- adopt it. Adopting is strictly better than removing
             and re-creating, which would start the purge we exist to avoid.
             This is only safe because the VFO has a destination to itself:
             on the shared group the scan matched sensors and the front-end
             probe's throwaway channel too, so it would adopt an arbitrary
             station's channel, or one radiod was still purging -- a dead
             channel, the precise failure this design exists to eliminate.
             The destination alone does not rule out the "still purging"
             case, though: radiod reaps a channel only once its frequency
             reads zero, so a channel mid-purge on OUR OWN destination (an
             app restart landing inside the ~20 s window `close()` starts,
             which `reload=True` makes routine in development) still answers
             discovery right up until it is reaped. `frequency != 0` below
             is the same freshness gate the sensor reuse path in
             `radio_controller.py` uses for exactly this reason.
          3. Nothing there -- create one and keep whatever SSRC the library
             allocates.
        """
        if self.ssrc is not None:
            if self.control.poll_channel(self.ssrc, timeout=2.0) is not None:
                return False
            # One dropped status reply reads exactly like a radiod restart --
            # confirm with a second poll before tearing the VFO down.
            if self.control.poll_channel(self.ssrc, timeout=2.0) is not None:
                return False
            logger.warning(
                f"radiod no longer has SSRC {self.ssrc} -- it restarted; "
                f"re-establishing the VFO"
            )
            self.ssrc = None
            self.preset = None

        try:
            # discover_channels always returns Dict[SSRC, ChannelInfo]
            # (ka9q/discovery.py); every path goes through the same dict
            # constructor, so no defensive isinstance check is needed here.
            # listen_duration's ~2 s default only costs this establish path,
            # never a routine retune (self.ssrc is already set by then).
            found = discover_channels(self.control.status_address)
            dest_ip = self.destination.split(":")[0]
            existing = [
                ch for ch in found.values()
                if dest_ip in (ch.multicast_address or "")
                and (ch.frequency or 0.0) != 0.0
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
        """Blocking half of a tune. Five steps, and the order is the point.

        **Centre LAST.** Measured on the Airspy HF+ at 660.5 kHz, retuning a
        channel from 162.400 MHz (nfm) to 102.300 MHz (wfm), three runs each:

            centre -> set_freq -> set_preset :  IF -219.2, -219.2, -219.2 kHz
            set_freq -> set_preset -> centre :  IF   +0.0,   +0.0,   +0.0 kHz

        219.2 kHz is `high_edge - filter.max_IF - fudge` for wfm -- radiod's
        edge-parking position. Centring first does not survive, because
        `set_preset` restarts the demodulator and `wfm.c` re-runs `set_freq`
        at demod start, which re-parks the channel at the edge and drags the
        LO with it, undoing the centring. Nothing re-runs `set_freq` after
        step 4, so centring there holds.

        This is also what the anchor was always for: it exists to rescue a
        channel radiod has *already* parked at the edge, by moving the LO onto
        it so every channel recalculates its IF against the new LO. An earlier
        version of this plan asserted the opposite as a global constraint --
        "centre before tuning" -- on the theory that a demod starting at the
        edge never recovers. The measurement above refutes it for the retune
        path, and CLAUDE.md's original account (IF +219.2 / snr -inf / ~17
        frames per 10 s before the anchor, IF +0.0 / snr 6.5 / 500 frames
        after) was always a description of centring last.

        1. Make sure the channel exists.
        2. Set the frequency, so nothing downstream sees a stale one.
        3. Set the preset, if it changed or this is a retry. It restarts the
           demodulator, so it must come after step 2, or wfm.c re-runs
           set_freq from the previous station's frequency.
        4. Re-assert the Opus grant and the held-open squelch.
        5. Centre the window on the target, last, so it survives.

        Raises if there is no control connection -- the caller turns that into
        a nosignal with a reason, which is a great deal more use than an
        AttributeError reported to the user as "Malformed tune request".
        """
        if self.control is None:
            raise RuntimeError("no connection to radiod")

        # 1. Make sure the channel exists.
        fresh = self._ensure_channel_exists(freq_hz, preset, sample_rate)
        if fresh and self._stream is not None:
            # The old stream is following an SSRC that no longer exists.
            try:
                self._stream.stop()
            except Exception as e:
                logger.debug(f"vfo stream stop: {e}")
            self._stream = None

        # 2. Frequency first...
        self.control.set_frequency(self.ssrc, freq_hz)
        # 3. ...then the preset, which restarts the demodulator in place --
        # which is also the retry, and is why the retry never destroys the VFO.
        if preset != self.preset or restart_demod:
            self.control.set_preset(self.ssrc, preset)
            self.preset = preset
        # 4.
        self.control.set_output_encoding(self.ssrc, Encoding.OPUS)
        try:
            self.control.set_squelch(self.ssrc, enable=True,
                                     open_snr_db=SQUELCH_OPEN_DB,
                                     close_snr_db=SQUELCH_CLOSE_DB)
        except Exception as e:
            logger.debug(f"vfo squelch: {e}")

        # 5. Centre LAST -- see this method's docstring. The anchor that does
        # the centring lives on its own destination, never the VFO's, so a
        # later adopt scan can never mistake it for the VFO. With
        # ssrc_hint=None (the session's first tune) centre_on takes its own
        # deadlock-breaking path, so it does not need the VFO channel to
        # exist -- but by this point it always does.
        self._centred = self.window.centre_on(
            self.control, freq_hz, self.anchor_destination,
            sample_rate, ssrc_hint=self.ssrc,
        )

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
            try:
                await asyncio.to_thread(stream.stop)
            except Exception as stop_err:
                logger.debug(f"vfo stream cleanup after failed start: {stop_err}")

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
                # Ground truth for the channel count, latched from the first
                # frame's TOC byte. `tune()` is the sole source of the
                # "tuned" boundary marker sent to listeners -- it only
                # returns after frames have been seen, so self.channels is
                # already populated by the time it broadcasts.
                self.channels = opus_channels(frame)
            for q in self.listeners:
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    pass
