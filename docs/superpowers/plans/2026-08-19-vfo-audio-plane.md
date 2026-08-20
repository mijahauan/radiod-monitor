# VFO Audio Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-station radiod channels with one retunable VFO channel, so switching stations costs two commands instead of a channel lifecycle.

**Architecture:** Two objects that are currently one. A `FrontEndWindow` owns where the receiver's window sits and places it via an anchor channel. A `Vfo` owns the single audio channel — fixed SSRC, retuned never recreated — and fans Opus frames to browser listeners. `RadioController` keeps only the sensor channels that feed the activity map. The browser holds one audio WebSocket and sends `{"tune": freq}` to change station.

**Tech Stack:** Python 3.13 / FastAPI / uvicorn, ka9q-python >= 3.25.1, vanilla JS + Leaflet + WebCodecs.

**Spec:** `docs/superpowers/specs/2026-08-19-vfo-audio-plane-design.md`

## Global Constraints

- `ka9q-python>=3.25.1` — do not lower.
- The front-end window position is **measured** from radiod (`poll_status` → `first_lo`, plus probed `FE_LOW_EDGE`/`FE_HIGH_EDGE`), never derived from what the app believes it set.
- The VFO is **retuned, never recreated**. Destroying a channel starts a ~20 s asynchronous purge in radiod; re-creating the same SSRC inside that window yields a dead channel. Any "fix" that removes and re-creates the VFO reintroduces the central bug.
- **Centre the window before tuning.** A demodulator that starts parked at the window edge does not recover when the window later moves onto it.
- The anchor must be placed on whichever side falls **outside** the current window. radiod ignores a channel already in range, so the wrong side is a silent no-op.
- `WINDOW_FILL = 0.8`; `DEFAULT_USABLE_BW_HZ = 8_000_000.0`; anchor margin `6_000.0` Hz.
- Audio verification is **content-based**: voice/hiss ratio and envelope variation against a known-good reference (NWR reads ≈14 and ≈0.4; steady hiss reads 0.03). Frame counts and RMS do not distinguish audio from noise.
- Run everything through the project venv: `venv/bin/python`, `venv/bin/pytest`.
- A live radiod is at `airspyhf-status.local`. Only one station is receivable at a time, so **never run audio tests while the user is listening** — and say so if you cannot tell.

---

### Task 1: FrontEndWindow — own where the window sits

Extracts window probing, reading and anchor placement out of `RadioController` into a module with one responsibility. The side-selection rule becomes a pure function so it can be tested without radiod.

**Files:**
- Create: `backend/window.py`
- Create: `tests/test_window_placement.py`
- Modify: `backend/radio_controller.py` (remove the extracted code; wire to the new class)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `choose_anchor_frequency(target_hz, current_lo_hz, low_edge_hz, high_edge_hz, margin_hz=6000.0) -> Optional[float]` — pure; returns the anchor frequency to use, or None when neither side would fall outside the window.
  - `class FrontEndWindow` with `probe(control, destination, sample_rate) -> Optional[float]` (returns usable width, sets `.low_edge_hz`/`.high_edge_hz`/`.usable_bw_hz`), `read(control, ssrc_hint) -> Optional[tuple[float, float]]` (absolute low/high), `centre_on(control, freq_hz, destination, sample_rate) -> bool`, `release(control)`, and attributes `low_edge_hz`, `high_edge_hz`, `usable_bw_hz`, `anchor_ssrc`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_window_placement.py
"""Anchor side selection.

radiod retunes only for a channel whose filter falls OUTSIDE the current
window; a channel already in range is ignored. So an anchor placed on the
wrong side is a silent no-op, and which side is correct depends on where the
LO happens to sit. These are the numbers measured on the Airspy HF+
(+/-330240 Hz window) during the 2026-08-19 session.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.window import choose_anchor_frequency

LOW, HIGH = -330_240.0, 330_240.0
MARGIN = 6_000.0


def test_high_side_used_when_it_falls_outside_the_window():
    # LO far below the target: the high-side anchor is well outside.
    got = choose_anchor_frequency(102_300_000.0, 101_000_000.0, LOW, HIGH, MARGIN)
    assert got == 102_300_000.0 + (HIGH - MARGIN)


def test_low_side_used_when_the_high_side_would_be_inside():
    # The measured failure: the VFO itself pulled the LO to target+219.2 kHz,
    # so the high-side anchor lands at IF +105 kHz -- inside, and ignored.
    lo = 102_300_000.0 + 219_200.0
    got = choose_anchor_frequency(102_300_000.0, lo, LOW, HIGH, MARGIN)
    assert got == 102_300_000.0 + (LOW + MARGIN)


def test_returns_none_when_neither_side_is_outside():
    # A window far wider than the offsets (direct-sampling RX888): no anchor
    # can move it, and none is needed.
    got = choose_anchor_frequency(10_000_000.0, 10_000_000.0, -32e6, 32e6, MARGIN)
    assert got is None


def test_prefers_high_side_when_both_are_outside():
    # Deterministic choice keeps behaviour reproducible.
    got = choose_anchor_frequency(102_300_000.0, 90_000_000.0, LOW, HIGH, MARGIN)
    assert got == 102_300_000.0 + (HIGH - MARGIN)


def test_asymmetric_window_is_honoured():
    # The Airspy R2 reports -4700..-600 kHz -- not centred on the LO.
    lo_edge, hi_edge = -4_700_000.0, -600_000.0
    got = choose_anchor_frequency(98_100_000.0, 98_100_000.0, lo_edge, hi_edge, MARGIN)
    assert got == 98_100_000.0 + (hi_edge - MARGIN)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/pytest tests/test_window_placement.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.window'`

- [ ] **Step 3: Write `backend/window.py`**

```python
"""Where the receiver's front-end window sits, and how to move it.

radiod owns front-end placement and derives it from the frequencies of the
channels it is asked for. It retunes only when a channel's filter would fall
outside the window, and then minimally -- parking the channel
`filter.max_IF + fudge` from the edge (radio.c:set_freq).

That is fine for narrowband demods, whose filters are a few kHz wide. It is
not fine for `wfm`: wfm.c demodulates through a 384 kHz composite path needing
+/-192 kHz around the channel, while radiod reserves only 110 kHz for it. A
wfm channel placed by radiod therefore cannot demodulate -- verified with
radiod's own `tune` utility, which places it at IF +219.2 kHz and reports
snr=-inf.

The workaround is an "anchor": a narrow `am` channel positioned so that
radiod's edge-parking of *the anchor* leaves the window centred on the
frequency we actually want. When radiod reserves the demodulator's real
bandwidth, this whole module reduces to probe() and read().
"""
import logging
from typing import Optional, Tuple

from ka9q.types import Encoding

logger = logging.getLogger(__name__)

# Filter half-width radiod reserves for the anchor's own `am` preset (~5 kHz)
# plus set_freq's 1 kHz fudge.
ANCHOR_MARGIN_HZ = 6_000.0

# Assumed window when radiod does not report FE_LOW_EDGE/FE_HIGH_EDGE.
DEFAULT_USABLE_BW_HZ = 8_000_000.0

# Frequency for the throwaway channel probe() needs: radiod reports the
# front-end limits only alongside per-channel status.
PROBE_FREQ_HZ = 10_000_000.0


def choose_anchor_frequency(
    target_hz: float,
    current_lo_hz: float,
    low_edge_hz: float,
    high_edge_hz: float,
    margin_hz: float = ANCHOR_MARGIN_HZ,
) -> Optional[float]:
    """Where to put the anchor so radiod moves the window onto `target_hz`.

    radiod ignores a channel already inside the window, so the anchor only
    works from a frequency that is outside it. Which side qualifies depends on
    where the LO currently sits -- get it wrong and the anchor is a silent
    no-op, which costs the listener minutes of silence.

    Returns None when neither side is outside, which means the window is far
    wider than the offsets and needs no centring.
    """
    high_side = target_hz + (high_edge_hz - margin_hz)
    low_side = target_hz + (low_edge_hz + margin_hz)
    for candidate in (high_side, low_side):
        offset = candidate - current_lo_hz
        if offset > high_edge_hz or offset < low_edge_hz:
            return candidate
    return None


class FrontEndWindow:
    """Measured position and width of the receiver's usable window."""

    def __init__(self) -> None:
        self.low_edge_hz: Optional[float] = None
        self.high_edge_hz: Optional[float] = None
        self.usable_bw_hz: Optional[float] = None
        self.anchor_ssrc: Optional[int] = None

    def probe(self, control, destination: str, sample_rate: int) -> Optional[float]:
        """Read FE_LOW_EDGE/FE_HIGH_EDGE. Costs one throwaway channel."""
        if control is None:
            return None
        ssrc = None
        try:
            probe = control.ensure_channel(
                frequency_hz=PROBE_FREQ_HZ, preset="am", sample_rate=sample_rate,
                gain=0.0, destination=destination, encoding=Encoding.OPUS, timeout=5.0,
            )
            ssrc = probe.ssrc
            fe = self._frontend_of(control, ssrc)
            low = getattr(fe, "fe_low_edge", None) if fe else None
            high = getattr(fe, "fe_high_edge", None) if fe else None
            if low is not None and high is not None and high > low:
                self.low_edge_hz, self.high_edge_hz = float(low), float(high)
                self.usable_bw_hz = float(high - low)
                logger.info(
                    f"{getattr(fe, 'description', 'front end')}: usable window "
                    f"{self.usable_bw_hz/1e3:.1f} kHz "
                    f"({low/1e3:+.1f}..{high/1e3:+.1f} kHz)"
                )
            else:
                logger.warning("radiod did not report FE_LOW_EDGE/FE_HIGH_EDGE")
        except Exception as e:
            logger.warning(f"Could not probe the front end: {e}")
        finally:
            if ssrc is not None:
                try:
                    control.remove_channel(ssrc)
                except Exception:
                    pass
        return self.usable_bw_hz

    @staticmethod
    def _frontend_of(control, ssrc: int):
        try:
            status = control.poll_status(ssrc, timeout=2.0)
        except Exception as e:
            logger.debug(f"poll_status({ssrc:08x}) failed: {e}")
            return None
        return getattr(status, "frontend", None) or status

    def read(self, control, ssrc_hint: Optional[int]) -> Optional[Tuple[float, float]]:
        """Absolute (low_hz, high_hz) of the window, measured. None if unknown."""
        if control is None or self.low_edge_hz is None or self.high_edge_hz is None:
            return None
        ssrc = self.anchor_ssrc if self.anchor_ssrc is not None else ssrc_hint
        if ssrc is None:
            return None
        fe = self._frontend_of(control, ssrc)
        first_lo = getattr(fe, "first_lo", None) if fe else None
        if first_lo is None:
            return None
        return (first_lo + self.low_edge_hz, first_lo + self.high_edge_hz)

    def centre_on(self, control, freq_hz: float, destination: str,
                  sample_rate: int, ssrc_hint: Optional[int] = None) -> bool:
        """Move the window so `freq_hz` sits near its centre.

        Returns True when the window is (or already was) usable for freq_hz.
        """
        if control is None or self.low_edge_hz is None or self.high_edge_hz is None:
            return False
        window = self.read(control, ssrc_hint)
        if window is None:
            return False
        low_hz, high_hz = window
        centre = (low_hz + high_hz) / 2.0
        current_lo = centre - (self.low_edge_hz + self.high_edge_hz) / 2.0
        anchor_hz = choose_anchor_frequency(
            freq_hz, current_lo, self.low_edge_hz, self.high_edge_hz
        )
        if anchor_hz is None:
            # The window is far wider than the offsets; nothing to centre.
            return True
        try:
            anchor = control.ensure_channel(
                frequency_hz=anchor_hz, preset="am", sample_rate=sample_rate,
                gain=0.0, destination=destination, encoding=Encoding.OPUS, timeout=5.0,
            )
        except Exception as e:
            logger.warning(f"Could not place the anchor at {anchor_hz/1e6:.3f} MHz: {e}")
            return False
        try:
            control.set_squelch(anchor.ssrc, enable=True,
                                open_snr_db=80.0, close_snr_db=75.0)
        except Exception:
            pass
        if self.anchor_ssrc is not None and self.anchor_ssrc != anchor.ssrc:
            try:
                control.remove_channel(self.anchor_ssrc)
            except Exception:
                pass
        self.anchor_ssrc = anchor.ssrc
        logger.info(
            f"Window centred on {freq_hz/1e6:.3f} MHz via anchor at "
            f"{anchor_hz/1e6:.3f} MHz (SSRC {anchor.ssrc:08x})"
        )
        return True

    def release(self, control) -> None:
        """Drop the anchor. Users see a lingering one as a phantom AM station."""
        ssrc, self.anchor_ssrc = self.anchor_ssrc, None
        if ssrc is not None and control is not None:
            try:
                control.remove_channel(ssrc)
            except Exception:
                pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/pytest tests/test_window_placement.py -v`
Expected: 5 passed.

- [ ] **Step 5: Delete the superseded code from `RadioController`**

In `backend/radio_controller.py`, delete these members entirely — every one is
replaced by `FrontEndWindow` or by the VFO in Task 2:
`probe_frontend`, `read_window`, `focus_on`, `_set_focus`, `_assert_frequency`,
`_drop_anchor`, `_reassert_focus`, `clear_focus`, and the attributes
`fe_low_edge_hz`, `fe_high_edge_hz`, `_anchor_ssrc`, `focused_freq_hz`,
`band_center_hz`, and the constants `_ANCHOR_MARGIN_HZ`, `PROBE_FREQ_HZ`.

Add `self.window = FrontEndWindow()` in `__init__`, import it
(`from .window import FrontEndWindow, DEFAULT_USABLE_BW_HZ`), and delete
`RadioController.DEFAULT_USABLE_BW_HZ` in favour of the module-level one.

`connect()` calls `await asyncio.to_thread(self.window.probe, self.control, self.destination, self.sample_rate)` instead of `probe_frontend`.

`fits_window()` stays, reading `self.window.usable_bw_hz or DEFAULT_USABLE_BW_HZ`.

Remove the `self._reassert_focus()` call at the end of `_apply_stations_locked`
and the directory-mode line that retains `self.focused_freq_hz` in `new_freqs`
— the VFO is no longer a monitored channel, so directory mode simply monitors
nothing:

```python
        self.activity_available = self.fits_window(new_freqs)
        if not self.activity_available:
            logger.info(
                f"{len(new_freqs)} stations span more than the receiver's "
                f"window — activity unavailable; the VFO carries whatever the "
                f"listener selects"
            )
            new_freqs = set()
```

- [ ] **Step 6: Verify nothing references the deleted members**

```bash
cd /home/mjh/git/radiod-monitor
grep -rn "probe_frontend\|read_window\|focus_on\|_set_focus\|_assert_frequency\|_drop_anchor\|_reassert_focus\|clear_focus\|focused_freq_hz\|fe_low_edge_hz\|fe_high_edge_hz" backend/ | grep -v '\.pyc'
```
Expected: only hits inside `backend/window.py` (its own attributes). Any hit in
`radio_controller.py` or `app.py` is a miss — fix it. `audio_streamer.py` still
references some of these and is deleted in Task 4; if its hits are the only
ones left, note that in your report and continue.

- [ ] **Step 7: Commit**

```bash
git add backend/window.py tests/test_window_placement.py backend/radio_controller.py
git commit -m "feat(window): own front-end placement in one module

Anchor side selection becomes a pure, tested function: radiod ignores a
channel already inside the window, so the side that works depends on where the
LO sits, and the wrong choice is a silent no-op."
```

---

### Task 2: The VFO — one channel, retuned

**Files:**
- Create: `backend/vfo.py`
- Create: `tests/test_vfo.py`

**Interfaces:**
- Consumes: `FrontEndWindow` from Task 1.
- Produces: `class Vfo` with `async tune(freq_hz, preset, sample_rate) -> dict`, `async add_listener(queue)`, `async remove_listener(queue) -> bool`, `async stop()`, and `vfo_ssrc(control, destination) -> int`. The dict returned by `tune()` is the `tuned`/`nosignal` message the socket sends verbatim.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vfo.py
"""The VFO's identity and retry policy.

No radiod: a fake control records the commands issued so the ORDER can be
asserted. Order is the whole point -- a demodulator that starts while parked
at the window edge does not recover when the window later moves onto it, so
the window must be centred BEFORE the frequency is set.
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.vfo import Vfo, vfo_ssrc


class FakeControl:
    def __init__(self):
        self.calls = []
        self.status_address = "fake-radiod.local"

    def ensure_channel(self, **kw):
        self.calls.append(("ensure_channel", kw.get("frequency_hz"), kw.get("preset")))
        class Ch:
            ssrc = 0x1234
            multicast_address = "239.0.0.1"
            port = 5004
        return Ch()

    def set_frequency(self, ssrc, hz):
        self.calls.append(("set_frequency", hz))

    def set_preset(self, ssrc, preset):
        self.calls.append(("set_preset", preset))

    def set_output_encoding(self, ssrc, enc):
        self.calls.append(("set_output_encoding", enc))

    def set_squelch(self, ssrc, **kw):
        self.calls.append(("set_squelch",))

    def remove_channel(self, ssrc):
        self.calls.append(("remove_channel", ssrc))


class FakeWindow:
    def __init__(self):
        self.centred_on = []
        self.low_edge_hz, self.high_edge_hz = -330_240.0, 330_240.0
        self.usable_bw_hz = 660_480.0
        self.anchor_ssrc = None

    def centre_on(self, control, freq_hz, destination, sample_rate, ssrc_hint=None):
        self.centred_on.append(freq_hz)
        control.calls.append(("centre_on", freq_hz))
        return True

    def release(self, control):
        control.calls.append(("release",))


def test_ssrc_is_stable_across_frequency_and_preset():
    c = FakeControl()
    a = vfo_ssrc(c, "239.1.2.3")
    b = vfo_ssrc(c, "239.1.2.3")
    assert a == b, "the VFO's identity must not depend on what it is tuned to"


def test_tune_centres_the_window_before_setting_frequency():
    c, w = FakeControl(), FakeWindow()
    v = Vfo(control=c, window=w, destination="239.1.2.3")
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    names = [x[0] for x in c.calls]
    assert "centre_on" in names and "set_frequency" in names
    assert names.index("centre_on") < names.index("set_frequency"), (
        "a demod that starts at the window edge never recovers"
    )


def test_second_tune_does_not_create_a_channel():
    c, w = FakeControl(), FakeWindow()
    v = Vfo(control=c, window=w, destination="239.1.2.3")
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    creates_before = sum(1 for x in c.calls if x[0] == "ensure_channel")
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    creates_after = sum(1 for x in c.calls if x[0] == "ensure_channel")
    assert creates_before >= 1
    assert creates_after == 0, "the VFO is retuned, never recreated"


def test_preset_is_sent_only_when_it_changes():
    c, w = FakeControl(), FakeWindow()
    v = Vfo(control=c, window=w, destination="239.1.2.3")
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert not any(x[0] == "set_preset" for x in c.calls)
    c.calls.clear()
    asyncio.run(v.tune(162_450_000.0, "nfm", 48000))
    assert any(x[0] == "set_preset" for x in c.calls)


def test_tune_never_removes_the_vfo_channel():
    c, w = FakeControl(), FakeWindow()
    v = Vfo(control=c, window=w, destination="239.1.2.3")
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    removed = [x for x in c.calls if x[0] == "remove_channel" and x[1] == 0x1234]
    assert removed == [], (
        "removing it starts a ~20s purge; re-creating inside that window "
        "yields a dead channel"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/pytest tests/test_vfo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.vfo'`

- [ ] **Step 3: Write `backend/vfo.py`**

```python
"""The VFO: the one channel the user actually listens to.

It has a fixed SSRC for the life of the process and is RETUNED, never
recreated. That is the load-bearing rule. Destroying a radiod channel starts a
~20 s asynchronous purge (radio.c reaps a channel only once its frequency is
zero, after Channel_idle_timeout); re-creating the same deterministic SSRC
inside that window hands back a channel radiod is still tearing down, which
never produces RTP. Every "switching is broken" symptom in this project traces
to that cycle.

Tuning order matters as much as identity: the window is centred BEFORE the
frequency is set, because a demodulator that starts parked at the window edge
does not recover when the window later moves onto it.
"""
import asyncio
import hashlib
import logging
from typing import Dict, List, Optional

from ka9q import ManagedStream, StreamQuality, allocate_ssrc
from ka9q.types import Encoding

logger = logging.getLogger(__name__)

# RFC 6716 §3.4: one Opus frame is at most 1275 bytes. Larger means the
# channel is serving PCM, i.e. the OUTPUT_ENCODING grant did not take.
MAX_OPUS_FRAME_BYTES = 1275

# Silence is not a dropped stream: a station with no signal is silent while
# perfectly healthy. Treating that as failure produced restore storms that
# saturated radiod's control socket.
DROP_TIMEOUT_SEC = 30.0
RESTORE_INTERVAL_SEC = 10.0
MAX_RESTORE_ATTEMPTS = 12

# How long to wait for RTP after a tune before deciding the demod did not come
# up, and how many times to restart it in place.
TUNE_SETTLE_SEC = 1.5
MAX_TUNE_ATTEMPTS = 2

# A frequency no station uses, for the VFO's identity hash only.
_IDENTITY_FREQ_HZ = 1.0


def opus_channels(frame: bytes) -> Optional[int]:
    """Channel count from an Opus frame's TOC byte (RFC 6716 §3.1, bit 2)."""
    if not frame:
        return None
    return 2 if (frame[0] >> 2) & 0x01 else 1


def vfo_ssrc(control, destination: str) -> int:
    """A stable SSRC for this app's VFO on this radiod.

    Deliberately NOT derived from frequency or preset: an SSRC is only an
    address, and radiod is happy to change everything else about a channel
    that already exists. Deriving it from the station is what forced a new
    channel per station.
    """
    seed = f"radiod-monitor-vfo|{destination}|{getattr(control, 'status_address', '')}"
    nonce = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return allocate_ssrc(
        frequency_hz=_IDENTITY_FREQ_HZ, preset="iq", sample_rate=nonce % 1_000_000,
        agc=False, gain=0.0, destination=destination, encoding=Encoding.OPUS,
        radiod_host=getattr(control, "status_address", None),
    )


class Vfo:
    """One retunable channel plus its listener fan-out."""

    def __init__(self, control, window, destination: str):
        self.control = control
        self.window = window
        self.destination = destination
        self.ssrc: Optional[int] = None
        self.freq_hz: Optional[float] = None
        self.preset: Optional[str] = None
        self.sample_rate: Optional[int] = None
        self.channels: Optional[int] = None
        self.listeners: List[asyncio.Queue] = []
        self._stream: Optional[ManagedStream] = None
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
        """Last listener gone: stop the stream and drop the anchor."""
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
            await asyncio.to_thread(self._tune_once, freq_hz, preset,
                                    sample_rate, attempt > 1)
            if self._stream is None:
                await self._start_stream(freq_hz)
            self._frames_seen = 0
            await asyncio.sleep(TUNE_SETTLE_SEC)
            if self._frames_seen > 0:
                self.freq_hz, self.preset, self.sample_rate = freq_hz, preset, sample_rate
                return {"type": "tuned", "freq_hz": freq_hz,
                        "channels": self.channels or 1}
            logger.info(
                f"No RTP {TUNE_SETTLE_SEC}s after tuning {freq_hz/1e6:.3f} MHz "
                f"(attempt {attempt}/{MAX_TUNE_ATTEMPTS})"
            )
        self.freq_hz, self.preset, self.sample_rate = freq_hz, preset, sample_rate
        return {"type": "nosignal", "freq_hz": freq_hz}

    def _tune_once(self, freq_hz: float, preset: str, sample_rate: int,
                   restart_demod: bool) -> None:
        """Blocking half of a tune. Centre first, then set frequency."""
        if self.ssrc is None:
            self.ssrc = vfo_ssrc(self.control, self.destination)
            self.control.ensure_channel(
                frequency_hz=freq_hz, preset=preset, sample_rate=sample_rate,
                gain=0.0, destination=self.destination,
                encoding=Encoding.OPUS, timeout=5.0, ssrc=self.ssrc,
            )
            self.preset = preset
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
                                     open_snr_db=-20.0, close_snr_db=-25.0)
        except Exception as e:
            logger.debug(f"vfo squelch: {e}")

    async def _start_stream(self, freq_hz: float) -> None:
        loop = asyncio.get_running_loop()

        def on_samples(samples: List[bytes], quality: StreamQuality):
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._broadcast, samples)

        stream = ManagedStream(
            control=self.control, frequency_hz=freq_hz, preset=self.preset,
            sample_rate=self.sample_rate or 48000, gain=0.0,
            destination=self.destination, encoding=Encoding.OPUS,
            on_samples=on_samples, drop_timeout_sec=DROP_TIMEOUT_SEC,
            restore_interval_sec=RESTORE_INTERVAL_SEC,
            max_restore_attempts=MAX_RESTORE_ATTEMPTS,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/pytest tests/test_vfo.py -v`
Expected: 5 passed.

If `ensure_channel` in your installed ka9q-python does not accept an `ssrc=`
keyword, drop that argument and instead call `control.create_channel(...)` with
the explicit SSRC, or set the frequency on the computed SSRC directly — report
in your notes which you used and why.

- [ ] **Step 5: Commit**

```bash
git add backend/vfo.py tests/test_vfo.py
git commit -m "feat(vfo): one retunable channel with a stable identity

The SSRC no longer depends on what the channel is tuned to, so switching
stations is a retune rather than a channel lifecycle. Tests assert the two
rules that matter: centre before tuning, and never remove the VFO."
```

---

### Task 3: Wire the controller to sensors only

**Files:**
- Modify: `backend/radio_controller.py`

**Interfaces:**
- Consumes: `FrontEndWindow` (Task 1), `Vfo` (Task 2).
- Produces: `RadioController.vfo` (a `Vfo`), `RadioController.window` (a `FrontEndWindow`); `apply_stations` unchanged in signature.

- [ ] **Step 1: Construct the VFO with the controller**

In `__init__`, after `self.window = FrontEndWindow()`:

```python
        # The one channel the user listens through. Separate from the sensor
        # channels in active_channels, which exist only to report SNR for the
        # activity map and are never listened to.
        self.vfo = Vfo(control=None, window=self.window, destination=self.destination)
```

In `connect()`, after the control is created and the window probed:

```python
        self.vfo.control = self.control
```

- [ ] **Step 2: Release the VFO on close and on host switch**

In `close()`, before the destination sweep:

```python
        try:
            await self.vfo.stop()
        except Exception as e:
            logger.debug(f"close: vfo stop: {e}")
```

The existing sweep then removes the VFO's channel and the anchor along with
the sensors, which is correct at shutdown: nothing will be re-created.

- [ ] **Step 3: Verify the app still imports and starts**

```bash
cd /home/mjh/git/radiod-monitor
venv/bin/python -c "import backend.radio_controller" && echo IMPORT_OK
venv/bin/pytest tests/ -v 2>&1 | tail -3
```
Expected: IMPORT_OK and all tests passing. `backend.app` will NOT import yet —
it still references `audio_streamer`, which Task 4 replaces. Say so in your
report rather than trying to fix `app.py` here.

- [ ] **Step 4: Commit**

```bash
git add backend/radio_controller.py
git commit -m "feat(controller): own sensors and the VFO separately

active_channels is now only the sensor set feeding the activity map. The
channel the user listens through is the VFO and is not swept by a search."
```

---

### Task 4: The single audio socket

**Files:**
- Modify: `backend/app.py`
- Delete: `backend/audio_streamer.py`

**Interfaces:**
- Consumes: `RadioController.vfo` (Task 3).
- Produces: `WS /ws/audio` accepting `{"tune": <freq_hz>}` and emitting `{"type":"tuned"|"nosignal", ...}` plus binary Opus frames.

- [ ] **Step 1: Replace the route**

Delete the whole `websocket_audio` function and its `@app.websocket("/ws/audio/{freq_hz}")` decorator, and write:

```python
@app.websocket("/ws/audio")
async def websocket_audio(websocket: WebSocket):
    """One socket for the session; the station changes underneath it.

    The browser sends {"tune": freq_hz} to change station. Because the VFO's
    SSRC never changes, no WebSocket, stream or radiod channel is torn down or
    created on a switch -- it is two commands to radiod.

    Every "tuned" message is a boundary marker: the browser resets its Opus
    decoder on it, which it must do anyway because the channel count can
    differ between presets.
    """
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    await controller.vfo.add_listener(queue)

    async def _pump():
        while True:
            item = await queue.get()
            if isinstance(item, dict):
                await websocket.send_json(item)
            else:
                await websocket.send_bytes(item)

    async def _commands():
        while True:
            msg = await websocket.receive_json()
            freq_hz = msg.get("tune")
            if freq_hz is None:
                continue
            freq_hz = float(freq_hz)
            if not any(abs(f - freq_hz) < 1.0 for f in controller.monitored_freqs):
                await websocket.send_json({
                    "type": "error",
                    "message": f"{freq_hz/1e6:.3f} MHz is not currently monitored — "
                               f"run a search first.",
                })
                continue
            result = await controller.vfo.tune(
                freq_hz, controller.preset, controller.sample_rate
            )
            await websocket.send_json(result)

    sender = asyncio.create_task(_pump())
    commands = asyncio.create_task(_commands())
    try:
        done, pending = await asyncio.wait(
            {sender, commands}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"audio ws error: {e}")
    finally:
        await controller.vfo.remove_listener(queue)
```

- [ ] **Step 2: Drop the streamer everywhere**

Delete `from .audio_streamer import streamer`. Replace the three remaining uses:
- in `lifespan` shutdown, `await streamer.stop_all()` becomes nothing (the
  controller's `close()` stops the VFO);
- in `radiod_select`, `await streamer.stop_all()` becomes
  `await controller.vfo.stop()`;
- in the search handler's `_converge`, delete the
  `await streamer.drop_unmonitored(...)` call and its comment — sensors are
  rebuilt by `apply_stations` and the VFO is not a sensor.

Then delete the file:

```bash
git rm backend/audio_streamer.py
```

- [ ] **Step 3: Verify the backend end to end**

```bash
cd /home/mjh/git/radiod-monitor
venv/bin/python -c "import backend.app" && echo IMPORT_OK
venv/bin/pytest tests/ -v 2>&1 | tail -3
./radiod-monitor.sh restart && sleep 10 && tail -5 backend.log
```

Then, **only if no one else is using the receiver**, exercise it:

```bash
timeout 115 venv/bin/python - <<'EOF'
import asyncio, json, ssl, time, websockets
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
async def main():
    async with websockets.connect("wss://localhost:8443/ws/control", ssl=ctx) as ws:
        await ws.send(json.dumps({"type":"search","mode":"fm","location":"EM38ww",
                                  "radius":300,"squelch":10,"params":{}}))
        while True:
            r=json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
            if r.get("type")=="results": break
    async with websockets.connect("wss://localhost:8443/ws/audio", ssl=ctx, max_size=None) as a:
        for f in (102300000, 91300000, 102300000):
            t0=time.monotonic(); n=0; first=None
            await a.send(json.dumps({"tune": f}))
            end=t0+15
            while time.monotonic()<end:
                try: d=await asyncio.wait_for(a.recv(), timeout=3)
                except asyncio.TimeoutError: continue
                if isinstance(d,str): continue
                if first is None: first=time.monotonic()-t0
                n+=1
            print(f"  {f/1e6:7.1f} MHz  first audio {first if first else -1:.1f}s  frames={n}")
asyncio.run(main())
EOF
```

Expected: audio within ~2 s on each tune, including the switch back — that
third line is the case that fails today. Record the channel count on radiod
before and after the three tunes; it must not change:

```bash
venv/bin/python -c "import logging; logging.basicConfig(level=logging.CRITICAL); from ka9q import discover_channels; print(len(discover_channels('airspyhf-status.local',2.0)))"
```

- [ ] **Step 4: Commit**

```bash
git add backend/app.py && git rm backend/audio_streamer.py
git commit -m "feat(api): one audio socket, tuned underneath

Switching stations no longer tears down a socket, a stream and a channel."
```

---

### Task 5: Frontend — one socket, tune underneath

**Files:**
- Modify: `frontend/app.js`
- Modify: `frontend/index.html` (cache-bust)

**Interfaces:**
- Consumes: `WS /ws/audio` (Task 4).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Rework `AudioSession` into a persistent connection**

Replace the existing `AudioSession` class's per-frequency behaviour: the
constructor takes no frequency, `start()` opens `/ws/audio` once, and a new
`tune(freqHz, name)` method sends `{tune: freqHz}`. On every `tuned` message,
close and recreate the `AudioDecoder` (Opus decoder state is per-stream, and
the channel count can change between presets), then configure it with
`msg.channels`. On `nosignal`, surface it to the UI and keep the socket open.

```javascript
    async tune(freqHz, name) {
        this.pendingName = name;
        this.tuning = true;
        this.pending = [];
        this.playing = false;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ tune: freqHz }));
        }
    }

    _onControlMessage(text) {
        let msg;
        try { msg = JSON.parse(text); } catch (_) { return; }
        if (msg.type === 'tuned') {
            this.tuning = false;
            this.freqHz = msg.freq_hz;
            this._resetDecoder(msg.channels || 1);
            setAudioStatus(`${this.pendingName || ''} ${(msg.freq_hz/1e6).toFixed(1)} MHz`);
        } else if (msg.type === 'nosignal') {
            this.tuning = false;
            setAudioStatus(`no signal on ${(msg.freq_hz/1e6).toFixed(1)} MHz`);
        } else if (msg.type === 'error') {
            this.tuning = false;
            setAudioStatus(msg.message);
        }
    }

    _resetDecoder(channels) {
        if (this.decoder && this.decoder.state !== 'closed') {
            try { this.decoder.close(); } catch (_) {}
        }
        this.decoder = new AudioDecoder({
            output: (a) => this._onDecoded(a),
            error: (e) => console.error('AudioDecoder error:', e),
        });
        this.decoder.configure({
            codec: 'opus', sampleRate: AUDIO_SAMPLE_RATE, numberOfChannels: channels,
        });
        this.numChannels = channels;
    }
```

Add a `setAudioStatus(text)` helper that writes into the existing audio panel
element, and show "tuning…" from `tune()` until a `tuned` or `nosignal`
arrives, so a 3–4 s worst case reads as the radio working.

- [ ] **Step 2: Point `listenToStation` at the persistent session**

```javascript
let audioSession = null;

async function listenToStation(freqHz, name) {
    audioPanel.classList.remove('hidden');
    resumeAudioBtn.classList.add('hidden');
    if (!audioSession) {
        audioSession = new AudioSession();
        try {
            await audioSession.start();
        } catch (err) {
            console.error('Audio start failed:', err);
            alert(`Failed to start audio: ${err.message}`);
            audioSession = null;
            return;
        }
    }
    setAudioStatus('tuning…');
    await audioSession.tune(freqHz, name);
    const marker = markers[freqHz];
    if (marker) marker.openPopup();
    map.closePopup();
}
```

`stopAudioBtn.onclick` closes the session entirely (`audioSession.stop();
audioSession = null;`), which is what releases the anchor server-side.

- [ ] **Step 3: Bump the cache-busting query**

In `frontend/index.html`, advance `/static/app.js?v=` and `/static/style.css?v=`
by one each, so a returning browser picks up the new protocol.

- [ ] **Step 4: Verify**

```bash
cd /home/mjh/git/radiod-monitor
node --check frontend/app.js && echo JS_OK
./radiod-monitor.sh restart && sleep 10
curl -sk https://localhost:8443/ | grep -oE "app.js\?v=[0-9]+|style.css\?v=[0-9]+"
curl -sk "https://localhost:8443/static/app.js?v=$(curl -sk https://localhost:8443/ | grep -oE 'app.js\?v=[0-9]+' | grep -oE '[0-9]+')" | grep -c '"tune"'
```

You have NO browser: do not claim you saw the UI. State plainly that visual
confirmation is left to the human.

- [ ] **Step 5: Commit**

```bash
git add frontend/
git commit -m "feat(ui): one audio socket for the session, tuned per station"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace the audio-plane section**

Rewrite the "Audio plane" bullet of the Shared pipeline section to describe the
VFO: one channel, fixed SSRC, retuned never recreated; the window centred
before tuning; the retry restarting the demod in place; and the reason each
rule exists (the ~20 s purge race, and a demod that starts at the window edge
never recovering). Keep the existing material on the Opus grant,
`raw_payloads=True`, the TOC-byte channel count, and the TTL=0 loopback join —
all still true.

- [ ] **Step 2: Replace the focus/anchor material**

Replace the `focus_on()` paragraphs with a pointer to `backend/window.py`:
anchor side selection depends on where the LO sits, radiod ignores an anchor
already inside the window, and the whole module collapses to `probe()` and
`read()` if radiod is ever fixed to reserve the demodulator's real bandwidth.

- [ ] **Step 3: Update the routes block**

```
WS   /ws/audio
  → {"tune": freq_hz}
  ← {"type": "tuned", "freq_hz": N, "channels": 1|2}
  ← {"type": "nosignal", "freq_hz": N}
  ← one binary message per Opus frame
```

- [ ] **Step 4: Verify no stale references**

```bash
cd /home/mjh/git/radiod-monitor
grep -n "audio_streamer\|focus_on\|ws/audio/{freq" CLAUDE.md
```
Expected: empty.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: the audio plane is one retunable VFO"
```

---

## Verification

```bash
venv/bin/pytest tests/ -v
./radiod-monitor.sh restart && sleep 10
```

1. Tune a station with signal → audio within ~2 s, voice/hiss > 10.
2. Switch stations → audio within ~2 s, **no** change in radiod's channel count.
3. Switch back → same, no purge wait. This fails today.
4. Switch mode FM → NWS → preset changes, sensors rebuild, VFO SSRC unchanged.
5. Hold a silent station 90 s → no restore storm, channel count stable, a
   concurrent search still completes promptly.
6. Stop listening → anchor gone, only sensors remain on radiod.
