# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## General Workflow Orchestration in Any Project

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## Specific to this Project

This is a Python web application that monitors radiod status. It uses uvicorn to serve a FastAPI backend with a simple frontend.

## Commands

```bash
./radiod-monitor.sh start|stop|restart|status
```

- Serves on port **8443**; HTTPS if `certs/key.pem` + `certs/cert.pem` exist, otherwise plain HTTP.
- PID file: `.app.pid`. Logs: `backend.log` (append-only).
- `start` calls `ensure_certs` which auto-generates a self-signed cert (valid 10 years, CN=`$(hostname -f)`, SANs for FQDN/localhost/127.0.0.1) on first run.
- `uvicorn` runs with `reload=True`, so edits under `backend/` hot-reload.
- Initial radiod host comes from `RADIOD_HOST` env var (default `airspyhf-status.local`); UI dropdown can override at runtime.
- `start` rotates `backend.log` past 32 MB to `backend.log.1`.

Dependencies live in `venv/`. `ka9q-python` is typically installed as an editable local checkout from a sibling directory:

```bash
venv/bin/pip install -e ../ka9q-python
venv/bin/pip install -e .
```

There is no test suite.

## Architecture

This is the unified successor to the sibling projects `../nws-monitor` and `../repeater-monitor`. Both of those have been aligned to a common pipeline over time; this project completes the merge by introducing a **Source** plugin interface so the shared pipeline (control WebSocket, channel lifecycle, activity monitor, audio streamer) is agnostic to the specific set of frequencies being monitored. If you're tempted to do anything in this repo that doesn't fit the shared pipeline, back up and check whether you should be extending a Source instead.

### Source plugin contract — the load-bearing abstraction

[backend/sources/base.py](backend/sources/base.py) defines:

- **`Station`** — one monitorable transmitter: `id`, `name`, `freq_hz`, `lat`, `lon`, `distance_km`, `extra: dict`. The `extra` dict is source-specific popup content that the frontend renders verbatim (channel number for NWS, offset/tone for repeaters, etc).
- **`Source`** — base class with two methods subclasses override:
  - `controls_schema() -> dict` — JSON-serializable description of per-source UI controls. Currently supports `bandSegments: [{value, label}]` for real amateur bands (2m, 1.25m, 70cm), never receiver-sized slices.
  - `list_stations(lat, lon, radius_km, params) -> list[Station]`.

Registry lives in [backend/sources/__init__.py](backend/sources/__init__.py); adding a new source is one import + one entry in `_SOURCES`. Three sources ship:

- **`NwsSource`** ([backend/sources/nws.py](backend/sources/nws.py)) — loads `data/nws_stations.json`, 7-channel NWR band centered on 162.475 MHz, no per-source controls, preset `nfm`. Falls back to the 7 standard frequencies at the user's exact location if no station is within range, so the audio pipeline is still exercisable.
- **`RepeaterSource`** ([backend/sources/repeaters.py](backend/sources/repeaters.py)) — loads `data/repeaters*.kml` (RepeaterBook export, newest mtime wins), filters by distance and by a real amateur band (2m / 1.25m / 70cm), preset `nfm`. Whether those stations can be monitored simultaneously is `RadioController.fits_window()`'s decision (see above). Parses callsign, downlink frequency, offset sign, and PL tone from the KML description CDATA. Warns at load time if the KML is >180 days old.
- **`FmSource`** ([backend/sources/fm.py](backend/sources/fm.py)) — loads `data/fm_stations.json` (compiled by [scripts/fetch_fm_stations.py](scripts/fetch_fm_stations.py) from the FCC CDBS public files), filters by distance across the whole 88–108 MHz band, preset `wfm`, `audio_channels=2`. Whether those stations can be monitored simultaneously is `RadioController.fits_window()`'s decision (see above). The `wfm` preset in [ka9q-radio/share/presets.conf](../ka9q-radio/share/presets.conf) forces a 384 kHz downconverter and 48 kHz output with 75 µs North American de-emphasis. **It does not force stereo** — both the repo copy and the installed `/usr/local/share/ka9q-radio/presets.conf` set `mono = yes`, which is why `audio_channels` is only a hint and the real count comes off the wire.

**About the `audio_channels` attribute.** Declared on `Source` and reported in `GET /api/sources` and each `{type: "results"}` message, it is a *hint* only — it seeds `AudioSession` before any audio arrives.

It is not what configures the decoder, because a source cannot know the answer: the `wfm` preset ships `mono = yes` in some ka9q-radio installs and stereo in others, and `ChannelInfo` carries no channel count. A wrong `numberOfChannels` makes WebCodecs throw on the first packet, so the value has to be ground truth.

The authority is the stream itself. Every Opus packet states its channel count in bit 2 of its TOC byte (RFC 6716 §3.1); `backend/vfo.py`'s `opus_channels()` reads it from the first frame, latched onto `Vfo.channels`. The audio WebSocket sends `{"type": "tuned", "freq_hz": N, "channels": N}` ahead of any binary frame for the newly-tuned station. The browser configures `AudioDecoder` on that message and ignores frames until it arrives (Opus frames are self-contained, so the few dropped cost a few ms). A listener joining a stream already in progress is handed the stored value on connect.

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
`FrontEndWindow.centre_on()`'s anchor mechanism (`backend/window.py`) exists
precisely because radiod's real placement differed from the obvious model —
see the anchor-channel notes further down this file.

**Other clients compete.** The front end is global to the radiod instance.
`ka9q-web` against the same radiod requests its own window and drags the
front end away; it must not run alongside this app on one receiver.

### Shared pipeline

Identical in shape to the aligned nws-monitor/repeater-monitor, just generalized:

1. **Control plane — [backend/radio_controller.py](backend/radio_controller.py).** On a `search` message, the active `Source` returns a station list; `apply_stations()` converges the channel set based on what fits in the receiver's window. Sensor channels live on a single stable multicast destination derived from `generate_multicast_ip("radiod-monitor")` — shared across all sources, so mode switching is a delta on the channel set rather than a teardown. The VFO and the anchor live on two *other* groups; see "Three multicast groups" below, and do not move them onto this one. SSRCs are deterministic (hash of frequency, preset, sample_rate, encoding, destination, agc, gain=0.0, and the radiod identity) so they survive server restarts and can be independently rediscovered by the audio streamer. Per-user squelch is applied *after* ensure_channel so it doesn't perturb the hash.

   **Convergence is a diff, and that is load-bearing.** Because the SSRC is a deterministic hash of exactly the parameters that define a channel, the wanted SSRC set is computable *before* talking to radiod — `allocate_ssrc()` with the same arguments `ensure_channel()` uses internally, including `radiod_host=control.status_address`. An existing channel on our destination whose SSRC is in that set is correct by construction, so it is left untouched; only SSRCs outside the set are removed, and only missing ones are created.

   The earlier implementation wiped every channel on the destination and rebuilt, which forced it to re-create SSRCs that radiod was still asynchronously tearing down — so it had to poll up to 10 s for `freq=0` zombies to disappear on *every* search (the "Timed out waiting for radiod to purge zombie channels" warning fired routinely). With a diff the removed and created sets are disjoint by definition, so there is no race to wait on: removals are fire-and-forget and the poll is gone. Time-to-first-audio went from 3.7 s cold / 7–13 s on a repeat search to 1.2 s / ~0.06 s, and a search no longer cuts off audio the user is listening to.

   Reuse is gated on the discovered `frequency` **and** `encoding`, not the SSRC alone — the frequency guards against a zombie mid-teardown still answering on that SSRC, and the encoding against radiod having reset the channel to the preset default. Both fields come free from the discovery already performed. If you add a parameter that affects what radiod serves, it must either be in the SSRC hash or checked here, or a stale channel will be silently reused.

2. **Activity monitor — [backend/app.py](backend/app.py) `activity_monitor()`.** Background task polling `discover_channels(radiod_host)` every 2 s, reading `ChannelInfo.snr`, and broadcasting `{type: "activity", freq, isActive, snr}` to all connected control-WebSocket clients. `isActive` is `snr > 3.0 dB`. Looks up channels by SSRC, falls back to frequency match within 100 Hz. This is what flips map markers green.

3. **Audio plane — [backend/vfo.py](backend/vfo.py), one retunable VFO.** There is exactly one radiod channel a listener ever hears, `Vfo`, owned by `RadioController` and shared by every browser listener via `asyncio.Queue` fan-out. It is created once and **retuned, never recreated**, across every station switch and every mode switch — that rule is load-bearing, not stylistic. Destroying a radiod channel starts a ~20 s asynchronous purge (`radio.c` reaps a channel only once its frequency is zero, after `Channel_idle_timeout`); re-creating the same SSRC inside that window hands back a channel radiod is still tearing down, which never produces RTP. Every "switching is broken" symptom this project hit before the VFO traces back to that cycle.

   **The app never computes the SSRC.** `create_channel()` allocates one and `Vfo` stores the opaque integer, handing it back to the library on every later command — the app's vocabulary is frequency, preset, and sample rate, never transport identity. The stream is a `RadiodStream` bound to that stored SSRC, not a `ManagedStream` bound to a frequency: only the former keeps delivering across a retune, because `ManagedStream` re-derives its own SSRC from frequency and preset on every restore and so cannot follow a channel whose frequency it didn't choose.

   **The window is centred before the frequency is set.** `Vfo._tune_once` calls `FrontEndWindow.centre_on()` (`backend/window.py`) before `set_frequency()`, because a demodulator that starts parked at the window edge does not recover once the window later moves onto it — see the anchor-channel notes further down this file. If no RTP arrives within `TUNE_SETTLE_SEC` (1.5 s), `Vfo.tune()` retries by reissuing the preset command — which makes radiod restart the demod in place, still on the same SSRC — up to `MAX_TUNE_ATTEMPTS` (2) before giving up and broadcasting `{"type": "nosignal"}`.

   radiod is configured with `Encoding.OPUS`, `sample_rate=48000`, `samples_per_packet=960` (20 ms), `deliver_interval_packets=1`, and **`raw_payloads=True`** — ka9q-python's transport mode for framed encodings, where `on_samples` receives a `List[bytes]` of undecoded RTP payloads with the resequencer bypassed (a codec frame is opaque: it can be neither concatenated nor zero-filled, and gap concealment belongs to the decoder). With `deliver_interval_packets=1` that is exactly one encoded frame per call.

   **The Opus grant must be asserted, not assumed.** Creating or retuning a channel is not sufficient: radiod applies the preset's default output encoding (s16be) and honours OPUS only from a follow-up `OUTPUT_ENCODING` command, and it must be asserted *before* the receiver starts — assert it afterwards and the first packets on the socket are PCM, forwarded to the browser labelled as Opus. `Vfo._tune_once` calls `set_output_encoding(ssrc, Encoding.OPUS)` on every tune for exactly this reason. As a backstop, `Vfo._broadcast` drops any payload over 1275 bytes — the RFC 6716 maximum for a single Opus frame — and logs it once, so a lost grant is loud instead of silent. Each frame is shipped to the browser as a single WebSocket binary message on `WS /ws/audio`. No Ogg container, no tagging, no server-side decoding — WebSocket/TCP preserves frame order and the browser uses WebCodecs `AudioDecoder`.

   **WebCodecs requires a secure context** (HTTPS or `localhost`) — the shell script auto-generates a self-signed cert to satisfy this.

### Three multicast groups, and why they must stay three

| group | attribute | slug | holds |
|---|---|---|---|
| sensors | `controller.destination` | `radiod-monitor` | the activity-map channels, one per station, never listened to |
| VFO | `controller.vfo_destination` | `radiod-monitor-vfo` | the one channel a listener actually hears |
| anchor | `controller.anchor_destination` | `radiod-monitor-anchor` | the squelched-shut channel that positions the front end |

`destination` is one of the inputs to `allocate_ssrc`. So is frequency, preset, sample rate, encoding, gain, agc, and the radiod identity — and `create_channel()` auto-allocates from **exactly** the argument list `_apply_stations_locked` uses to compute the sensor set. Put the VFO on the sensor group and tune it to a station that is also monitored (NWS mode, always) and the two SSRCs are *the same integer*: one radiod channel doing both jobs. Measured, 162.475 MHz / `nfm` / 48 kHz / OPUS:

```
sensor group 239.123.74.180 -> 551094885
VFO    group 239.161.73.136 -> 1767890759
same group for both         -> 551094885 == 551094885   COLLIDE
```

Nothing about that fails loudly. The audio keeps playing while the search re-applies the user's squelch over the VFO's held-open one, retunes the channel the user is listening through back to the sensor's frequency, and reports one station's SNR on another station's marker. It also defeated the adopt-an-existing-channel scan in `Vfo._ensure_channel_exists`, which took `existing[0]` off the group and so could adopt an arbitrary sensor — or, in the first seconds after startup, `probe()`'s throwaway channel that radiod was still purging, which is a dead channel producing no audio.

Separate groups make all of that impossible by construction, and make the VFO's and the anchor's exemption from the stale-channel sweep **structural**: neither can appear in the swept set at all. There is deliberately no `ssrc == vfo.ssrc` check in `_apply_stations_locked` — the one that used to be there never ran in the case it was written for (the aliased SSRC was in `desired`), which is precisely the kind of dead guard that has misled work in this repo before. If the VFO is ever swept, the bug is that it moved back onto the sensor group.

What still has to be kept true across `RadioController.apply_stations()` and `Vfo.tune()`: both must assert `OUTPUT_ENCODING = OPUS` after creating or retuning a channel, or radiod serves the preset default on whichever path skipped it. `preset` and `sample_rate` cannot drift — `websocket_audio`'s command loop reads them off `controller.preset`/`controller.sample_rate` on every `vfo.tune()` call, and `_handle_search` publishes them synchronously before the results message reaches the browser so an eager Listen click cannot land in the gap. `audio_channels` is not part of this coupling: it is a display hint, and the decoder is configured from the first Opus frame's TOC byte instead.

### Host switching

Radiod instance discovery uses `ka9q.discovery.discover_radiod_services()`, which shells out to `avahi-browse -t _ka9q-ctl._udp`. The `GET /api/radiod/discover` endpoint returns `{hosts: [{name, address}], current}`. `POST /api/radiod/select` validates the hostname against `^[a-zA-Z0-9][a-zA-Z0-9.\-]*$` (matching SWL-ka9q's regex to prevent shell metacharacters even though we never shell out), stops all active audio streams, tears down `RadioController`, reconnects to the new host, and returns. The frontend re-runs the search automatically after a successful switch.

### HTTP / WebSocket routes

```
GET  /api/radiod/discover              → {hosts: [{name, address}], current}
POST /api/radiod/select  {host}        → {ok, host, changed}
GET  /api/sources                      → {sources: [{key, display_name, preset, controls}]}
WS   /ws/control                       ← JSON messages
  → {type: "search", mode, location, radius, squelch, params}
  ← {type: "results", mode, lat, lon, activity, stations: [Station]}
  ← {type: "window", low_hz, high_hz, center_hz}
  ← {type: "activity", freq, isActive, snr}
  ← {type: "error", message}
WS   /ws/audio
  → {"tune": <freq_hz>}
  ← {"type": "tuned",    "freq_hz": N, "channels": 1|2}
  ← {"type": "nosignal", "freq_hz": N, "reason": "..."}   (reason optional)
  ← {"type": "error",    "message": "..."}
  ← one binary message per Opus frame
```

`nosignal` carries a `reason` whenever one is known — radiod refused or is
unreachable, the window could not be centred, or the station is not in the
current search (a mode switch moves the front end out from under a listener).
A bare `nosignal` means the tune itself succeeded and nothing came back. The
browser appends the reason to the status line; the distinction is the whole
point, so do not collapse them.

Control dicts share the 200-slot listener queue with Opus frames, and they are
not interchangeable: the browser ignores every frame until a `tuned`
reconfigures its decoder, so a dropped `tuned` is permanent silence while a
dropped frame costs 20 ms. `vfo.put_control()` therefore displaces a queued
item rather than dropping the message, and every control-message enqueue in
the backend goes through it.

## Known quirks

- **Markers are keyed by `freq_hz`** in the frontend (`markers[st.freq_hz]`). Two stations at identical downlink frequencies but different locations collide — last one wins for the icon and activity updates. Acceptable today, worth knowing.
- **First-run radiod failure is non-fatal.** If the default radiod host is unreachable at startup, the app logs a warning and still comes up; the user can pick a different host from the dropdown.
- **`data/repeaters*.kml`** is shipped as a starter. The KML age warning fires at 180 days. There is no built-in refresh — the user downloads a new file from RepeaterBook and drops it into `data/`.
- **Browser autoplay gating.** The "Listen" click itself satisfies the user-gesture requirement, and the `AudioSession` class resumes the `AudioContext` immediately. If it's still suspended afterwards, the "Unmute / Resume Audio" button appears and the user clicks it.


## radiod on this host (verified 2026-08-18)

`radiod@airspyhf-generic` runs **on the same machine** as radiod-monitor and is
configured `ttl = 0`, so it emits RTP **only on the loopback interface**. That
is a supported configuration for a local browser and needs no change — but it
puts a hard requirement on the client: the multicast join must cover `lo`.

`ka9q-python` ≤ 3.13 joined with `imr_interface = INADDR_ANY`, which the kernel
resolves through the routing table to the default interface (`enp3s0` here), so
it never saw the stream — the socket was healthy and simply silent. This was the
cause of "no audio in the browser". Current `RadiodStream._create_socket()`
binds the group and explicitly joins loopback for exactly this case, which is
why **the ka9q-python floor is >= 3.24.0**. Do not lower it.

Measured, same channel, same moment, before the upgrade:

| multicast join      | frames in 6 s |
|---------------------|---------------|
| `127.0.0.1`         | 401           |
| `INADDR_ANY`        | 0             |

Hardware note: both radios are now attached. `radiod@airspy-generic` drives the
wideband Airspy R2 (10 Msps, **4.1 MHz** usable window, `isreal=True`, window
reported as −4700..−600 kHz — not centred on the LO, which is why
`backend/window.py`'s anchor placement is computed from the window's
half-width rather than assuming symmetry — see fact below). As of 2026-08-19 the
R2 hears nothing on VHF — NWR reads −9 to −22 dB against +17.6 dB on the HF+ —
so it likely has no antenna connected. The HF+ is the receiver with the working
VHF feed, at a 660.5 kHz window.

The HF+ **does** tune VHF: measured clean tuning at 146 MHz and 36 dB SNR at
162 MHz (NWR audio plays). What it cannot do is cover much at once — a 660.5 kHz
window — which is why band segments are now cut to the radio rather than fixed.
Broadcast FM works, but only because the app centres the window on the station.
Getting there took three fixes and disproved three earlier guesses in this file
(front-end bandwidth, antenna, then "it works" on the strength of frame counts).

1. **ka9q-python sent `DEMOD_TYPE` contradicting the preset.** It derived the
   demodulator from a five-name allowlist and sent it right after `PRESET` in
   the same packet, so `wfm` got `FM_DEMOD` and radiod ran the *narrowband* FM
   demod behind a ±110 kHz filter — no output, `snr=-inf`, no error. Fixed in
   ka9q-python 3.25.1.

2. **`Source.snr_squelch = False` cannot be honoured as written.** `wfm.c` sets
   `chan->squelch.snr_enable = true` unconditionally at demod start, so
   `_squelch_args()` holds the squelch open with a −20 dB threshold instead.

3. **radiod parks a channel at the window edge, and wfm cannot demodulate
   there.** `set_freq()` retunes only far enough to bring a channel's *filter*
   inside, leaving it `filter.max_IF + fudge` — 111 kHz for wfm — from the
   edge. But `wfm.c` demodulates through a 384 kHz composite path needing
   ±192 kHz around the channel, so 81 kHz of it falls outside the window. The
   demod then half-works: clear voice in brief fragments, `snr` flipping to
   `-inf`. The 111 kHz margin is identical on a 660 kHz HF+ window and a
   4.1 MHz R2 window, so no receiver is wide enough to escape it.

   `backend/window.py`'s `FrontEndWindow` fixes this with an **anchor
   channel** — see its module docstring and `choose_anchor_frequency()` for
   the mechanics, and `FrontEndWindow.centre_on()`/`release()` for the
   lifecycle. Placing a narrow `am` channel outside the window makes radiod's
   edge-parking of *that* channel leave the LO on our station; every other
   channel then recalculates its IF against the new LO, so the station sits at
   IF ≈ 0 with the whole composite inside the window. Measured at 102.300 MHz:
   IF +219.2 kHz / `snr=-inf` / ~17 frames per 10 s before, IF +0.0 kHz /
   `snr` 6.5 / 500 frames per 10 s after.

   Three facts belong here because each was expensive to learn and none is
   visible from the code alone:

   - **The anchor offset is half the window's WIDTH, not either edge**:
     `target ± ((high_edge - low_edge)/2 - margin)`. The edge-based form
     (`target + (high_edge - margin)`) is arithmetically identical only when
     the window happens to be symmetric about the LO — which is why it
     worked on the Airspy HF+ and hid the bug for so long — but on the R2,
     whose window sits at −4700..−600 kHz, it places the anchor *inside* the
     window, where radiod ignores it. Anchor placement fails **silently**:
     no error, just a station that never comes up. Which side of the target
     the anchor actually lands on depends on where the LO currently sits;
     `choose_anchor_frequency()` computes both candidates and returns
     whichever is genuinely outside the window.
   - **The anchor lives on its own multicast destination**
     (`radiod-monitor-anchor`), and so does the VFO (`radiod-monitor-vfo`).
     Sharing one destination let the VFO's adopt-an-existing-channel scan
     mistake the anchor for the VFO after a restart, which then "centred"
     the window on wherever the anchor happened to be instead of the station
     the user asked for — and, worse, aliased the VFO's SSRC onto a sensor's.
     `probe()`'s throwaway channel goes on the anchor group for the same
     reason. See "Three multicast groups" above; neither the anchor nor the
     VFO needs an exemption from the stale-channel sweep, because neither is
     ever in the set it sweeps.
   - **A channel radiod parks at the window edge cannot be rescued by
     moving the window onto it afterwards** — the demodulator does not
     recover. `Vfo._tune_once` therefore runs a fixed five-step order and
     every step of it is load-bearing: `centre_on()`, *then*
     `_ensure_channel_exists()` (so a channel radiod creates is created
     inside an already-centred window — creating it first left a fresh `wfm`
     demod parked at the edge, rescued only by accident when the 1.5 s retry
     re-asserted the preset), *then* `set_frequency()`, *then* `set_preset()`
     if the preset changed or this is a retry, *then* `set_output_encoding()`
     and the squelch. `set_preset` restarts the demodulator and `wfm.c`
     re-runs `set_freq` at demod start, so issuing it before `set_frequency`
     re-places the channel from the *previous* station's frequency and drags
     the LO back off the target.

   The anchor is squelched shut, created once and then retuned (never
   recreated per station), and dropped by `FrontEndWindow.release()` when
   the last audio listener disconnects. If radiod is ever fixed to reserve
   the demodulator's real bandwidth around a channel, `backend/window.py`
   collapses to just `probe()` and `read()` — no anchor needed.

   The idea came from ka9q-web, which runs a spectrum channel alongside its
   audio channel and so positions the front end without ever commanding the LO.
   Nothing else moves the placement: `set_first_lo` is ignored, approach
   direction is irrelevant, and `low_edge`/`high_edge` are overridden when
   `wfm.c` re-runs `set_freq` at demod start.

**Verify FM audio by listening to the content, not by counting frames.** A
wide-open squelch streams noise at full rate, which satisfies any check based
on packet count or RMS — that is how "FM works" got claimed here in error once.
Compare spectral shape and envelope variation against a known-good station: NWR
reads voice/hiss 13.8 and envelope variation 0.49; steady hiss reads 0.03.
102.300 MHz through the browser path now reads voice/hiss 14.1 over 8.8 s of
continuous audio.
