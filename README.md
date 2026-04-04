# radiod-monitor

A web frontend for [ka9q-radio](https://github.com/ka9q/ka9q-radio)'s `radiod`. Pick a radiod instance on the LAN, pick a monitoring mode (NOAA Weather Radio, amateur repeaters, etc.), scan for stations around your location, watch them light up when signal is present, and click any station to listen live in your browser.

Supersedes the older `nws-monitor` and `repeater-monitor` apps by unifying their control / audio pipelines and introducing a pluggable **Source** interface so adding new frequency sets (airband, marine VHF, NOAA GOES, etc.) is a ~50-line drop-in.

## Features

- **Pluggable monitoring modes.** Each "mode" is a Python class (a `Source`) that knows its frequencies, the front-end tune target, and any per-source UI controls. Ships with:
    - **NOAA Weather Radio** — 7-channel NWR band, filtered by distance.
    - **VHF/UHF Amateur Repeaters** — RepeaterBook KML, filtered by distance and band segment (2m / 1.25m / 70cm).
- **Radiod instance discovery.** The sidebar dropdown lists every radiod instance advertising `_ka9q-ctl._udp` on the LAN (via mDNS). Switch between SDRs — for example, an Airspy R2 for VHF and an RX888 for HF — from the browser without restarting anything.
- **Live activity indication.** A background task polls radiod's per-channel SNR every 2 s; map markers and the sidebar list flip to "active" green above a configurable threshold and back to idle blue when silent.
- **Low-latency browser audio.** Raw Opus frames (20 ms @ 48 kHz mono) are forwarded over WebSocket and decoded with the browser's native WebCodecs `AudioDecoder`, scheduled on a Web Audio `AudioContext`. No Ogg muxing, no server-side decoding, ~100 ms jitter buffer.
- **Self-healing streams.** Uses `ka9q-python`'s `ManagedStream`, which re-attaches to channels automatically across radiod restarts via deterministic SSRCs.
- **HTTPS out of the box.** The start script auto-generates a self-signed certificate on first run, because WebCodecs requires a secure context.

## Architecture

```
Browser (Leaflet + WebCodecs + Web Audio)
  │
  ├── GET  /api/radiod/discover   — list radiod instances on LAN (mDNS)
  ├── POST /api/radiod/select     — switch the backing radiod
  ├── GET  /api/sources           — list modes and their UI control schemas
  ├── WS   /ws/control            — search request + live activity updates (JSON)
  └── WS   /ws/audio/{freq_hz}    — one Opus frame per binary message
  │
FastAPI + Uvicorn  (backend/)
  │
  ├── app.py               — HTTP/WS routes, activity monitor
  ├── radio_controller.py  — ensure_channel, front-end tune, squelch
  ├── audio_streamer.py    — ManagedStream → Opus → WebSocket fan-out
  ├── geo.py               — Maidenhead + haversine
  └── sources/             — pluggable frequency providers
      ├── base.py          — Source protocol, Station dataclass
      ├── nws.py           — NOAA Weather Radio
      └── repeaters.py     — VHF/UHF amateur repeaters (KML)
  │
ka9q-python
  │
radiod  (ka9q-radio)
```

### Source plugin contract

A Source subclass supplies three things to the pipeline (see [backend/sources/base.py](backend/sources/base.py)):

```python
class MySource(Source):
    key = "my_source"                  # registry key
    display_name = "My Frequencies"    # shown in the mode dropdown
    preset = "nfm"                     # radiod demod preset

    def controls_schema(self) -> dict:
        """Optional UI controls (e.g. band segment selector)."""
        return {}

    def center_freq_hz(self, params) -> float:
        """Front-end center frequency to tune for these params."""

    def list_stations(self, lat, lon, radius_km, params) -> list[Station]:
        """Filtered station list to monitor."""
```

Wire it into `backend/sources/__init__.py` and it shows up as a new mode in the UI. The shared control WebSocket, `ensure_channel` loop, activity monitor, and audio WebSocket are completely source-agnostic — they only see the uniform `Station` schema.

## Prerequisites

- **Python 3.10+**
- **`openssl`** on `$PATH` (used once on first run to generate a self-signed cert)
- **`avahi-browse`** on `$PATH` (used for radiod instance discovery — ka9q-python shells out to it)
- **A running `radiod` instance** from [ka9q-radio](https://github.com/ka9q/ka9q-radio), reachable on your LAN, configured with NFM Opus output (or whatever demod mode your chosen sources need).
- **[ka9q-python](https://github.com/ka9q/ka9q-python)** ≥ 3.4.2
- **A modern browser with WebCodecs** — Chrome, Edge, or Firefox. WebCodecs is only available in a secure context (HTTPS or `localhost`); the startup script handles this automatically.

## Installation

```bash
git clone https://github.com/mijahauan/radiod-monitor.git
cd radiod-monitor

python3 -m venv venv
venv/bin/pip install --upgrade pip

# Install ka9q-python — from PyPI:
venv/bin/pip install ka9q-python

# ...or as an editable install from a local sibling checkout:
#   venv/bin/pip install -e ../ka9q-python

# Install radiod-monitor and its remaining dependencies:
venv/bin/pip install -e .
```

## Running

```bash
./radiod-monitor.sh start     # start in the background
./radiod-monitor.sh status    # check whether it's running
./radiod-monitor.sh restart   # stop + start
./radiod-monitor.sh stop      # stop
```

- Serves on port **8443**. PID in `.app.pid`, logs appending to `backend.log`.
- `uvicorn` runs with `reload=True`, so edits under `backend/` hot-reload.
- Open `https://<hostname>:8443/` (or `https://localhost:8443/` locally) in a browser. Accept the self-signed certificate once.

`RADIOD_HOST=foo.local ./radiod-monitor.sh start` sets the initial radiod host; the UI dropdown can override it at any time.

## TLS

The browser's WebCodecs `AudioDecoder` is only available in a **secure context** (HTTPS or `localhost`). On first `./radiod-monitor.sh start`, the script auto-generates a self-signed certificate:

- Written to `certs/cert.pem` and `certs/key.pem` (valid 10 years, `CN=$(hostname -f)`, SANs for the FQDN, `localhost`, and `127.0.0.1`).
- Browsers will show a one-time "unsafe" warning — accept it once per client.
- To supply your own certificate, drop the files into `certs/` before first run; the script skips generation if they already exist.
- If `openssl` is not on `$PATH`, the script warns and starts over plain HTTP — in that case, audio works only from a `localhost` browser.

The `certs/` directory is git-ignored.

## Repeater Data

The repeater source loads `data/repeaters*.kml`, a KML export from [RepeaterBook](https://www.repeaterbook.com). A starter file is included but will get stale over time; the app logs a warning at load time if the file is older than 180 days.

To refresh:

1. Go to <https://www.repeaterbook.com/repeaters/index.php>.
2. Use "Advanced Search" to filter by state/region/band as desired.
3. On the results page, choose "Export this listing" → "KML".
4. Replace `data/repeaters.kml` with the download (any filename matching `data/repeaters*.kml` works; newest mtime wins).

## Controls

- **Radiod Instance** — dropdown of radiod instances discovered on the LAN via mDNS. Click ↻ to rescan. Switching instances closes all active audio sessions and re-scans under the new host.
- **Mode** — which Source to drive the scan (NOAA Weather Radio, repeaters, …). Persisted in `localStorage` across reloads.
- **Per-mode controls** — automatically rendered from the selected Source's `controls_schema()`. Currently: the repeater source shows a band-segment dropdown.
- **Grid Square / Lat,Lon** — Maidenhead locator (e.g. `EM38ww`) or a decimal `lat,lon` pair.
- **Squelch** — SNR threshold in dB. Above this value, a station lights up green on the map and in the sidebar.
- **Search Radius** — maximum great-circle distance in km for filtering the station list.

Any change to squelch, radius, band, or location re-runs the scan.

## Why a Single App

Given an Airspy R2 (≈5 MHz bandwidth), NWS (162 MHz) and 2m repeaters (144–148 MHz) can't be monitored concurrently — the receiver front end tunes to one window at a time. Running two separate apps against the same radiod would have them stomp on each other's tune calls silently. A single app with mode switching makes that constraint explicit (switching modes *is* retuning) and turns future monitoring targets into a strictly additive plugin problem. If you have a wider-bandwidth SDR (e.g. an RX888 for HF) on a second radiod instance, the instance selector lets you jump between them from the same UI.

## Troubleshooting

- **"Failed to connect to radiod"** at startup — the hostname isn't resolving, or radiod isn't advertising on this LAN. Pick a different instance from the dropdown, or check `avahi-browse -t _ka9q-ctl._udp`.
- **Radiod dropdown is empty** — `avahi-browse` isn't on `$PATH`, or mDNS traffic is being blocked. Install `avahi-utils` and ensure the `avahi-daemon` is running.
- **"WebCodecs AudioDecoder unavailable"** alert — you're hitting the server over plain HTTP from a non-`localhost` origin. Connect via `https://…` and accept the self-signed cert, or from the server machine itself.
- **Station list is empty after a scan** — check `backend.log` for KML/JSON load messages. For the repeater source: make sure a `data/repeaters*.kml` file exists. For NWS: the station JSON is shipped, but if your location is far from any listed station, the source falls back to the 7 standard frequencies at your exact location so you can still exercise the audio pipeline.
- **Markers never turn green** — the activity monitor publishes `{type: "activity"}` messages on the control WebSocket every 2 s. If they're arriving in the browser console but markers don't change, two stations at the same downlink frequency may be colliding on the marker keyed by Hz. If they're not arriving at all, look for `activity_monitor error:` lines in `backend.log`; the most common cause is `discover_channels` timing out against the selected radiod host.
- **Audio plays briefly then stops** — usually squelch misconfiguration or a genuine signal drop. Lower the squelch slider to confirm the pipeline is healthy before chasing anything deeper.
- **Orphaned channels in radiod** — SSRCs are deterministic, so restarting this app is safe; `ensure_channel` reattaches to existing channels. Channels created under a *different* destination slug (e.g. from the legacy `nws-monitor`/`repeater-monitor` apps) won't collide but also won't be cleaned up — restart radiod itself to clear them.

## License

MIT — see [LICENSE](LICENSE).
