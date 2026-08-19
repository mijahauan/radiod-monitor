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
- **`Source`** — base class with three methods subclasses override:
  - `controls_schema() -> dict` — JSON-serializable description of per-source UI controls. Currently supports `bandSegments: [{value, label, center_mhz}]` with a `defaultBand`.
  - `center_freq_hz(params) -> float` — radiod front-end center frequency for the given params.
  - `list_stations(lat, lon, radius_km, params) -> list[Station]`.

Registry lives in [backend/sources/__init__.py](backend/sources/__init__.py); adding a new source is one import + one entry in `_SOURCES`. Three sources ship:

- **`NwsSource`** ([backend/sources/nws.py](backend/sources/nws.py)) — loads `data/nws_stations.json`, 7-channel NWR band centered on 162.475 MHz, no per-source controls, preset `nfm`. Falls back to the 7 standard frequencies at the user's exact location if no station is within range, so the audio pipeline is still exercisable.
- **`RepeaterSource`** ([backend/sources/repeaters.py](backend/sources/repeaters.py)) — loads `data/repeaters*.kml` (RepeaterBook export, newest mtime wins), filters by distance and by a receiver-sized band segment, preset `nfm`. Parses callsign, downlink frequency, offset sign, and PL tone from the KML description CDATA. Warns at load time if the KML is >180 days old.
- **`FmSource`** ([backend/sources/fm.py](backend/sources/fm.py)) — loads `data/fm_stations.json` (compiled by [scripts/fetch_fm_stations.py](scripts/fetch_fm_stations.py) from the FCC CDBS public files), filters by distance and by a band segment cut to the connected receiver's window (see below), preset `wfm`, `audio_channels=2`. The `wfm` preset in [ka9q-radio/share/presets.conf](../ka9q-radio/share/presets.conf) forces a 384 kHz downconverter and 48 kHz output with 75 µs North American de-emphasis. **It does not force stereo** — both the repo copy and the installed `/usr/local/share/ka9q-radio/presets.conf` set `mono = yes`, which is why `audio_channels` is only a hint and the real count comes off the wire (see below).

**About the `audio_channels` attribute.** Declared on `Source` and reported in `GET /api/sources` and each `{type: "results"}` message, it is a *hint* only — it seeds `AudioSession` before any audio arrives.

It is not what configures the decoder, because a source cannot know the answer: the `wfm` preset ships `mono = yes` in some ka9q-radio installs and stereo in others, and `ChannelInfo` carries no channel count. A wrong `numberOfChannels` makes WebCodecs throw on the first packet, so the value has to be ground truth.

The authority is the stream itself. Every Opus packet states its channel count in bit 2 of its TOC byte (RFC 6716 §3.1); `audio_streamer.opus_channels()` reads it from the first frame, and the audio WebSocket sends `{"type": "config", "channels": N}` as a text message ahead of any binary frame. The browser configures `AudioDecoder` on that message and ignores frames until it arrives (Opus frames are self-contained, so the few dropped cost a few ms). A listener joining a stream already in progress is handed the stored value on connect.

### Band segments are a property of the radio, not the source

A `Source` that spans more spectrum than the receiver can cover at once must
cut it into segments sized to that receiver — `segment_band()` in
[backend/sources/base.py](backend/sources/base.py), at `SEGMENT_FILL` (80%) of
the usable window. Hardcoded segments are wrong on every radio but the one
they were written for.

**Where the number comes from.** radiod reports the front end's usable IF
limits as `FE_LOW_EDGE`/`FE_HIGH_EDGE` — the same `Frontend.min_IF`/`max_IF`
that `set_freq()` tests a channel against. `RadioController.probe_frontend()`
reads them on connect (and on every host switch) into `usable_bw_hz`, which
`app.py` passes to `controls_schema()` and injects into search `params`.
Deriving the width from `input_samprate` instead would overstate it: this
Airspy HF+ samples at 768 kHz but reports a 660.5 kHz window, radiod having
already discounted filter rolloff. Measured widths: **660 kHz** (Airspy HF+ @
768k), **~8.6 MHz** (Airspy R2 @ 10 Msps), the whole HF spectrum on a
direct-sampling RX888 (`isreal=True`).

The probe costs one throwaway channel — the limits ride along with
*per-channel* status, so there has to be a channel to ask about.

**Why it matters more than it looks.** radiod is frequency-first: `set_freq()`
accepts the channel frequency, then retunes the front end *only* if the
resulting IF falls outside the window, and then "as little as possible"
(`radio.c`). One window, shared by every channel. So a segment wider than the
window does not merely show unreachable stations — it makes them
*unlistenable*, and the channels inside it fight each other, each creation
dragging the window off the last. Sizing segments to the window is what makes
the activity map mean anything: every station shown is simultaneously
receivable, and radiod never has to retune at all.

Segment keys (`fm_3`, `2m_5`) therefore encode a division that depends on the
connected radio. A key saved by the browser or chosen before a host switch may
not exist afterwards, so `_segment_for()` in each source falls back to a sane
segment rather than failing the search.

**Focus is the fallback, not the mechanism.** `RadioController.focus_on()`
aims the window at one station by re-asserting its frequency
(`radio_status.c` documents `set_freq` on an unchanged frequency as the way to
"possibly reassert front end tuner control"). It is sticky — re-applied after
`apply_stations`, since every channel created moves the window — and it is
what rescues a selection outside the current window. `set_first_lo()` does
**not** work for this: radiod overrides it (measured identical `first_lo` with
no LO command, with the right one, and with a deliberately wrong one), which
is why `tune_center()` is now advisory only.

**Other clients compete.** The front end is global to the radiod instance.
`ka9q-web` against the same radiod requests its own window and drags the
front end away; it must not run alongside this app on one receiver.

### Shared pipeline

Identical in shape to the aligned nws-monitor/repeater-monitor, just generalized:

1. **Control plane — [backend/radio_controller.py](backend/radio_controller.py).** On a `search` message, the active `Source` produces a station list and a center frequency. `apply_stations()` converges the channel set. (`tune_center()` no longer tunes anything — radiod owns front-end placement; see the band-segment section above.) Channels live on a single stable multicast destination derived from `generate_multicast_ip("radiod-monitor")` — shared across all sources, so mode switching is a delta on the channel set rather than a teardown. SSRCs are deterministic (hash of frequency, preset, sample_rate, encoding, destination, agc, gain=0.0, and the radiod identity) so they survive server restarts and can be independently rediscovered by the audio streamer. Per-user squelch is applied *after* ensure_channel so it doesn't perturb the hash.

   **Convergence is a diff, and that is load-bearing.** Because the SSRC is a deterministic hash of exactly the parameters that define a channel, the wanted SSRC set is computable *before* talking to radiod — `allocate_ssrc()` with the same arguments `ensure_channel()` uses internally, including `radiod_host=control.status_address`. An existing channel on our destination whose SSRC is in that set is correct by construction, so it is left untouched; only SSRCs outside the set are removed, and only missing ones are created.

   The earlier implementation wiped every channel on the destination and rebuilt, which forced it to re-create SSRCs that radiod was still asynchronously tearing down — so it had to poll up to 10 s for `freq=0` zombies to disappear on *every* search (the "Timed out waiting for radiod to purge zombie channels" warning fired routinely). With a diff the removed and created sets are disjoint by definition, so there is no race to wait on: removals are fire-and-forget and the poll is gone. Time-to-first-audio went from 3.7 s cold / 7–13 s on a repeat search to 1.2 s / ~0.06 s, and a search no longer cuts off audio the user is listening to.

   Reuse is gated on the discovered `frequency` **and** `encoding`, not the SSRC alone — the frequency guards against a zombie mid-teardown still answering on that SSRC, and the encoding against radiod having reset the channel to the preset default. Both fields come free from the discovery already performed. If you add a parameter that affects what radiod serves, it must either be in the SSRC hash or checked here, or a stale channel will be silently reused.

2. **Activity monitor — [backend/app.py](backend/app.py) `activity_monitor()`.** Background task polling `discover_channels(radiod_host)` every 2 s, reading `ChannelInfo.snr`, and broadcasting `{type: "activity", freq, isActive, snr}` to all connected control-WebSocket clients. `isActive` is `snr > 3.0 dB`. Looks up channels by SSRC, falls back to frequency match within 100 Hz. This is what flips map markers green.

3. **Audio plane — [backend/audio_streamer.py](backend/audio_streamer.py).** One `ManagedStream` per frequency, shared across browser listeners via `asyncio.Queue`s. radiod is configured with `Encoding.OPUS`, `sample_rate=48000`, `samples_per_packet=960` (20 ms), `deliver_interval_packets=1`, and **`raw_payloads=True`** — ka9q-python's transport mode for framed encodings, where `on_samples` receives a `List[bytes]` of undecoded RTP payloads with the resequencer bypassed (a codec frame is opaque: it can be neither concatenated nor zero-filled, and gap concealment belongs to the decoder). With `deliver_interval_packets=1` that is exactly one encoded frame per call.

   **The Opus grant must be asserted, not assumed.** `ensure_channel(encoding=OPUS)` is not sufficient: radiod applies the preset's default output encoding (s16be) and honours OPUS only from a follow-up `OUTPUT_ENCODING` command. It must also be asserted *before* the receiver starts — assert it afterwards and the first packets on the socket are PCM, which is then forwarded to the browser labelled as Opus. `add_listener` therefore calls `ensure_channel` + `_assert_opus` (which verifies the grant) before constructing the `ManagedStream`, and `on_stream_restored` re-asserts it because a re-created channel comes back on the preset default. As a backstop, `_broadcast` drops any payload over 1275 bytes — the RFC 6716 maximum for a single Opus frame — and logs it, so a lost grant is loud instead of silent. Each frame is shipped to the browser as a single WebSocket binary message on `WS /ws/audio/{freq_hz}`. No Ogg container, no tagging, no server-side decoding — WebSocket/TCP preserves frame order and the browser uses WebCodecs `AudioDecoder`. `on_stream_restored` re-applies squelch because only hash-stable parameters survive a radiod restart.

   **WebCodecs requires a secure context** (HTTPS or `localhost`) — the shell script auto-generates a self-signed cert to satisfy this.

### Frequency→SSRC coupling (the one cross-file invariant)

`RadioController.apply_stations` and `AudioStreamer.add_listener` both call `ensure_channel`/`ManagedStream` and must pass *identical* `destination`, `encoding`, `sample_rate`, `preset`, and `gain=0.0`. `AudioStreamer` reads `preset` and `sample_rate` off the controller at stream creation time to keep them in lockstep. If you change how preset is selected (e.g. per-station instead of per-source), change it in both places and through the controller's `preset` attribute. `apply_stations()` and `add_listener()` must also both assert `OUTPUT_ENCODING = OPUS` after `ensure_channel` and verify the grant — radiod otherwise serves the preset default on whichever path skipped it. `audio_channels` is *not* part of this invariant any more: it is a display hint, and the decoder is configured from the first Opus frame's TOC byte instead.

### Host switching

Radiod instance discovery uses `ka9q.discovery.discover_radiod_services()`, which shells out to `avahi-browse -t _ka9q-ctl._udp`. The `GET /api/radiod/discover` endpoint returns `{hosts: [{name, address}], current}`. `POST /api/radiod/select` validates the hostname against `^[a-zA-Z0-9][a-zA-Z0-9.\-]*$` (matching SWL-ka9q's regex to prevent shell metacharacters even though we never shell out), stops all active audio streams, tears down `RadioController`, reconnects to the new host, and returns. The frontend re-runs the search automatically after a successful switch.

### HTTP / WebSocket routes

```
GET  /api/radiod/discover              → {hosts: [{name, address}], current}
POST /api/radiod/select  {host}        → {ok, host, changed}
GET  /api/sources                      → {sources: [{key, display_name, preset, controls}]}
WS   /ws/control                       ← JSON messages
  → {type: "search", mode, location, radius, squelch, params}
  ← {type: "results", mode, lat, lon, stations: [Station]}
  ← {type: "activity", freq, isActive, snr}
  ← {type: "error", message}
WS   /ws/audio/{freq_hz}
  ← {"type": "config", "channels": N}   (text, once, before any audio)
  ← one binary message per Opus frame
```

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

Hardware note: `radiod@airspy-generic` (the wideband Airspy R2) crash-loops with
`AIRSPY_ERROR_NOT_FOUND` — only the Airspy HF+ is on USB.

The HF+ **does** tune VHF: measured clean tuning at 146 MHz and 36 dB SNR at
162 MHz (NWR audio plays). What it cannot do is cover much at once — a 660.5 kHz
window — which is why band segments are now cut to the radio rather than fixed.
Broadcast FM works on this radio. Getting there took fixing two bugs, and the
earlier guesses in this file (front-end bandwidth, then antenna) were both wrong
— worth remembering when the next mode produces silence.

1. **ka9q-python sent `DEMOD_TYPE` contradicting the preset.** It derived the
   demodulator from a five-name allowlist and sent it right after `PRESET` in
   the same packet, so the later value won: `wfm` got `FM_DEMOD`, and radiod
   ran the *narrowband* FM demod behind the wfm preset's ±110 kHz filter. That
   emits nothing and reports `snr=-inf` forever. Fixed in ka9q-python 3.25.1
   (which is why the pin is >= 3.25.1).

2. **`Source.snr_squelch = False` cannot be honoured as written.** `wfm.c` sets
   `chan->squelch.snr_enable = true` unconditionally when the demod thread
   starts, so `set_squelch(enable=False)` is reverted. `_squelch_args()` holds
   the squelch open with a −20 dB threshold instead.

The window was never the problem: radiod places the channel at IF +219.2 kHz,
exactly `max_IF - filter.max_IF - fudge`, so the whole ±110 kHz filter lands
inside the 660.5 kHz window, and the 384 kHz composite path fits the 768 kHz
input. Verified end-to-end: FM stations stream ~9 s of continuous audio at
rms 0.06 through the browser path.
