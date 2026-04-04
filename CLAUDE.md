# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
./radiod-monitor.sh start|stop|restart|status
```

- Serves on port **8443**; HTTPS if `certs/key.pem` + `certs/cert.pem` exist, otherwise plain HTTP.
- PID file: `.app.pid`. Logs: `backend.log` (append-only).
- `start` calls `ensure_certs` which auto-generates a self-signed cert (valid 10 years, CN=`$(hostname -f)`, SANs for FQDN/localhost/127.0.0.1) on first run.
- `uvicorn` runs with `reload=True`, so edits under `backend/` hot-reload.
- Initial radiod host comes from `RADIOD_HOST` env var (default `airspy-status.local`); UI dropdown can override at runtime.

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

Registry lives in [backend/sources/__init__.py](backend/sources/__init__.py); adding a new source is one import + one entry in `_SOURCES`. Two sources ship:

- **`NwsSource`** ([backend/sources/nws.py](backend/sources/nws.py)) — loads `data/nws_stations.json`, 7-channel NWR band centered on 162.475 MHz, no per-source controls. Falls back to the 7 standard frequencies at the user's exact location if no station is within range, so the audio pipeline is still exercisable.
- **`RepeaterSource`** ([backend/sources/repeaters.py](backend/sources/repeaters.py)) — loads `data/repeaters*.kml` (RepeaterBook export, newest mtime wins), filters by distance and band segment, center frequency = midpoint of the selected segment. Parses callsign, downlink frequency, offset sign, and PL tone from the KML description CDATA. Warns at load time if the KML is >180 days old.

### Shared pipeline

Identical in shape to the aligned nws-monitor/repeater-monitor, just generalized:

1. **Control plane — [backend/radio_controller.py](backend/radio_controller.py).** On a `search` message, the active `Source` produces a station list and a center frequency. `RadioController.tune_center()` re-tunes the receiver front end; `apply_stations()` diffs the requested station set against `active_channels`, removes stale channels, and calls `ensure_channel` for new ones with the current `Source.preset`. Channels are created on a single stable multicast destination derived from `generate_multicast_ip("radiod-monitor")` — shared across all sources, so mode switching is a cheap delta on the channel set rather than a teardown. SSRCs are deterministic (hash of frequency, preset, sample_rate, encoding, destination, gain=0.0) so they survive server restarts and can be independently rediscovered by the audio streamer. Per-user squelch is applied *after* ensure_channel so it doesn't perturb the hash.

2. **Activity monitor — [backend/app.py](backend/app.py) `activity_monitor()`.** Background task polling `discover_channels(radiod_host)` every 2 s, reading `ChannelInfo.snr`, and broadcasting `{type: "activity", freq, isActive, snr}` to all connected control-WebSocket clients. `isActive` is `snr > 3.0 dB`. Looks up channels by SSRC, falls back to frequency match within 100 Hz. This is what flips map markers green.

3. **Audio plane — [backend/audio_streamer.py](backend/audio_streamer.py).** One `ManagedStream` per frequency, shared across browser listeners via `asyncio.Queue`s. radiod is configured with `Encoding.OPUS`, `sample_rate=48000`, `samples_per_packet=960` (20 ms), `deliver_interval_packets=1`; `RadiodStream._parse_samples` returns the raw payload as `bytes` and bypasses the resequencer for Opus, so `on_samples` delivers exactly one encoded frame per call. Each frame is shipped to the browser as a single WebSocket binary message on `WS /ws/audio/{freq_hz}`. No Ogg container, no tagging, no server-side decoding — WebSocket/TCP preserves frame order and the browser uses WebCodecs `AudioDecoder`. `on_stream_restored` re-applies squelch because only hash-stable parameters survive a radiod restart.

   **WebCodecs requires a secure context** (HTTPS or `localhost`) — the shell script auto-generates a self-signed cert to satisfy this.

### Frequency→SSRC coupling (the one cross-file invariant)

`RadioController.apply_stations` and `AudioStreamer.add_listener` both call `ensure_channel`/`ManagedStream` and must pass *identical* `destination`, `encoding`, `sample_rate`, `preset`, and `gain=0.0`. `AudioStreamer` reads `preset` and `sample_rate` off the controller at stream creation time to keep them in lockstep. If you change how preset is selected (e.g. per-station instead of per-source), change it in both places and through the controller's `preset` attribute.

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
WS   /ws/audio/{freq_hz}                 ← one binary message per Opus frame
```

## Known quirks

- **Markers are keyed by `freq_hz`** in the frontend (`markers[st.freq_hz]`). Two stations at identical downlink frequencies but different locations collide — last one wins for the icon and activity updates. Acceptable today, worth knowing.
- **First-run radiod failure is non-fatal.** If the default radiod host is unreachable at startup, the app logs a warning and still comes up; the user can pick a different host from the dropdown.
- **`data/repeaters*.kml`** is shipped as a starter. The KML age warning fires at 180 days. There is no built-in refresh — the user downloads a new file from RepeaterBook and drops it into `data/`.
- **Browser autoplay gating.** The "Listen" click itself satisfies the user-gesture requirement, and the `AudioSession` class resumes the `AudioContext` immediately. If it's still suspended afterwards, the "Unmute / Resume Audio" button appears and the user clicks it.
