"""The VFO: the one channel the user actually listens to.

The app asks for a frequency, a preset and a sample rate. radiod does the
rest -- including placing the receiver's front-end window, which this module
used to fight over with a decoy "anchor" channel. Measured on an empty
receiver, one channel created and nothing else touched:

    Airspy R2 (4.1 MHz window), wfm 91.300
        LO 93.9500, IF -2650.0 kHz -- exactly the window centre
        snr 29.1, 601 frames in 6 s, voice/hiss 377.6, envelope-var 0.52

    Airspy HF+ (660 kHz window), nfm 162.400
        LO 162.0770, IF +323.0 kHz
        snr 12.0, 301 frames in 6 s, voice/hiss 7.80, envelope-var 0.42

radiod centred the wideband channel unaided, which is exactly what the anchor
was built to achieve, and the narrowband channel worked happily off-centre
without any placement at all. All of that machinery is gone.

Two rules remain, and both are about radiod rather than the radio:

  * A channel is RETUNED where that works and replaced where it does not.
    Destroying a channel starts a ~20 s purge (radio.c reaps a channel only
    once its frequency is zero, after Channel_idle_timeout), and re-creating
    an SSRC inside that window hands back the channel being torn down, which
    never produces RTP. ka9q-python avoids that collision; this module only
    has to avoid needless churn.

  * The SSRC is never computed here. `create_channel()` allocates one and
    this module hands it straight back to the library. The app's vocabulary
    is frequency, preset and sample rate; transport identity is not its
    business.

The stream is a `RadiodStream` bound to the channel rather than a
`ManagedStream` bound to a frequency, because only the former follows a
retune.
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

# Presets whose demodulator needs more of the window than radiod's placement
# guarantees, and which therefore have to be centred in it.
#
# radiod decides a channel is "in range" by its FILTER width and parks it
# `filter.max_IF + fudge` inside an edge. `wfm` runs a 384 kHz composite path
# needing +/-192 kHz, which is more than its filter, so an edge-parked wfm
# channel loses part of that composite and sputters. Measured on the HF+'s
# 660.5 kHz window at 91.300 MHz:
#
#     edge-parked (IF +219.2 kHz)   45 frames / 6 s, no SNR
#     centred     (IF   +0.0 kHz)  501 frames, voice/hiss 496.9, env-var 0.60
#
# Narrowband presets need none of this: nfm at IF +323.0 kHz gives 301 frames
# and snr 12.0. Centring costs a channel create and about a second, so it is
# spent only where it buys something -- NWS first audio is 0.08 s without it.
#
# On a receiver with room the question does not arise: the R2's 4.1 MHz window
# gives 601 frames at snr 29.1 from a plain create, because radiod's placement
# already leaves the whole composite inside.
PRESETS_NEEDING_CENTRE = frozenset({"wfm"})
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
                 anchor_destination: str = "",
                 settle_sec: float = TUNE_SETTLE_SEC,
                 flush_sec: float = TUNE_FLUSH_SEC):
        self.control = control
        self.window = window
        self.destination = destination
        self.anchor_destination = anchor_destination or destination
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
        self._created: dict = {}      # ssrc -> preset it was created with
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
        """Last listener gone: stop the stream. Remove nothing.

        The channel is not dropped. It stays for
        the reason it always did -- removing it starts a purge the next tune
        could land inside, and a channel radiod has not finished reaping
        breaks the demodulator of any channel created after it. Measured at
        91.300 MHz, identical window both times: 3 channels lingering at
        freq 0 gave snr None and 0 frames, while 0 lingering gave snr 19.09
        and 201 frames.

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
                await self._settle(self._settle_budget(preset))
                if self._frames_seen > 0:
                    self.sample_rate = sample_rate
                    result = {"type": "tuned", "freq_hz": freq_hz,
                              "channels": self.channels or 1}
                    self._broadcast_message(result)
                    return result
                logger.info(
                    f"No RTP {self._settle_budget(preset)}s after tuning {freq_hz/1e6:.3f} MHz "
                    f"(attempt {attempt}/{MAX_TUNE_ATTEMPTS})"
                )
            self.sample_rate = sample_rate
            result = {"type": "nosignal", "freq_hz": freq_hz}
            self._broadcast_message(result)
            return result

    def _settle_budget(self, preset: str) -> float:
        """How long to wait for RTP after a tune.

        A centred tune needs longer: centring moves the local oscillator and
        the demodulator restarts against the new placement. Measured on the
        HF+ at 91.300 MHz, the channel came up healthy -- LO 91.3000, snr
        14.3 -- but only after the 2.5 s budget had already expired, so the
        app reported nosignal about a station that was working.
        """
        return self.settle_sec * (2.0 if preset in PRESETS_NEEDING_CENTRE else 1.0)

    async def _settle(self, budget: Optional[float] = None) -> None:
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
        deadline = time.monotonic() + (self.settle_sec if budget is None else budget)
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
        BEFORE the tune begins, so a station change can avoid destroying and
        re-creating a channel when a plain retune will do.
        """
        if self.ssrc is None or self.preset != preset:
            return False
        return (preset not in RECREATE_ON_RETUNE
                or self._channel_freq_hz == freq_hz)

    def _ensure_channel_exists(self, freq_hz: float, preset: str,
                               sample_rate: int,
                               force_new: bool = False) -> bool:
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
        if force_new:
            # Do not keep the channel we were told is dead.
            self.ssrc = None
            self.preset = None


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

        # 2. Retunable channels move onto this frequency so nothing is left
        #    stranded in the band we came from.
        self._park_our_channels(freq_hz)

        if preset in RECREATE_ON_RETUNE:
            # This preset's channel cannot be reused: a wfm channel that has
            # been retuned is permanently dead, and parking it is a retune.
            # Drop ours before making the replacement, or they accumulate --
            # three wfm channels at 91.300 MHz, one reading snr 13.5 and the
            # stream following a silent one. Measured with the removal in
            # place: LO 91.3000, IF 0.0 kHz, snr 13.6, 550 frames, env-var
            # 1.28.
            for ssrc, made_with in list(self._created.items()):
                if made_with != preset:
                    continue
                try:
                    self.control.remove_channel(ssrc)
                except Exception as e:
                    logger.debug(f"replacing {preset} channel {ssrc}: {e}")
                self._created.pop(ssrc, None)
            force_new = True

        self.ssrc = None

        # 3. Create the one channel. The library allocates its SSRC; this app
        #    never chooses or derives one.
        # force_new on a retry: the channel we got did not produce audio, and
        # for wfm that is usually because the deterministic SSRC named a
        # channel left dead by an earlier session -- create_channel reuses an
        # existing SSRC by design, so asking again returns the same corpse.
        # The library steps to one radiod does not have; the app still never
        # picks an SSRC.
        self.ssrc = self.control.create_channel(
            frequency_hz=freq_hz, preset=preset, sample_rate=sample_rate,
            gain=0.0, destination=self.destination, encoding=Encoding.OPUS,
            # Only when the caller has a channel it knows is dead. Insisting
            # on a brand-new channel for every wfm tune -- which this used to
            # do -- accumulated one per attempt and left the app streaming the
            # newest while an earlier one played: three wfm channels at
            # 91.300 MHz, one reading snr 13.5, the stream on a silent one.
            force_new=force_new,
        )
        self.preset = preset
        self._channel_freq_hz = freq_hz
        self._created[self.ssrc] = preset
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

    def _park_our_channels(self, freq_hz: float):
        """Move every channel we own onto the current frequency. Remove none.

        Both halves matter, and they pull in opposite directions.

        A channel left in the band we came from stops the window being placed
        for the band we are going to. Measured, one channel each: nfm 162.400
        alone gave LO 162.4000 and 251 frames; a fresh wfm 91.300 with that
        nfm still live gave LO 91.5192 -- 219.2 kHz off, radiod having
        edge-parked it instead of centring it -- and no audio.

        But REMOVING it is worse. radiod reaps a channel only once its
        frequency reads zero, and until it does the corpse breaks the
        demodulator of any channel created after it. Measured at 91.300 MHz,
        identical window (LO 91.3000) both times:

            3 channels lingering at freq 0    snr None     0 frames
            0 channels lingering at freq 0    snr 19.09  201 frames

        Every cross-band switch used to make two of those -- the old VFO
        channel and the anchor it then had -- which is exactly why switching
        bands mid-session failed while a cold start worked.

        Parking satisfies both: nothing is in the wrong band, and nothing is
        being reaped. The cost is an idle demodulator per preset the session
        has visited, all sitting on the frequency being listened to. They go
        at close(), when nothing follows them.
        """
        # Every retunable one, including the channel we are leaving -- an
        # earlier version skipped `self.ssrc`, which is precisely the channel
        # being replaced, so the old band kept a live channel and the window
        # could not be placed for the new one. Observed directly: after a
        # switch to 91.300 MHz the old nfm channel still read 162.4000.
        #
        # wfm channels are left exactly where they are. Retuning one kills it
        # (see RECREATE_ON_RETUNE), and a dead channel is worse than an
        # out-of-band one: `create_channel` returns the same deterministic
        # SSRC when the listener comes back to that station, handing back the
        # corpse. Left alone it stays alive and is reused as it stands.
        # Measured: a narrowband tune succeeds with a wfm channel sitting in
        # another band, so leaving it costs nothing.
        for ssrc, created_with in sorted(self._created.items()):
            if created_with in RECREATE_ON_RETUNE:
                continue    # retuning one kills it; it is replaced instead
            try:
                self.control.set_frequency(ssrc, freq_hz)
            except Exception as e:
                logger.debug(f"parking {ssrc}: {e}")


    def _tune_once(self, freq_hz: float, preset: str, sample_rate: int,
                   restart_demod: bool) -> None:
        """Blocking half of a tune.

        Make sure a channel exists for what was asked, point it at the
        station, assert the Opus grant and hold the squelch open. radiod
        places the front end itself; nothing here touches it.

        `set_preset` comes after `set_frequency` because a preset command
        restarts the demodulator, and `wfm.c` re-runs `set_freq` at demod
        start -- restart it first and radiod re-places the channel from the
        previous station's frequency.

        Raises if there is no control connection; the caller turns that into
        a nosignal with a reason rather than an AttributeError reported to
        the user as "Malformed tune request".
        """
        if self.control is None:
            raise RuntimeError("no connection to radiod")

        # 1. Make sure the channel exists.
        reusing = self.can_reuse(freq_hz, preset)

        fresh = self._ensure_channel_exists(
            freq_hz, preset, sample_rate,
            # The retry does NOT ask for a new channel. It used to, on the
            # theory that a wfm channel which produced nothing must be dead --
            # but that measurement predates centring. With the window centred
            # the channel is usually fine and merely slow, so creating another
            # one left the app listening to the newest while an earlier one
            # played: observed with three wfm channels at 91.300 MHz, one
            # reading snr 13.5 and the stream following a different, silent
            # one. The retry re-asserts the preset instead, which restarts the
            # demodulator in place.
            force_new=False,
        )
        if fresh and self._stream is not None:
            # The old stream is following an SSRC that no longer exists.
            try:
                self._stream.stop()
            except Exception as e:
                logger.debug(f"vfo stream stop: {e}")
            self._stream = None

        # A channel we just created is already on frequency -- create_channel
        # was given it. Re-sending it is not a no-op: radiod places a NEW
        # channel freshly, but a frequency CHANGE only moves the front end far
        # enough to bring the channel back in range. Measured on the R2, same
        # station and preset both times:
        #
        #   create only            LO 93.9500  IF -2650.0 kHz  601 frames
        #   create + set_frequency LO 162.077  (front end never moved)   0
        #
        # So only retunes send it.
        if not fresh:
            self.control.set_frequency(self.ssrc, freq_hz)

        # The preset restarts the demodulator in place -- which is also the
        # retry, and is why the retry never destroys the channel. It comes
        # after the frequency: restart it first and wfm.c re-runs set_freq
        # from the previous station's frequency.
        if preset != self.preset or restart_demod:
            self.control.set_preset(self.ssrc, preset)
            self.preset = preset
        if preset in PRESETS_NEEDING_CENTRE:
            # Centre AFTER the frequency and preset are set: `set_preset`
            # restarts the demodulator and wfm.c re-runs set_freq at demod
            # start, which re-parks the channel and drags the LO off anything
            # centred earlier.
            self.window.centre_on(self.control, freq_hz,
                                  self.anchor_destination, sample_rate,
                                  ssrc_hint=self.ssrc)

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
