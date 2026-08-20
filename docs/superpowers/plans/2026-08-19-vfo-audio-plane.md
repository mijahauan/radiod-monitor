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
- ~~**Centre the window before tuning.**~~ **WRONG — corrected 2026-08-20 by measurement.** Centre the window **last**, after the frequency and preset are set. `set_preset` restarts the demodulator and `wfm.c` re-runs `set_freq` at demod start, which re-parks the channel at the window edge and drags the LO off whatever was centred. Measured retuning 162.400 nfm → 102.300 wfm, three runs each: centre-first IF −219.2 kHz every time, centre-last IF +0.0 kHz every time. The anchor exists precisely to rescue an already-parked channel, which is centring last.
- The anchor must be placed on whichever side falls **outside** the current window. radiod ignores a channel already in range, so the wrong side is a silent no-op.
- **The app never computes an SSRC.** `create_channel()` allocates one and returns it; the app stores that integer as an opaque handle. Frequency, preset, and sample rate are the app's vocabulary — transport identity is ka9q-python's. Importing `allocate_ssrc` in `backend/` is a defect.
- The RTP receiver is bound to the channel's **SSRC** (`RadiodStream`), never to its frequency (`ManagedStream`), or a retune strands it on a second channel.
- **The anchor lives on its own multicast destination**, separate from the audio group. It carries no audio, and sharing a group lets the VFO's adopt-an-existing-channel scan mistake the anchor for the VFO — which destroys the centring `wfm` depends on.
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
  - `class FrontEndWindow` with `probe(control, destination, sample_rate) -> Optional[float]` (returns usable width, sets `.low_edge_hz`/`.high_edge_hz`/`.usable_bw_hz`), `read(control, ssrc_hint) -> Optional[tuple[float, float]]` (absolute low/high), `centre_on(control, freq_hz, destination, sample_rate, ssrc_hint=None) -> bool`, `release(control)`, and attributes `low_edge_hz`, `high_edge_hz`, `usable_bw_hz`, `anchor_ssrc`.

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


def test_anchor_is_retuned_not_recreated():
    """The anchor is created once and moved thereafter.

    Recreating it per station churns channels radiod reaps only after ~20 s;
    a silent station once accumulated 38 dead channels that way and starved
    the control socket, which is what made switching take minutes.
    """
    import backend.window as W

    class Ctl:
        def __init__(self):
            self.creates = 0
            self.retunes = 0
        def ensure_channel(self, **kw):
            self.creates += 1
            class Ch:
                ssrc = 0xABCD
            return Ch()
        def set_frequency(self, ssrc, hz):
            self.retunes += 1
        def set_squelch(self, ssrc, **kw):
            pass
        def poll_status(self, ssrc, timeout=2.0):
            class FE:
                first_lo = 90_000_000.0
                fe_low_edge = LOW
                fe_high_edge = HIGH
            class S:
                frontend = FE()
            return S()

    w = W.FrontEndWindow()
    w.low_edge_hz, w.high_edge_hz = LOW, HIGH
    c = Ctl()
    assert w.centre_on(c, 102_300_000.0, "239.1.2.3", 48000, ssrc_hint=1) is True
    assert w.centre_on(c, 91_300_000.0, "239.1.2.3", 48000, ssrc_hint=1) is True
    assert c.creates == 1, "the anchor must be created once"
    assert c.retunes >= 1, "subsequent placements must retune it"


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
            if self.anchor_ssrc is None:
                # Created once, then RETUNED -- never recreated per station.
                # Recreating it per station churns channels radiod reaps only
                # after ~20 s, which is how a silent station accumulated 38
                # dead channels and starved the control socket.
                anchor = control.ensure_channel(
                    frequency_hz=anchor_hz, preset="am", sample_rate=sample_rate,
                    gain=0.0, destination=destination,
                    encoding=Encoding.OPUS, timeout=5.0,
                )
                self.anchor_ssrc = anchor.ssrc
                control.set_squelch(self.anchor_ssrc, enable=True,
                                    open_snr_db=80.0, close_snr_db=75.0)
            else:
                control.set_frequency(self.anchor_ssrc, anchor_hz)
        except Exception as e:
            logger.warning(f"Could not place the anchor at {anchor_hz/1e6:.3f} MHz: {e}")
            return False
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
- Produces: `class Vfo` with `async tune(freq_hz, preset, sample_rate) -> dict`, `async add_listener(queue)`, `async remove_listener(queue) -> bool`, `async stop()`, and the module function `opus_channels(frame) -> int | None`. The dict returned by `tune()` is the `tuned`/`nosignal` message the socket sends verbatim.
- **Produces no SSRC function.** The app never computes an SSRC. `control.create_channel()` allocates one and returns it; the VFO stores that integer as an opaque handle and passes it back to the library. If you find yourself importing `allocate_ssrc`, stop — you are re-implementing the library's job.

**Why there is no `ManagedStream` here.** `ManagedStream` is bound to the
*parameters* it was constructed with, not to a channel: it calls
`ensure_channel(frequency_hz=…, preset=…)` at start and again on every restore
(`managed_stream.py:237,421`), deriving the SSRC from them each time. Retune the
VFO and its restore would compute the SSRC of the *old* frequency, silently
create a second channel, and listen to the wrong one. `RadiodStream` filters on
`channel.ssrc`, which a retune does not change — so it follows the VFO. That is
the whole reason this task uses the lower-level class.

Losing `ManagedStream` also loses its restore loop, and that is a gain: the loop
cannot tell an idle station from a dead channel, so it fired on silence and
produced the restore storms this redesign exists to end. A genuinely forgotten
channel is caught explicitly instead — see `_ensure_channel_exists` below.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vfo.py
"""The VFO's identity, ownership, and tuning order.

No radiod: a fake control records the commands issued so the ORDER can be
asserted. Order is the whole point -- a demodulator that starts while parked
at the window edge does not recover when the window later moves onto it, so
the window must be centred BEFORE the frequency is set.
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.vfo as vfo_mod
from backend.vfo import Vfo


class FakeChannelInfo:
    def __init__(self, ssrc, freq=1.0e6):
        self.ssrc = ssrc
        self.frequency = freq
        self.multicast_address = "239.0.0.1"
        self.port = 5004
        self.sample_rate = 48000


class FakeControl:
    """Records calls. `known` is the set of SSRCs radiod admits to having."""

    def __init__(self, known=()):
        self.calls = []
        self.status_address = "fake-radiod.local"
        self.known = set(known)
        self._next_ssrc = 0x1234

    def create_channel(self, **kw):
        assert kw.get("ssrc") is None or "ssrc" not in kw, (
            "the app must not choose the SSRC -- let the library allocate it"
        )
        ssrc = self._next_ssrc
        self._next_ssrc += 1
        self.known.add(ssrc)
        self.calls.append(("create_channel", kw.get("frequency_hz"), kw.get("preset")))
        return ssrc

    def poll_channel(self, ssrc, **kw):
        self.calls.append(("poll_channel", ssrc))
        return FakeChannelInfo(ssrc) if ssrc in self.known else None

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
        self.low_edge_hz, self.high_edge_hz = -330_240.0, 330_240.0
        self.usable_bw_hz = 660_480.0
        self.anchor_ssrc = None

    def centre_on(self, control, freq_hz, destination, sample_rate, ssrc_hint=None):
        control.calls.append(("centre_on", freq_hz))
        return True

    def release(self, control):
        control.calls.append(("release",))


class FakeVfo(Vfo):
    """A Vfo whose stream is a stand-in and whose RTP always arrives."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.started = 0

    async def _start_stream(self):
        self.started += 1
        self._stream = "fake-stream"

    def _tune_once(self, *a, **kw):
        super()._tune_once(*a, **kw)
        self._frames_seen = 1  # pretend RTP followed the retune


def make():
    c = FakeControl()
    v = FakeVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                settle_sec=0.01)
    return c, v


def test_the_app_never_computes_an_ssrc():
    src = open(os.path.join(os.path.dirname(vfo_mod.__file__), "vfo.py")).read()
    assert "allocate_ssrc" not in src, (
        "the SSRC is the library's business; the app holds the handle it returns"
    )


def test_the_ssrc_is_the_one_the_library_allocated(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: [])
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    created = [x for x in c.calls if x[0] == "create_channel"]
    assert len(created) == 1
    assert v.ssrc in c.known


def test_tune_centres_the_window_before_setting_frequency(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: [])
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    names = [x[0] for x in c.calls]
    assert "centre_on" in names and "set_frequency" in names
    assert names.index("centre_on") < names.index("set_frequency"), (
        "a demod that starts at the window edge never recovers"
    )


def test_second_tune_does_not_create_a_channel(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: [])
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert not any(x[0] == "create_channel" for x in c.calls), (
        "the VFO is retuned, never recreated"
    )
    assert v.started == 1, "and its stream follows the SSRC, so it is not restarted"


def test_preset_is_sent_only_when_it_changes(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: [])
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert not any(x[0] == "set_preset" for x in c.calls)
    c.calls.clear()
    asyncio.run(v.tune(162_450_000.0, "nfm", 48000))
    assert any(x[0] == "set_preset" for x in c.calls)


def test_tune_never_removes_the_vfo_channel(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: [])
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert not any(x[0] == "remove_channel" for x in c.calls), (
        "removing it starts a ~20s purge; re-creating inside that window "
        "yields a dead channel"
    )


def test_an_existing_channel_on_our_destination_is_adopted(monkeypatch):
    """Surviving a restart of THIS app, not of radiod."""
    left_behind = FakeChannelInfo(0x9999)
    monkeypatch.setattr(vfo_mod, "discover_channels",
                        lambda *a, **k: [left_behind])
    c = FakeControl(known={0x9999})
    v = FakeVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                settle_sec=0.01)
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert v.ssrc == 0x9999
    assert not any(x[0] == "create_channel" for x in c.calls), (
        "a channel already on our destination IS the VFO -- adopt it"
    )


def test_a_channel_radiod_has_forgotten_is_recreated(monkeypatch):
    """Surviving a restart of radiod."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: [])
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    first = v.ssrc
    c.known.clear()          # radiod restarted; our channel is gone
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert any(x[0] == "create_channel" for x in c.calls)
    assert v.ssrc != first
    assert v.started == 2, "a new channel means a new stream to follow it"


def test_opus_channels_reads_the_toc_byte():
    assert vfo_mod.opus_channels(b"") is None
    assert vfo_mod.opus_channels(bytes([0b00000100])) == 2
    assert vfo_mod.opus_channels(bytes([0b00000000])) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/pytest tests/test_vfo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.vfo'`

- [ ] **Step 3: Write `backend/vfo.py`**

```python
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
            existing = [
                ch for ch in discover_channels(self.control.status_address)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/pytest tests/test_vfo.py -v`
Expected: 9 passed.

Two library facts this task depends on — verify them rather than assuming, and
say in your report what you found:

- `control.create_channel(...)` auto-allocates an SSRC when `ssrc` is omitted
  and returns it (`ka9q/control.py`, "if ssrc is None: ssrc = allocate_ssrc").
  Do **not** pass `ssrc=`.
- `control.poll_channel(ssrc, timeout=…)` returns a `ChannelInfo` or `None`,
  and `RadiodStream(channel=…)` needs `multicast_address`, `port`, `ssrc`, and
  `sample_rate` off it.

If either differs in the installed version, adapt and report the difference —
do not reintroduce an app-side SSRC computation to work around it.

- [ ] **Step 5: Commit**

```bash
git add backend/vfo.py tests/test_vfo.py
git commit -m "feat(vfo): one retunable channel the library names

The app no longer computes SSRCs: create_channel allocates one and the VFO
holds it as an opaque handle. Switching stations is a retune rather than a
channel lifecycle, and the stream follows the SSRC (RadiodStream) instead of
the frequency (ManagedStream), so a retune does not strand it.

Tests assert the rules that matter: never compute an SSRC, centre before
tuning, never remove the VFO, adopt what is already on our destination, and
re-create only when radiod has genuinely forgotten us."
```

---

### Task 3: Wire the controller to sensors only

**Files:**
- Modify: `backend/radio_controller.py`
- Modify: `backend/app.py` (Step 3 only — the activity monitor's window hint)

**Interfaces:**
- Consumes: `FrontEndWindow` (Task 1), `Vfo` (Task 2).
- Produces: `RadioController.vfo` (a `Vfo`), `RadioController.window` (a `FrontEndWindow`); `apply_stations` unchanged in signature.

- [ ] **Step 1: Construct the VFO with the controller**

In `__init__`, after `self.window = FrontEndWindow()`:

```python
        # Where the anchor lives. It carries no audio, so it does not belong
        # on the audio group: sharing one destination is what let the VFO's
        # adopt-an-existing-channel scan pick up the anchor after a restart
        # and destroy the centring that wfm depends on. A separate group also
        # makes the anchor's exemption from the stale sweep structural rather
        # than a special case.
        self.anchor_destination: str = generate_multicast_ip("radiod-monitor-anchor")

        # The one channel the user listens through. Separate from the sensor
        # channels in active_channels, which exist only to report SNR for the
        # activity map and are never listened to.
        self.vfo = Vfo(control=None, window=self.window,
                       destination=self.destination,
                       anchor_destination=self.anchor_destination)
```

`generate_multicast_ip` is already imported at `backend/radio_controller.py:24`.

In `connect()`, after the control is created and the window probed:

```python
        self.vfo.control = self.control
        self.vfo.ssrc = None      # an SSRC belongs to one radiod, not to us
        self.vfo.preset = None
```

Clearing the SSRC is not optional. It is a handle into *this* radiod's channel
table; carrying it across a host switch would aim commands at whatever the new
radiod happens to have under that number. The next `tune()` polls it, finds
nothing, and adopts or creates on the new host — the same path a radiod restart
takes.

- [ ] **Step 2: Release the VFO on close and on host switch**

In `close()`, before the destination sweep:

```python
        try:
            await self.vfo.stop()
        except Exception as e:
            logger.debug(f"close: vfo stop: {e}")
```

The existing sweep then removes the VFO's channel along with the sensors,
which is correct at shutdown: nothing will be re-created.

The sweep matches on `dest_ip = self.destination.split(":")[0]`, so it no
longer covers the anchor now that the anchor has its own group. Extend it to
both. In `close()`, where `dest_ip` is computed:

```python
        dest_ips = tuple(
            d.split(":")[0]
            for d in (self.destination, self.anchor_destination)
        )
```

and change the membership test in the `discover_channels` loop from
`if dest_ip in (ch.multicast_address or "")` to:

```python
                if any(d in (ch.multicast_address or "") for d in dest_ips):
```

Miss this and the anchor survives every shutdown, holding the front end on a
station nobody is listening to until radiod itself restarts — the orphan
accumulation this plan exists to end.

- [ ] **Step 3: Give the activity monitor an SSRC it can always poll**

**Files:** also modify `backend/app.py`.

`activity_monitor()` reads the window every cycle so the frequency strip can
draw it, and `FrontEndWindow.read()` needs some SSRC to poll `first_lo` from.
Today the hint is `next(iter(controller.active_channels), None)`. In directory
mode — the whole FM band, where no set fits the window — `active_channels` is
permanently empty, so the hint is permanently `None` and **no `{"type":
"window"}` message is ever sent**. The strip then shows stations with no window
on the one source that most needs it.

The VFO closes this: it outlives every search and its SSRC is valid the moment
it exists. Change the hint to prefer it:

```python
            # The VFO outlives searches, so it is the reliable hint. In
            # directory mode active_channels is empty by design, and the
            # anchor may not exist until the first Listen -- read() tries
            # its own anchor_ssrc first and falls back to whatever we pass.
            ssrc_hint = (
                controller.vfo.ssrc
                if controller.vfo.ssrc is not None
                else next(iter(controller.active_channels), None)
            )
```

Before the first tune of the session there is still nothing to poll and the
window stays unknown, which is correct: nothing has told radiod where to put
it yet.

- [ ] **Step 4: Verify the app still imports and starts**

```bash
cd /home/mjh/git/radiod-monitor
venv/bin/python -c "import backend.radio_controller" && echo IMPORT_OK
venv/bin/pytest tests/ -v 2>&1 | tail -3
```
Expected: IMPORT_OK and all tests passing. `backend.app` will NOT import yet —
it still references `audio_streamer`, which Task 4 replaces. Say so in your
report rather than trying to fix `app.py` here.

- [ ] **Step 5: Commit**

```bash
git add backend/radio_controller.py backend/app.py
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

**Two `streamer` references in `app.py` die with the module** and must be
replaced, not merely deleted:

- `await streamer.drop_unmonitored(controller.monitored_freqs)` in `_converge`
  — delete outright. It existed to stop `ManagedStream` re-creating channels a
  search had swept; the VFO is never swept, so there is nothing to drop.
- `if not any(streamer.listeners.values()):` guarding
  `controller.window.release(...)` in `_converge` — becomes
  `if not controller.vfo.listeners:`. Keep the guard. Without it a re-search
  drops the anchor under someone who is listening, which un-centres the window
  mid-audio.

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

Add the status helper next to the other audio globals. `audioRepeaterFreq` is
the existing element under the station name in the audio panel:

```javascript
function setAudioStatus(text) {
    if (audioRepeaterFreq) audioRepeaterFreq.textContent = text;
}
```

`tune()` sets it to "tuning…" and the `tuned`/`nosignal`/`error` branches above
replace it, so a 3–4 s worst case reads as the radio working rather than the
app hanging.

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
- Modify: `README.md`
- Modify: `backend/radio_controller.py` (comments only)
- Modify: `backend/sources/base.py` (comments only)

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

Three facts learned during implementation belong in that section, because each
was expensive and none is visible from the code alone:

- **The anchor offset is half the window's WIDTH, not either edge.**
  `target ± ((high_edge - low_edge)/2 - margin)`. The edge-based form is
  identical whenever the window is symmetric about the LO — which is why it
  worked on the Airspy HF+ and hid the bug — but on the R2, whose window sits
  at −4700..−600 kHz, it places the anchor *inside* the window, where radiod
  ignores it. Anchor placement fails silently: there is no error, just a
  station that never comes up.
- **The anchor lives on its own multicast destination** (`radiod-monitor-anchor`).
  It carries no audio, and sharing the audio group let the VFO's
  adopt-an-existing-channel scan mistake the anchor for the VFO after a restart.
  This is also why the anchor needs no exemption from the stale-channel sweep,
  while **the VFO does** — see `_apply_stations_locked`.
- **A channel radiod parks at the window edge cannot be rescued by moving the
  window afterwards.** Centre first, then tune. This is why `Vfo._tune_once`
  calls `centre_on()` before `set_frequency()`.

- [ ] **Step 3: Update the routes block**

```
WS   /ws/audio
  → {"tune": freq_hz}
  ← {"type": "tuned", "freq_hz": N, "channels": 1|2}
  ← {"type": "nosignal", "freq_hz": N}
  ← one binary message per Opus frame
```

- [ ] **Step 4: Clean the stale comments left in the code**

Deleting `backend/audio_streamer.py` left comments referring to a module that
no longer exists. They are comments only — nothing executable — but they point
a reader at the design this plan replaced, which is exactly how the dead anchor
check misled Task 3's implementer. Fix each in place:

- `backend/radio_controller.py:10` — "reachable from AudioStreamer with the
  same arguments"
- `backend/radio_controller.py:60` — "AudioStreamer reads this when..."
- `backend/radio_controller.py:378` — "verify in AudioStreamer._assert_opus,
  once per listener,"
- `backend/sources/base.py:78` — the `audio_channels` note referencing
  `audio_streamer.opus_channels()`, now `backend/vfo.py`'s `opus_channels()`
- `README.md:34` — still lists `backend/audio_streamer.py` in the file
  overview; replace the entry with `backend/vfo.py` and `backend/window.py`,
  and check the rest of the README for the per-station audio socket

Re-word each to name the VFO or `backend/vfo.py`; do not simply delete the
sentences, as each carries a fact that is still true.

- [ ] **Step 5: Verify no stale references**

```bash
cd /home/mjh/git/radiod-monitor
grep -rn "audio_streamer\|AudioStreamer\|focus_on\|ws/audio/{freq" CLAUDE.md README.md backend/ frontend/
```
Expected: empty.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md backend/radio_controller.py backend/sources/base.py
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
