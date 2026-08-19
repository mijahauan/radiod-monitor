"""
radiod-monitor FastAPI application.

Architecture (see README.md and CLAUDE.md):

    /api/radiod/discover    GET   → {hosts: [{name, address}], current}
    /api/radiod/select      POST  → switch the backing radiod instance
    /api/sources            GET   → {sources: [{key, display_name, controls}]}
    /ws/control             WS    ← search, activity updates
    /ws/audio/{freq_hz}     WS    ← raw Opus frames (binary, one per message)

The backend is source-agnostic below the control WebSocket. On a search
message it looks up the requested Source, asks it for a station list and
a front-end center frequency, tunes radiod, and hands the list to
RadioController which ensures channels on a stable multicast destination.
The activity monitor polls discover_channels every 2 s and broadcasts SNR
to all connected control sockets.
"""
import asyncio
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ka9q import discover_channels
from ka9q.discovery import discover_radiod_services

from .audio_streamer import streamer
from .geo import parse_location
from .radio_controller import RadioController
from . import sources as source_registry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_RADIOD_HOST = os.environ.get("RADIOD_HOST", "airspyhf-status.local")

# Match SWL-ka9q's validation — alphanumeric + dash + dot, must begin
# with an alphanumeric. Prevents shell metacharacters or whitespace.
_HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]*$")

SNR_ACTIVE_THRESHOLD = 3.0    # dB above noise → "active"
ACTIVITY_POLL_INTERVAL = 2.0  # seconds

controller = RadioController(radiod_host=DEFAULT_RADIOD_HOST)
active_websockets: List[WebSocket] = []


# ---------------------------------------------------------------------------
# Background activity monitor
# ---------------------------------------------------------------------------
async def activity_monitor():
    """Poll radiod for per-channel SNR and broadcast to control WebSockets."""
    while True:
        await asyncio.sleep(ACTIVITY_POLL_INTERVAL)
        if not active_websockets:
            continue
        try:
            if controller.active_channels:
                channels = await asyncio.to_thread(
                    discover_channels, controller.radiod_host, 1.0
                )
                for ssrc, freq_hz in list(controller.active_channels.items()):
                    ch = channels.get(ssrc)
                    if ch is None:
                        ch = next(
                            (c for c in channels.values()
                             if abs(c.frequency - freq_hz) < 100.0),
                            None,
                        )
                    raw_snr = ch.snr if ch is not None else None
                    if raw_snr is not None and (math.isinf(raw_snr) or math.isnan(raw_snr)):
                        raw_snr = None
                    is_active = raw_snr is not None and raw_snr > SNR_ACTIVE_THRESHOLD
                    msg = {
                        "type": "activity",
                        "freq": freq_hz,
                        "isActive": is_active,
                        "snr": round(raw_snr, 1) if raw_snr is not None else None,
                    }
                    for ws in list(active_websockets):
                        try:
                            await ws.send_json(msg)
                        except Exception:
                            pass

            # The window read/broadcast runs every cycle regardless of
            # whether any channels exist: read_window() can resolve an SSRC
            # from the anchor channel alone, which is precisely the case
            # (directory mode) where active_channels is empty but the
            # frequency strip most needs the window position.
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"activity_monitor error: {e}")


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"radiod-monitor startup — default radiod host: {DEFAULT_RADIOD_HOST}")
    try:
        await controller.connect()
    except Exception as e:
        logger.warning(f"Initial radiod connection failed: {e}. "
                       f"User can pick a different host from the UI.")
    monitor_task = asyncio.create_task(activity_monitor())
    yield
    logger.info("radiod-monitor shutdown...")
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    await streamer.stop_all()
    await controller.close()


app = FastAPI(lifespan=lifespan)

frontend_dir = os.path.join(BASE_DIR, "frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    with open(os.path.join(frontend_dir, "index.html"), "r") as f:
        return HTMLResponse(f.read())


@app.get("/api/radiod/discover")
async def radiod_discover():
    """
    Browse the LAN for radiod instances via mDNS (_ka9q-ctl._udp).
    Returns the current host regardless so the UI always has something.
    """
    try:
        services = await asyncio.to_thread(discover_radiod_services, 5.0)
    except Exception as e:
        logger.warning(f"discover_radiod_services failed: {e}")
        services = []
    return {
        "hosts": services,  # [{name, address}, ...]
        "current": controller.radiod_host,
    }


class HostSelect(BaseModel):
    host: str


@app.post("/api/radiod/select")
async def radiod_select(body: HostSelect):
    """Switch the backing radiod instance. Validates hostname shape first."""
    new_host = (body.host or "").strip()
    if not _HOST_RE.match(new_host):
        raise HTTPException(status_code=400, detail="Invalid host format")

    if new_host == controller.radiod_host and controller.control is not None:
        return {"ok": True, "host": new_host, "changed": False}

    logger.info(f"Switching radiod host: {controller.radiod_host} → {new_host}")

    # Stop any in-flight audio streams (they're bound to the old host's
    # multicast groups and would otherwise keep the old ManagedStream alive).
    await streamer.stop_all()
    await controller.close()
    controller.radiod_host = new_host
    try:
        await controller.connect()
    except Exception as e:
        logger.error(f"Failed to connect to new radiod {new_host}: {e}")
        raise HTTPException(status_code=502, detail=f"Cannot connect to {new_host}")

    return {"ok": True, "host": new_host, "changed": True}


@app.get("/api/sources")
async def sources():
    """Return the registered frequency sources and their UI control schemas."""
    return {
        "sources": [
            {
                "key": s.key,
                "display_name": s.display_name,
                "preset": s.preset,
                "audio_channels": s.audio_channels,
                "controls": s.controls_schema(),
            }
            for s in source_registry.all_sources().values()
        ],
    }


# ---------------------------------------------------------------------------
# Control WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws/control")
async def websocket_control(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "search":
                await _handle_search(websocket, data)
            else:
                logger.debug(f"Unknown control message type: {msg_type}")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"control WS error: {e}")
    finally:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


async def _handle_search(websocket: WebSocket, data: Dict[str, Any]):
    mode = data.get("mode", "nws")
    try:
        source = source_registry.get(mode)
    except KeyError:
        await websocket.send_json({"type": "error", "message": f"Unknown mode: {mode}"})
        return

    loc = data.get("location", "")
    radius_km = float(data.get("radius", 100.0))
    squelch_db = float(data.get("squelch", 10.0))
    params = data.get("params", {}) or {}

    lat, lon = parse_location(loc)
    if lat is None or lon is None:
        await websocket.send_json({
            "type": "error",
            "message": "Invalid grid square or lat,lon format",
        })
        return

    # Squelch (applied immediately — synchronous on RadiodControl, fast
    # enough not to need to_thread).
    controller.set_squelch(squelch_db)

    stations = source.list_stations(lat, lon, radius_km, params)

    # Whether the activity map can mean anything for this set. Computed here
    # rather than read back from apply_stations because results are sent
    # first: the converge task runs in the background.
    activity_available = controller.fits_window(s.freq_hz for s in stations)

    await websocket.send_json({
        "type": "results",
        "mode": mode,
        "preset": source.preset,
        "audio_channels": source.audio_channels,
        "lat": lat,
        "lon": lon,
        "stations": [s.to_dict() for s in stations],
        "activity": activity_available,
    })

    # ensure_channel is blocking; fire-and-forget off the event loop.
    async def _converge():
        await asyncio.to_thread(
            controller.apply_stations,
            stations,
            source.preset,
            source.audio_channels,
            source.snr_squelch,
            source.sample_rate,
        )
        # A mode switch drops frequencies from the monitored set. Any stream
        # still running on one of those has just had its channel deleted, and
        # ManagedStream would otherwise keep re-creating it against the next
        # search's removals — see AudioStreamer.drop_unmonitored.
        await streamer.drop_unmonitored(controller.monitored_freqs)

    asyncio.create_task(_converge())


# ---------------------------------------------------------------------------
# Audio WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws/audio/{freq_hz}")
async def websocket_audio(websocket: WebSocket, freq_hz: float):
    """
    Streams raw Opus frames for a single monitored frequency. One binary
    message per Opus frame (20 ms @ 48 kHz). WebSocket/TCP preserves order;
    the browser feeds each message into WebCodecs AudioDecoder.

    Ahead of the audio, one JSON text message carries the stream's real
    channel count, read from the first Opus frame's TOC byte. The browser
    cannot configure a decoder without it, and a preset-derived guess is
    not reliable (see audio_streamer.opus_channels).

    The frequency must be one the current search is monitoring. Without that
    check this route creates a radiod channel at whatever frequency it is
    handed, so anything that can open the socket can allocate channels on
    the receiver -- and those channels then sit there, outside the set that
    apply_stations() knows how to clean up.
    """
    await websocket.accept()
    if not any(abs(f - freq_hz) < 1.0 for f in controller.monitored_freqs):
        logger.warning(
            f"Rejecting audio request for {freq_hz/1e6:.3f} MHz: "
            f"not in the monitored set"
        )
        await websocket.send_json({
            "type": "error",
            "message": f"{freq_hz/1e6:.3f} MHz is not currently monitored — "
                       f"run a search first.",
        })
        await websocket.close()
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    await streamer.add_listener(freq_hz, queue, controller)
    try:
        while True:
            item = await queue.get()
            if isinstance(item, dict):
                await websocket.send_json(item)
            else:
                await websocket.send_bytes(item)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"audio ws error for {freq_hz/1e6:.3f} MHz: {e}")
    finally:
        if await streamer.remove_listener(freq_hz, queue):
            # Nobody is listening any more, so stop pinning the front end to
            # this station; the next Listen re-aims it.
            controller.clear_focus()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    cert_dir = os.path.join(BASE_DIR, "certs")
    ssl_kwargs = {}
    if (os.path.isfile(os.path.join(cert_dir, "key.pem")) and
            os.path.isfile(os.path.join(cert_dir, "cert.pem"))):
        ssl_kwargs = {
            "ssl_keyfile": os.path.join(cert_dir, "key.pem"),
            "ssl_certfile": os.path.join(cert_dir, "cert.pem"),
        }
        logger.info(f"SSL enabled from {cert_dir}")
    else:
        logger.info("No certs found — running without SSL (HTTP)")
    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=8443,
        reload=True,
        reload_dirs=[os.path.join(os.path.dirname(__file__))],
        **ssl_kwargs,
    )
