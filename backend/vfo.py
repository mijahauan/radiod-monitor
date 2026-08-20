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

# RTP already in flight when a retune is issued belongs to the PREVIOUS
# station: radiod has sent those 20 ms packets, they are in the socket buffer,
# and they keep arriving for a few tens of milliseconds. Counting them as
# evidence the new station came up made a mode switch report "tuned" in 0.00 s
# and then deliver 11 frames in 8 s -- success declared on the sound of the
# station we just left. Frames are discarded for this long after the retune
# commands land, before any are counted.
TUNE_FLUSH_SEC = 0.15

# Presets whose demodulator does not survive a frequency change, and which
# therefore need a NEW channel per station rather than a retune. Measured on
# the Airspy HF+, one wfm channel, window verified by LO on every reading:
#
#   fresh wfm @91.300      LO 91.3000  IF +0.0k  snr 19.29  251 frames / 5 s
#   retuned to 93.900      LO 93.9000  IF +0.0k  snr -8.45    0 frames / 5 s
#   retuned back to 91.300 LO 91.3000  IF +0.0k  snr None     0 frames / 5 s
#
# The third line is the proof: the same channel, back on the frequency where
# it had just worked, with the window on it, is dead. wfm.c does not survive
# being retuned. Narrowband presets do -- nfm retunes across the NWR band all
# day -- so this is deliberately a small exception list, not a policy.
RECREATE_ON_RETUNE = frozenset({"wfm"})
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
                 anchor_destination: str, settle_sec: float = TUNE_SETTLE_SEC,
                 flush_sec: float = TUNE_FLUSH_SEC):
        self.control = control
        self.window = window
        self.destination = destination
        self.anchor_destination = anchor_destination
        self.settle_sec = settle_sec
        self.flush_sec = flush_sec
        self.ssrc: Optional[int] = None
        # The frequency the current channel was CREATED at, which is not
        # self.freq_hz -- tune() sets that to the requested frequency before
        # the channel work starts. wfm channels are only reusable at the
        # frequency they were born on, so the distinction is load-bearing.
        self._channel_freq_hz: Optional[float] = None
        # Every SSRC this VFO has created. Cleanup must not depend on
        # discovery: discover_channels() is a fixed-duration listen for status
        # multicast and can simply not hear a channel inside its window --
        # observed with live nfm channels at 162.4-162.55 reading 8-9 dB while
        # the sweep that was meant to remove them logged nothing. Anything we
        # made, we can remove by name.
        self._created: set = set()
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
                if self._stream is None:
                    await self._start_stream()
                # Discard the previous station's in-flight RTP before counting
                # anything. radiod keeps demodulating across a frequency
                # change -- there is no stop in the tune path, and ka9q-web
                # does not use one either (control_set_mode sends PRESET alone
                # on a live channel) -- so packets already sent for the old
                # station keep arriving for tens of milliseconds.
                #
                # Tearing down and re-creating the RTP receiver per tune was
                # tried instead, as the definitive version of this. It is
                # worse: measured, 162.400 MHz went from tuning in 0.36 s
                # every time to producing no frames at all, the multicast
                # rejoin being slower and less reliable than the window this
                # replaces. The listener never hears the discarded frames
                # regardless -- the browser ignores audio until "tuned"
                # configures its decoder, which happens after this.
                await asyncio.sleep(self.flush_sec)
                self._frames_seen = 0
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

    def can_reuse(self, freq_hz: float, preset: str) -> bool:
        """Whether the channel we hold can serve this station as it stands.

        A narrowband preset retunes freely; `wfm` only works on the frequency
        it was created for (see RECREATE_ON_RETUNE). The caller needs this
        BEFORE the tune begins, because it decides when the window is centred:
        a channel about to be CREATED must be born inside a window that
        already covers it, while a channel being RETUNED must be centred
        afterwards.
        """
        if self.ssrc is None or self.preset != preset:
            return False
        return (preset not in RECREATE_ON_RETUNE
                or self._channel_freq_hz == freq_hz)

    def _ensure_channel_exists(self, freq_hz: float, preset: str,
                               sample_rate: int) -> bool:
        """Make `self.ssrc` name a live channel serving `freq_hz`/`preset`.

        Returns True when the channel changed, so the caller repoints the
        stream at it.

        **One channel. Never two.** Every channel shares the receiver's single
        front-end window, and a channel live anywhere else in the spectrum
        stops the window being placed for the station the listener picked.
        Measured, one channel each:

            nfm 162.400 alone                  LO 162.4000  snr 8.51  251 frames
            fresh wfm 91.300, nfm still alive  LO  91.5192  snr None    0 frames

        91.5192 is 219.2 kHz off target -- radiod edge-parked the wfm channel
        instead of centring it. Removing the other channel afterwards does not
        repair it, because nothing re-runs placement. So the previous channel
        goes before the new one arrives.

        Retune when it is safe, replace when it is not:

        * A narrowband preset retunes freely. nfm crosses the NWR band all day
          and keeps working, and a retune costs one command with no channel
          lifecycle at all -- which is what makes station switching 0.1 s.
        * `wfm` cannot be retuned. Measured on one channel: fresh at 91.300
          gave snr 19.29 and 251 frames in 5 s; retuned to 93.900, 0 frames;
          retuned BACK to 91.300 -- the frequency where it had just worked,
          window on it -- still 0 frames. It dies on the first retune wherever
          it is pointed, so every FM station gets a new channel.
        """
        # 1. Can we keep what we have?
        if self.can_reuse(freq_hz, preset):
            if self.control.poll_channel(self.ssrc, timeout=2.0) is not None:
                return False
            # One dropped status reply reads exactly like a radiod restart --
            # confirm before tearing anything down.
            if self.control.poll_channel(self.ssrc, timeout=2.0) is not None:
                return False
            logger.warning(
                f"radiod no longer has SSRC {self.ssrc} -- it restarted; "
                f"re-establishing the VFO"
            )
            self.ssrc = None
            self.preset = None

        # 2. Anything still on our group has to go before the new channel is
        #    created, or it will fight the window. This includes the channel
        #    we were just using.
        self._drop_our_channels()

        # 3. Create the one channel. The library allocates its SSRC; this app
        #    never chooses or derives one.
        self.ssrc = self.control.create_channel(
            frequency_hz=freq_hz, preset=preset, sample_rate=sample_rate,
            gain=0.0, destination=self.destination, encoding=Encoding.OPUS,
        )
        self.preset = preset
        self._channel_freq_hz = freq_hz
        self._created.add(self.ssrc)
        born = self.control.poll_channel(self.ssrc, timeout=2.0)
        if born is not None and (getattr(born, "frequency", None) or 0) == 0:
            # radiod hands back a channel it is still reaping if this SSRC was
            # removed inside the last ~20 s. It answers status, but its
            # frequency reads zero and it never emits RTP. Measured: the same
            # SSRC re-created at +2 s and +10 s gave 0 frames; at +25 s it gave
            # snr 19.07 and 201 frames.
            logger.warning(
                f"radiod returned SSRC {self.ssrc} with frequency 0 -- it is "
                f"still purging a channel with this SSRC and will not stream. "
                f"Re-creating a channel for a station left less than ~20 s ago."
            )
        logger.info(f"Created the VFO: SSRC {self.ssrc} on {self.destination}")
        return True

    def _drop_our_channels(self):
        """Clear the spectrum for a new channel: our own, and the anchor.

        The anchor counts. It is a real channel sitting `half_span` from
        whatever station it last centred, so during a band change it is still
        in the OLD band while the new channel is being created -- and a live
        channel elsewhere in the spectrum is exactly what stops the window
        being placed (LO 91.5192 instead of 91.3000, the wfm channel
        edge-parked rather than centred). `centre_on` builds a fresh anchor in
        the right band immediately afterwards.

        Fire-and-forget: radiod reaps them on its own schedule. Nothing is
        re-created in this pass at the same frequency and preset, so the
        removed and created SSRCs are disjoint -- except when a listener
        returns to a station they left seconds ago, which the frequency-0
        check above reports.
        """
        dest_ip = self.destination.split(":")[0]
        try:
            found = discover_channels(self.control.status_address)
        except Exception as e:
            logger.debug(f"vfo sweep: {e}")
            found = {}
        ours = {s for s, ch in found.items()
                if dest_ip in (getattr(ch, "multicast_address", "") or "")}
        # Discovery is a backstop, not the source of truth -- see _created.
        ours |= self._created
        if self.ssrc is not None:
            ours.add(self.ssrc)
        for ssrc in ours:
            try:
                self.control.remove_channel(ssrc)
            except Exception as e:
                logger.debug(f"vfo sweep: remove {ssrc}: {e}")
        try:
            self.window.release(self.control)
        except Exception as e:
            logger.debug(f"vfo sweep: anchor release: {e}")
        self._created.clear()
        self.ssrc = None
        self.preset = None
        self._channel_freq_hz = None


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
        # WHEN the window is centred depends on whether a channel is about to
        # be born or merely retuned, and the two are opposites.
        #
        # A channel being CREATED must be born inside a window that already
        # covers it. radiod places a new channel against the window as it
        # stands, and a wfm demodulator that starts edge-parked never
        # recovers -- centring afterwards does not repair it, because nothing
        # re-runs placement. Measured: create-then-centre left the LO at
        # 91.5192 for a 91.300 station (219.2 kHz off, the edge-park
        # position) and produced no audio, while centre-then-create gave
        # snr 19.29 and 251 frames in 5 s.
        #
        # A channel being RETUNED is the opposite: `set_preset` restarts the
        # demodulator and `wfm.c` re-runs `set_freq` at demod start, which
        # re-parks it and drags the LO off anything centred beforehand.
        # Measured over three runs each, retuning 162.400 nfm -> 102.300 wfm:
        # centre-first gave IF -219.2 kHz every time, centre-last +0.0 kHz.
        reusing = self.can_reuse(freq_hz, preset)
        if not reusing:
            self._drop_our_channels()
            self._centred = self.window.centre_on(
                self.control, freq_hz, self.anchor_destination,
                sample_rate, ssrc_hint=None,
            )

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

        # 5. Centre AFTER a retune only -- a newly created channel was already
        # centred before it was born, and re-centring here would move the LO
        # under a demodulator that has just started.
        if reusing:
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
