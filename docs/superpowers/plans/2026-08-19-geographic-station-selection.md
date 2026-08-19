# Geographic Station Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user choose a station by location and identity instead of by band segment, and show the receiver's frequency window as information rather than as a chore.

**Architecture:** Band sub-segmentation is removed from the `Source` contract; sources return every station within the geographic radius. `RadioController` decides from the measured front-end window whether the whole station set fits — monitoring all of them with live activity if so, or acting as a directory and creating a channel only for the station the user selects if not. A frequency strip in the UI draws every station at its frequency plus the window's measured position.

**Tech Stack:** Python 3.13 / FastAPI / uvicorn backend, vanilla JS + Leaflet frontend, ka9q-python >= 3.25.1 against ka9q-radio's radiod.

**Spec:** `docs/superpowers/specs/2026-08-19-geographic-station-selection-design.md`

## Global Constraints

- `ka9q-python>=3.25.1` — do not lower. Earlier versions drop OPUS payloads silently and send `DEMOD_TYPE` contradicting the preset.
- The front-end window is **measured** from radiod (`FE_LOW_EDGE`/`FE_HIGH_EDGE`, `FIRST_LO_FREQUENCY`), never computed from what the app believes it set.
- `WINDOW_FILL = 0.8` — fit is tested against `usable_bw_hz * WINDOW_FILL`.
- `DEFAULT_USABLE_BW_HZ = 8_000_000.0` when radiod does not report the edges.
- Audio verification is **content-based**: compare voice/hiss ratio and envelope variation against a known-good NWR station (voice/hiss ~14, envelope variation ~0.4). Steady hiss reads 0.03. Frame counts and RMS alone do not distinguish audio from noise.
- This project has no pre-existing test suite. Task 2 introduces `tests/` for pure logic only; hardware-dependent behaviour is verified with the commands given in each task.
- Run everything through the project venv: `venv/bin/python`, `venv/bin/pytest`.

---

### Task 1: Remove band segmentation from the Source contract

Deletes `center_freq_hz()` and `segment_band()`, and drops the `usable_bw_hz`
argument from `controls_schema()`. All three sources and their callers change
together — splitting this leaves the app unable to start.

**Files:**
- Modify: `backend/sources/base.py`
- Modify: `backend/sources/fm.py`
- Modify: `backend/sources/repeaters.py`
- Modify: `backend/sources/nws.py`
- Modify: `backend/app.py`
- Modify: `backend/radio_controller.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Source.controls_schema(self) -> Dict[str, Any]` (no arguments);
  `Source.list_stations(self, lat, lon, radius_km, params) -> List[Station]`
  unchanged in signature but no longer filters by sub-segment. `Source` no
  longer has `center_freq_hz`. `RadioController` no longer has `tune_center`
  or `band_center_hz`.

- [ ] **Step 1: Delete `segment_band`, `SEGMENT_FILL`, `DEFAULT_USABLE_BW_HZ` and `center_freq_hz` from the contract**

In `backend/sources/base.py`, delete the `SEGMENT_FILL` and
`DEFAULT_USABLE_BW_HZ` constants and the whole `segment_band()` function, and
delete the `center_freq_hz` method from `Source`. Change the remaining
`controls_schema` to:

```python
    def controls_schema(self) -> Dict[str, Any]:
        """
        Return a JSON-serializable description of this source's UI controls.

        Currently supported keys:
          - `bandSegments`: [{value, label}, ...] — if present, the frontend
            shows a band dropdown and passes the selected value back in
            params["band"]. Use it only for bands the user has an opinion
            about (2m vs 70cm), never to subdivide a band to fit the
            receiver: that is the app's problem, not the user's, and
            RadioController solves it by monitoring what fits and treating
            the rest as a directory.
          - `defaultBand`: string — initial selection.

        Return an empty dict for sources with no extra controls.
        """
        return {}
```

Trim the `Optional, Tuple` imports if they become unused.

- [ ] **Step 2: Make `FmSource` control-free and radius-only**

In `backend/sources/fm.py`, delete `_BAND_LOW_MHZ`, `_BAND_HIGH_MHZ`,
`_KEY_PREFIX`, `_segments()`, `_segment_for()`, `controls_schema()` and
`center_freq_hz()`, and drop `segment_band` from the import. In
`list_stations`, replace the two-line band lookup and the frequency-range
guard with a fixed band check:

```python
        # The whole FM broadcast band. Which of these the receiver can hear at
        # once is RadioController's business, not this source's.
        low_hz, high_hz = 88.0e6, 108.0e6
```

Leave the rest of `list_stations` as it is — the `if not (low_hz <= freq_hz <= high_hz): continue` guard still applies.

- [ ] **Step 3: Give `RepeaterSource` real bands only**

In `backend/sources/repeaters.py`, replace `_BANDS`, `_segments()` and
`_segment_for()` with:

```python
# Real amateur bands the user has an opinion about. Not subdivided: a band
# too wide for the connected receiver becomes a directory (see
# RadioController.fits_window), which is not something the user should have
# to think about.
_BANDS: Dict[str, Tuple[float, float, str]] = {
    "2m":    (144.0, 148.0, "2m (144 – 148 MHz)"),
    "1.25m": (222.0, 225.0, "1.25m (222 – 225 MHz)"),
    "70cm":  (420.0, 450.0, "70cm (420 – 450 MHz)"),
}
_DEFAULT_BAND = "2m"


def _band_for(band):
    """Resolve a band key to (low_mhz, high_mhz), falling back to 2m."""
    low, high, _ = _BANDS.get(band) or _BANDS[_DEFAULT_BAND]
    return low, high
```

Replace `controls_schema` with:

```python
    def controls_schema(self) -> Dict[str, Any]:
        return {
            "bandSegments": [
                {"value": key, "label": label}
                for key, (_low, _high, label) in _BANDS.items()
            ],
            "defaultBand": _DEFAULT_BAND,
        }
```

Delete `center_freq_hz`. In `list_stations`, replace the `_segment_for(...)`
call with `low, high = _band_for(params.get("band"))`.

- [ ] **Step 4: Drop `center_freq_hz` from `NwsSource`**

In `backend/sources/nws.py`, delete the `center_freq_hz` method and change
`controls_schema(self, usable_bw_hz=None)` back to `controls_schema(self)`,
keeping its comment.

- [ ] **Step 5: Delete `tune_center` and `band_center_hz` from the controller**

In `backend/radio_controller.py`, delete the whole `tune_center` method and
the `self.band_center_hz` attribute, and delete the `self._reassert_focus()`
call's neighbouring comment block that refers to `tune_center()`. Replace that
comment with:

```python
        # No front-end tuning to a band centre: radiod places the front end
        # from the channels it was asked for and can only cover one window at
        # a time. focus_on() aims it at the station a listener chose.
        #
        # Every channel created above just moved the front end, so if a
        # listener is holding a station, aim it back at them.
        self._reassert_focus()
```

- [ ] **Step 6: Update the callers in `app.py`**

In `backend/app.py`: change `"controls": s.controls_schema(controller.usable_bw_hz),`
to `"controls": s.controls_schema(),` and delete the two-line comment above it.
Delete the line `params = dict(params, usable_bw_hz=controller.usable_bw_hz)`
and its comment. Delete the line `controller.tune_center(source.center_freq_hz(params))`.

- [ ] **Step 7: Verify the app starts and serves both sources**

```bash
venv/bin/python -c "import ast,sys; [ast.parse(open(f).read()) for f in ['backend/app.py','backend/radio_controller.py','backend/sources/base.py','backend/sources/fm.py','backend/sources/repeaters.py','backend/sources/nws.py']]; print('parse ok')"
./radiod-monitor.sh restart && sleep 10
curl -sk https://localhost:8443/api/sources | venv/bin/python -m json.tool | head -40
```

Expected: HTTP 200 with three sources. Commercial FM has `"controls": {}`.
VHF/UHF Repeaters has exactly three bandSegments (`2m`, `1.25m`, `70cm`) with
no `center_mhz` key. NOAA Weather Radio has `"controls": {}`.

- [ ] **Step 8: Verify a search returns the whole band**

```bash
timeout 120 venv/bin/python - <<'EOF'
import asyncio, json, ssl, websockets
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
async def m():
    async with websockets.connect("wss://localhost:8443/ws/control", ssl=ctx) as ws:
        await ws.send(json.dumps({"type":"search","mode":"fm","location":"EM38ww",
                                  "radius":300,"squelch":10,"params":{}}))
        while True:
            r=json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if r.get("type")=="results":
                f=sorted({round(s['freq_hz']/1e6,1) for s in r['stations']})
                print(f"{len(r['stations'])} stations spanning {min(f)}-{max(f)} MHz"); break
asyncio.run(m())
EOF
```

Expected: stations spanning most of 88–108 MHz, not a 500 kHz slice.

- [ ] **Step 9: Commit**

```bash
git add backend/ && git commit -m "feat(sources): select by geography, not band segment

Removes Source.center_freq_hz (dead since radiod was found to own front-end
placement) and segment_band. Sources now return every station within the
geographic radius; which of them the receiver can hear at once is the
controller's problem. Repeater band selection keeps only real bands."
```

---

### Task 2: Decide monitoring from the measured window

**Files:**
- Modify: `backend/radio_controller.py`
- Create: `tests/test_window_fit.py`

**Interfaces:**
- Consumes: Task 1's contract changes.
- Produces: `RadioController.WINDOW_FILL: float = 0.8`,
  `RadioController.DEFAULT_USABLE_BW_HZ: float = 8_000_000.0`, and
  `RadioController.fits_window(self, freqs: Iterable[float]) -> bool`, used by
  Task 3 for the `activity` flag and by `apply_stations` for the channel
  decision.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_window_fit.py
"""fits_window decides whether a whole station set can be monitored at once.

Pure logic: no radiod, no network. The rule is span <= usable_bw_hz *
WINDOW_FILL, where span is max(freq) - min(freq).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.radio_controller import RadioController


def _controller(usable_bw_hz):
    c = RadioController.__new__(RadioController)   # no radiod connection
    c.usable_bw_hz = usable_bw_hz
    return c


def test_nws_band_fits_the_narrowest_receiver():
    # 7 NWR channels span 150 kHz; the Airspy HF+ window is 660.5 kHz.
    nws = [162.400e6, 162.425e6, 162.450e6, 162.475e6, 162.500e6, 162.525e6, 162.550e6]
    assert _controller(660_500.0).fits_window(nws) is True


def test_fm_band_does_not_fit_a_narrow_receiver():
    assert _controller(660_500.0).fits_window([88.1e6, 107.9e6]) is False


def test_fm_band_does_not_fit_even_a_wide_receiver():
    # The Airspy R2 reports a 4.1 MHz window; the FM band is 20 MHz.
    assert _controller(4_100_000.0).fits_window([88.1e6, 107.9e6]) is False


def test_two_m_fits_the_r2_but_not_the_hf_plus():
    two_m = [144.0e6, 148.0e6]
    assert _controller(4_100_000.0).fits_window(two_m) is False   # 4.0 > 4.1*0.8
    assert _controller(660_500.0).fits_window(two_m) is False


def test_single_station_always_fits():
    assert _controller(660_500.0).fits_window([102.3e6]) is True


def test_empty_set_fits():
    assert _controller(660_500.0).fits_window([]) is True


def test_fill_margin_is_applied():
    # Exactly the raw window does NOT fit; 80% of it does.
    assert _controller(1_000_000.0).fits_window([100.0e6, 101.0e6]) is False
    assert _controller(1_000_000.0).fits_window([100.0e6, 100.8e6]) is True


def test_unknown_window_falls_back_to_the_default():
    c = _controller(None)
    assert c.fits_window([100.0e6, 106.0e6]) is True     # 6 MHz <= 8 MHz * 0.8
    assert c.fits_window([88.1e6, 107.9e6]) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/pytest tests/test_window_fit.py -v`
Expected: FAIL — `AttributeError: 'RadioController' object has no attribute 'fits_window'`

- [ ] **Step 3: Implement `fits_window`**

In `backend/radio_controller.py`, add the constants next to
`SQUELCH_WIDE_OPEN_DB` and the method after `probe_frontend`:

```python
    # Fraction of the receiver's usable window a monitored station set may
    # span. radiod parks channels near the window edge by design, and the wfm
    # demodulator cannot demodulate there (see CLAUDE.md), so leaving margin
    # is not cosmetic.
    WINDOW_FILL = 0.8

    # Assumed window when radiod does not report FE_LOW_EDGE/FE_HIGH_EDGE.
    # Roughly an Airspy R2 at 10 Msps.
    DEFAULT_USABLE_BW_HZ = 8_000_000.0

    def fits_window(self, freqs) -> bool:
        """True if every one of `freqs` can be monitored simultaneously.

        A monitored station needs a radiod channel inside the front end's
        window, and there is one window shared by every channel. So this is
        what decides whether the activity map can mean anything: with the
        whole set inside the window each channel reports real SNR, and
        without it the app monitors nothing and serves the station list as a
        directory instead.
        """
        values = [float(f) for f in freqs]
        if len(values) < 2:
            return True
        span = max(values) - min(values)
        window = self.usable_bw_hz or self.DEFAULT_USABLE_BW_HZ
        return span <= window * self.WINDOW_FILL
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/pytest tests/test_window_fit.py -v`
Expected: 8 passed.

- [ ] **Step 5: Use the decision in `apply_stations`**

In `_apply_stations_locked`, immediately after `self.monitored_freqs = set(new_freqs)`, add:

```python
        # A set wider than the window cannot be monitored: channels outside it
        # report snr=-inf and produce no RTP. Rather than create channels that
        # cannot work, serve the list as a directory and let the audio plane
        # create the one channel a listener actually asks for.
        self.activity_available = self.fits_window(new_freqs)
        if not self.activity_available:
            logger.info(
                f"{len(new_freqs)} stations span more than the receiver's "
                f"window — directory mode, channels created on demand"
            )
            new_freqs = set()
```

Initialise `self.activity_available: bool = True` in `__init__` next to
`monitored_freqs`.

Note the existing stale-channel sweep below this point now removes the
previous mode's channels when `new_freqs` is empty, which is what we want:
switching from NWS to FM should tear the NWS channels down.

- [ ] **Step 6: Verify both modes against radiod**

```bash
./radiod-monitor.sh restart && sleep 10
timeout 200 venv/bin/python - <<'EOF'
import asyncio, json, ssl, websockets
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
async def search(mode, params):
    async with websockets.connect("wss://localhost:8443/ws/control", ssl=ctx) as ws:
        await ws.send(json.dumps({"type":"search","mode":mode,"location":"EM38ww",
                                  "radius":300,"squelch":10,"params":params}))
        while True:
            r=json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if r.get("type")=="results": return r
async def main():
    r=await search("nws",{}); print("nws stations:",len(r["stations"]))
    await asyncio.sleep(15)
    from ka9q import discover_channels
    print("  channels after nws search:",len(discover_channels("airspyhf-status.local",2.0)))
    r=await search("fm",{}); print("fm stations:",len(r["stations"]))
    await asyncio.sleep(15)
    print("  channels after fm search:",len(discover_channels("airspyhf-status.local",2.0)))
asyncio.run(main())
EOF
```

Expected: NWS creates one channel per station (7-ish). FM creates **0**, and
the log line "directory mode, channels created on demand" appears in
`backend.log`.

- [ ] **Step 7: Commit**

```bash
git add backend/radio_controller.py tests/ && git commit -m "feat(controller): monitor everything that fits, otherwise serve a directory

Compares the station set's span against the measured front-end window. NWS
(150 kHz) fits any receiver and keeps its live activity map; the FM band fits
none, so no channels are created until a listener picks a station."
```

---

### Task 3: Report which mode applied, and where the window is

**Files:**
- Modify: `backend/app.py`
- Modify: `backend/radio_controller.py`

**Interfaces:**
- Consumes: `RadioController.fits_window` (Task 2).
- Produces: the `{type: "results"}` message gains `"activity": bool`; a new
  broadcast `{"type": "window", "low_hz": float, "high_hz": float,
  "center_hz": float}` is sent to control sockets from the activity monitor.
  `RadioController.read_window(self) -> Optional[tuple]` returns
  `(low_hz, high_hz)` in absolute Hz, or `None` if it cannot be read.

- [ ] **Step 1: Add the measured-window reader**

In `backend/radio_controller.py`, after `probe_frontend`:

```python
    def read_window(self):
        """Measure where the front end currently sits, in absolute Hz.

        Returns (low_hz, high_hz) or None. Measured rather than derived from
        what this app believes it set: radiod re-places the front end on its
        own terms, and the anchor mechanism in focus_on() exists precisely
        because the obvious model of its placement was wrong.
        """
        if not self.control or not self.active_channels:
            return None
        if self.fe_low_edge_hz is None or self.fe_high_edge_hz is None:
            return None
        ssrc = next(iter(self.active_channels))
        try:
            status = self.control.poll_status(ssrc, timeout=2.0)
        except Exception as e:
            logger.debug(f"read_window: {e}")
            return None
        fe = getattr(status, "frontend", None) or status
        first_lo = getattr(fe, "first_lo", None)
        if not first_lo:
            return None
        return (first_lo + self.fe_low_edge_hz, first_lo + self.fe_high_edge_hz)
```

- [ ] **Step 2: Send the `activity` flag with the results**

In `backend/app.py`, in the search handler, immediately before the
`await websocket.send_json({...})` that carries `"type": "results"`, add:

```python
    # Whether the activity map can mean anything for this set. Computed here
    # rather than read back from apply_stations because results are sent
    # first: the converge task runs in the background.
    activity_available = controller.fits_window(s.freq_hz for s in stations)
```

and add `"activity": activity_available,` to that message's dict.

- [ ] **Step 3: Broadcast the window from the activity monitor**

In `activity_monitor()` in `backend/app.py`, after the per-channel activity
loop and still inside the `try:`, add:

```python
            window = await asyncio.to_thread(controller.read_window)
            if window:
                low_hz, high_hz = window
                wmsg = {
                    "type": "window",
                    "low_hz": low_hz,
                    "high_hz": high_hz,
                    "center_hz": (low_hz + high_hz) / 2.0,
                }
                for ws in list(active_websockets):
                    try:
                        await ws.send_json(wmsg)
                    except Exception:
                        pass
```

- [ ] **Step 4: Verify both messages on the wire**

```bash
./radiod-monitor.sh restart && sleep 10
timeout 200 venv/bin/python - <<'EOF'
import asyncio, json, ssl, websockets
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
async def run(mode):
    async with websockets.connect("wss://localhost:8443/ws/control", ssl=ctx) as ws:
        await ws.send(json.dumps({"type":"search","mode":mode,"location":"EM38ww",
                                  "radius":300,"squelch":10,"params":{}}))
        seen={}
        end=asyncio.get_event_loop().time()+40
        while asyncio.get_event_loop().time()<end:
            try: r=json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError: continue
            t=r.get("type")
            if t=="results": seen["activity"]=r.get("activity")
            if t=="window":
                seen["window"]=f"{r['low_hz']/1e6:.3f}-{r['high_hz']/1e6:.3f} MHz"; break
        print(f"{mode}: activity={seen.get('activity')} window={seen.get('window')}")
async def main():
    await run("nws"); await run("fm")
asyncio.run(main())
EOF
```

Expected: `nws: activity=True window=<about 660 kHz wide>` and
`fm: activity=False window=...` (the window may be absent for fm until a
station is played, since directory mode creates no channels — that is
correct, and the strip omits the box).

- [ ] **Step 5: Commit**

```bash
git add backend/ && git commit -m "feat(api): report activity availability and the measured window

results carries activity:bool so the UI can omit a legend for green markers
that will never appear; the activity monitor broadcasts the front end's real
position for the frequency strip."
```

---

### Task 4: Frontend — labels, no band control for FM, honour `activity`

**Files:**
- Modify: `frontend/app.js`

**Interfaces:**
- Consumes: `activity` on results (Task 3).
- Produces: `currentActivityAvailable` module-level flag read by Task 5.

- [ ] **Step 1: Store the flag and label the markers**

In `handleSearchResults(data)` in `frontend/app.js`, after
`currentAudioChannels = data.audio_channels || 1;` add:

```javascript
    // False when the station set is wider than the receiver's window: no
    // channels exist, so no marker will ever go green. Say nothing rather
    // than promising activity that cannot arrive.
    currentActivityAvailable = data.activity !== false;
```

Declare `let currentActivityAvailable = true;` next to `let currentAudioChannels = 1;`.

In the same function, after `marker.bindPopup(buildPopup(st));` add:

```javascript
        marker.bindTooltip(
            `${st.name} ${(st.freq_hz / 1e6).toFixed(1)}`,
            { permanent: true, direction: 'right', className: 'station-label' }
        );
```

- [ ] **Step 2: Hide the band control when a source has none**

Find where the band dropdown is populated from `controls.bandSegments` and
ensure the container is hidden when the key is absent. Add to that block:

```javascript
    const hasBands = !!(controls && controls.bandSegments && controls.bandSegments.length);
    if (bandControlEl) bandControlEl.style.display = hasBands ? '' : 'none';
```

using whatever element variable already wraps the dropdown; if none exists,
add `const bandControlEl = document.getElementById('band-control');` and give
that id to the dropdown's wrapper element in `frontend/index.html`.

- [ ] **Step 3: Verify in the browser**

```bash
./radiod-monitor.sh restart && sleep 10
```

Open https://localhost:8443/ and check: selecting Commercial FM hides the band
dropdown and shows stations across the whole band with permanent labels;
selecting NOAA Weather Radio also hides it; VHF/UHF Repeaters shows exactly
three bands.

- [ ] **Step 4: Commit**

```bash
git add frontend/ && git commit -m "feat(ui): label stations on the map, drop the band control where it has no meaning"
```

---

### Task 5: Frontend — the frequency strip

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/app.js`

**Interfaces:**
- Consumes: `{type: "window"}` (Task 3), `currentActivityAvailable` (Task 4),
  `stationsData`, `listenToStation(freqHz, name)` (existing).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Add the container**

In `frontend/index.html`, immediately after the map container element, add:

```html
    <div id="freq-strip" class="freq-strip" aria-label="Frequency view">
      <canvas id="freq-strip-canvas" height="64"></canvas>
      <div id="freq-strip-caption" class="freq-strip-caption"></div>
    </div>
```

- [ ] **Step 2: Draw the strip**

Add to `frontend/app.js`:

```javascript
// ---------------------------------------------------------------------------
// Frequency strip — stations by frequency, and where the receiver's window is.
// The map answers "who is near me"; this answers "what can the radio hear at
// once". The window box is drawn only from a measured position (see the
// "window" message); it is never inferred, because radiod places the front end
// on its own terms.
// ---------------------------------------------------------------------------
let currentWindow = null;   // {low_hz, high_hz} or null

function stripBounds() {
    if (!stationsData.length) return null;
    const fs = stationsData.map(s => s.freq_hz);
    let lo = Math.min(...fs), hi = Math.max(...fs);
    const pad = Math.max((hi - lo) * 0.05, 200e3);
    return { lo: lo - pad, hi: hi + pad };
}

function drawFreqStrip() {
    const canvas = document.getElementById('freq-strip-canvas');
    const caption = document.getElementById('freq-strip-caption');
    if (!canvas) return;
    const b = stripBounds();
    canvas.width = canvas.parentElement.clientWidth;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!b) { caption.textContent = ''; return; }
    const x = hz => ((hz - b.lo) / (b.hi - b.lo)) * canvas.width;

    // Window box first, so ticks draw over it.
    if (currentWindow) {
        const x0 = Math.max(0, x(currentWindow.low_hz));
        const x1 = Math.min(canvas.width, x(currentWindow.high_hz));
        if (x1 > x0) {
            ctx.fillStyle = 'rgba(59,130,246,0.18)';
            ctx.fillRect(x0, 0, x1 - x0, canvas.height - 18);
            ctx.strokeStyle = 'rgba(59,130,246,0.7)';
            ctx.strokeRect(x0, 0, x1 - x0, canvas.height - 18);
        }
    }

    ctx.strokeStyle = '#334155';
    ctx.beginPath();
    ctx.moveTo(0, canvas.height - 18);
    ctx.lineTo(canvas.width, canvas.height - 18);
    ctx.stroke();

    for (const st of stationsData) {
        const sx = x(st.freq_hz);
        const inWindow = currentWindow &&
            st.freq_hz >= currentWindow.low_hz && st.freq_hz <= currentWindow.high_hz;
        ctx.strokeStyle = inWindow ? '#22c55e' : '#94a3b8';
        ctx.lineWidth = inWindow ? 2 : 1;
        ctx.beginPath();
        ctx.moveTo(sx, 6);
        ctx.lineTo(sx, canvas.height - 18);
        ctx.stroke();
    }

    ctx.fillStyle = '#94a3b8';
    ctx.font = '10px sans-serif';
    ctx.fillText((b.lo / 1e6).toFixed(1), 2, canvas.height - 4);
    const hiLabel = (b.hi / 1e6).toFixed(1);
    ctx.fillText(hiLabel, canvas.width - ctx.measureText(hiLabel).width - 2, canvas.height - 4);

    caption.textContent = currentWindow
        ? `receiver window ${((currentWindow.high_hz - currentWindow.low_hz) / 1e3).toFixed(0)} kHz`
        : (currentActivityAvailable ? '' : 'wider than the receiver — pick a station to listen');
}

function freqStripClick(ev) {
    const b = stripBounds();
    if (!b || !stationsData.length) return;
    const canvas = document.getElementById('freq-strip-canvas');
    const rect = canvas.getBoundingClientRect();
    const hz = b.lo + ((ev.clientX - rect.left) / rect.width) * (b.hi - b.lo);
    let best = stationsData[0];
    for (const st of stationsData) {
        if (Math.abs(st.freq_hz - hz) < Math.abs(best.freq_hz - hz)) best = st;
    }
    listenToStation(best.freq_hz, best.name);
}
```

- [ ] **Step 3: Wire it up**

At the end of `handleSearchResults(data)`, add `drawFreqStrip();`.

In the control WebSocket `onmessage` handler, alongside the existing
`activity` case, add:

```javascript
        } else if (msg.type === 'window') {
            currentWindow = { low_hz: msg.low_hz, high_hz: msg.high_hz };
            drawFreqStrip();
```

At the end of the file's event wiring, add:

```javascript
document.getElementById('freq-strip-canvas')
        .addEventListener('click', freqStripClick);
window.addEventListener('resize', drawFreqStrip);
```

- [ ] **Step 4: Style it**

In `frontend/index.html`'s stylesheet (or the project's CSS file if one
exists), add:

```css
.freq-strip { width: 100%; padding: 4px 0; }
.freq-strip canvas { width: 100%; display: block; cursor: pointer; }
.freq-strip-caption { font-size: 11px; color: #94a3b8; text-align: right; }
.station-label { background: rgba(15,23,42,0.75); border: 0; color: #e2e8f0;
                 font-size: 10px; padding: 1px 3px; }
```

- [ ] **Step 5: Verify in the browser**

```bash
./radiod-monitor.sh restart && sleep 10
```

Open https://localhost:8443/. Search NWS: the strip shows 7 ticks and the
window box covers all of them (the window is wider than the band). Search
Commercial FM: ticks span the band, no box until you click one. Click a tick:
that station plays and the box appears centred on it. Click a different tick:
the box moves. Confirm the audio is the station you picked.

- [ ] **Step 6: Commit**

```bash
git add frontend/ && git commit -m "feat(ui): frequency strip showing stations and the measured receiver window

The map answers who is near you; the strip answers what the radio can hear at
once. The window box is drawn only from radiod's reported position."
```

---

### Task 6: Update the architecture documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace the band-segment section**

Replace the whole "### Band segments are a property of the radio, not the
source" section with:

```markdown
### The receiver's window is the app's problem, not the user's

A `Source` returns every station within the geographic radius. It does **not**
subdivide its band to fit the receiver — that was `segment_band()`, removed on
2026-08-19 after it produced 38 FM segments and 71 repeater segments on the
Airspy HF+'s 660.5 kHz window, making the user solve the radio's problem before
reaching the station they wanted.

`RadioController.fits_window()` decides instead, from the measured window
(`probe_frontend`, `FE_LOW_EDGE`/`FE_HIGH_EDGE`):

- **Set fits** (`span <= usable_bw_hz * WINDOW_FILL`) — a channel per station,
  live SNR, markers go green. NWS always lands here: 7 channels in 150 kHz fit
  any receiver this app meets.
- **Set does not fit** — no channels; the list is a directory, and a channel is
  created only when a listener picks a station. The FM band is 20 MHz and fits
  no window, so activity across it is simply not observable — an accepted
  consequence, reported to the UI as `activity: false` rather than hidden.

Measured windows: **660.5 kHz** (Airspy HF+ @ 768k), **4.1 MHz** (Airspy R2 @
10 Msps, `isreal=True`, window −4700..−600 kHz — *not* centred on the LO), and
the whole HF spectrum on a direct-sampling RX888.

The frequency strip in the UI draws each station at its frequency and the
window at its **measured** position, broadcast as `{type: "window"}` by the
activity monitor. Never infer that position from what the app believes it set:
`focus_on()`'s anchor mechanism exists precisely because radiod's real
placement differed from the obvious model.
```

- [ ] **Step 2: Fix the route documentation**

In the HTTP/WebSocket routes block, change the results line to:

```
  ← {type: "results", mode, lat, lon, activity, stations: [Station]}
  ← {type: "window", low_hz, high_hz, center_hz}
```

- [ ] **Step 3: Remove `center_freq_hz` from the Source contract description**

In the "Source plugin contract" section, delete the
`center_freq_hz(params) -> float` bullet and change the `controls_schema`
bullet to note that `bandSegments` names real bands only.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md && git commit -m "docs: the receiver's window is the app's problem, not the user's"
```

---

## Verification

After all tasks, confirm end to end:

```bash
venv/bin/pytest tests/ -v
./radiod-monitor.sh restart && sleep 10
```

1. NWS → 7 stations, `activity: true`, all channels created, markers green,
   strip box covers the whole band. Audio at voice/hiss ≈ 14.
2. FM → full 88–108 list, no band control, `activity: false`, 0 channels
   before a click; clicking a station plays it and the strip box appears on it.
3. `./radiod-monitor.sh stop` → "Released N channel(s)", radiod purges to 0.
