"""
radiod-monitor FastAPI application.

Architecture (see README.md and CLAUDE.md):

    /api/radiod/discover    GET   → {hosts: [{name, address}], current}
    /api/radiod/select      POST  → switch the backing radiod instance
    /api/sources            GET   → {sources: [{key, display_name, controls}]}
    /ws/control             WS    ← search, activity updates
    /ws/audio                WS    ← raw Opus frames (binary, one per message)

The backend is source-agnostic below the control WebSocket. On a search
message it looks up the requested Source, asks it for a station list and
a front-end center frequency, tunes radiod, and hands the list to
RadioController which ensures channels on a stable multicast destination.
The activity monitor polls discover_channels every 2 s and broadcasts SNR
to all connected control sockets.
"""
import asyncio
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ka9q import discover_channels
from ka9q.discovery import discover_radiod_services

from .geo import parse_location
from .radio_controller import RadioController
from .vfo import put_control
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

# How long the app keeps the radio after the last control socket closes.
# A browser reload disconnects and reconnects within a second, so releasing
# immediately would churn channels for nothing; waiting forever leaves the
# VFO parked on a station, dragging the shared front-end window to it long
# after anyone is listening. Also comfortably longer than radiod's ~20 s
# channel purge, so a reconnect cannot land on an SSRC still being reaped.
IDLE_RELEASE_SEC = 30.0
_idle_release_task: Optional[asyncio.Task] = None


def _cancel_idle_release():
    global _idle_release_task
    if _idle_release_task is not None:
        _idle_release_task.cancel()
        _idle_release_task = None


def _schedule_idle_release():
    """Hand the radio back if nobody comes back within the grace period."""
    global _idle_release_task
    _cancel_idle_release()

    async def _later():
        try:
            await asyncio.sleep(IDLE_RELEASE_SEC)
        except asyncio.CancelledError:
            return
        if active_websockets or controller.vfo.listeners:
            return          # someone came back, or is still listening
        try:
            await controller.release_idle()
        except Exception as e:
            logger.warning(f"idle release failed: {e}")

    _idle_release_task = asyncio.create_task(_later())


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
            # whether any channels exist: FrontEndWindow.read() can resolve
            # an SSRC from the anchor channel alone, which is precisely the
            # case (directory mode) where active_channels is empty but the
            # frequency strip most needs the window position.
            # The VFO outlives searches, so it is the reliable hint. In
            # directory mode active_channels is empty by design, and the
            # anchor may not exist until the first Listen -- read() tries
            # its own anchor_ssrc first and falls back to whatever we pass.
            ssrc_hint = (
                controller.vfo.ssrc
                if controller.vfo.ssrc is not None
                else next(iter(controller.active_channels), None)
            )
            window = await asyncio.to_thread(
                controller.window.read, controller.control, ssrc_hint
            )
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

    # Tell any open audio sockets the station is going away before we stop
    # the stream out from under them -- controller.close() already calls
    # vfo.stop(), so a redundant call here would be silent. Without this the
    # browser just goes quiet with no indication why.
    if controller.vfo.freq_hz is not None:
        nosignal = {"type": "nosignal", "freq_hz": controller.vfo.freq_hz,
                    "reason": f"switching to {new_host}"}
        for q in controller.vfo.listeners:
            put_control(q, nosignal)
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
    _cancel_idle_release()
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
        if not active_websockets:
            _schedule_idle_release()


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

    # Squelch (applied immediately). It iterates active_channels issuing a
    # command per channel, and send_command retries with backoff, so it is a
    # blocking radiod call like every other one -- off the loop it goes.
    await asyncio.to_thread(controller.set_squelch, squelch_db)

    stations = source.list_stations(lat, lon, radius_km, params)

    # Whether the activity map can mean anything for this set. Computed here
    # rather than read back from apply_stations because results are sent
    # first: the converge task runs in the background.
    activity_available = controller.fits_window(s.freq_hz for s in stations)

    # Published synchronously, before the results reach the browser and before
    # _converge is even scheduled. These three fields are what a Listen click
    # is validated and tuned against; _apply_stations_locked sets them too,
    # but it runs in a worker thread behind a lock a previous converge may
    # still hold. The browser gets the station list first either way, so an
    # eager click landed in that gap and was rejected with "not currently
    # monitored" for a station visibly on screen -- or tuned with the previous
    # source's preset, which for an FM station under `nfm` is silence.
    controller.monitored_freqs = {float(s.freq_hz) for s in stations}
    controller.preset = source.preset
    controller.sample_rate = source.sample_rate

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
        # A mode switch rebuilds the sensor set on a new band, and every
        # ensure_channel makes radiod re-place the front end. A listener tuned
        # to a station from the PREVIOUS search falls out of the window and
        # simply goes quiet -- no nosignal, no UI change, nothing above DEBUG.
        # Tell them, then release the VFO so the anchor can be dropped below.
        vfo_freq = controller.vfo.freq_hz
        if vfo_freq is not None and not any(
                abs(f - vfo_freq) < 1.0 for f in controller.monitored_freqs):
            stale = {"type": "nosignal", "freq_hz": vfo_freq,
                     "reason": "station is not in the current search"}
            for q in controller.vfo.listeners:
                put_control(q, stale)
            await controller.vfo.stop()

        # A mode switch may leave the anchor centred on a frequency that no
        # longer belongs to this search. The anchor channel is exempt from
        # the stale-channel sweep in _apply_stations_locked (that is what
        # keeps it from being deleted out from under a listener), so without
        # this it would sit there holding the front end on a station nobody
        # can reach any more, surviving every later search until shutdown.
        # The next Listen recentres it via FrontEndWindow.centre_on.
        #
        # Gated on the VFO not being tuned to anything, not on whether a
        # session socket is open: the session socket is opened once per page
        # load and stays open the whole time, so `vfo.listeners` alone is
        # non-empty from load to close and would never gate this. `freq_hz`
        # is None before the first tune and after stop() clears it, and is
        # set for exactly as long as someone is hearing a station -- so this
        # releases an idle session's anchor while still not un-centring the
        # window out from under whoever is currently listening on an
        # anchored station, which is the "search cuts off audio" regression
        # this project already fixed once.
        if controller.vfo.freq_hz is None:
            # remove_channel on the control socket: blocking, like the rest.
            await asyncio.to_thread(controller.window.release,
                                    controller.control)

    asyncio.create_task(_converge())


# ---------------------------------------------------------------------------
# Audio WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws/audio")
async def websocket_audio(websocket: WebSocket):
    """One socket for the session; the station changes underneath it.

    The browser sends {"tune": freq_hz} to change station. Because the VFO's
    SSRC never changes, no WebSocket, stream or radiod channel is torn down or
    created on a switch -- it is two commands to radiod.

    Every "tuned" message is a boundary marker: the browser resets its Opus
    decoder on it, which it must do anyway because the channel count can
    differ between presets. `Vfo.tune()` broadcasts that message (and
    "nosignal") to every listener queue itself, so `_commands` never sends
    one directly -- `_pump` is the sole writer on this socket, which is the
    invariant that lets two tasks share one WebSocket safely.
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
            msg = None
            try:
                # receive_json() itself can raise on bad input -- non-JSON
                # text (JSONDecodeError) or a binary frame (KeyError) -- so
                # it has to be inside the same guard as the body below. A
                # disconnect raises WebSocketDisconnect, which is not in the
                # except clause and must propagate to end the loop.
                msg = await websocket.receive_json()
                freq_hz = msg.get("tune")
                if freq_hz is None:
                    continue
                freq_hz = float(freq_hz)
                if not any(abs(f - freq_hz) < 1.0 for f in controller.monitored_freqs):
                    put_control(queue, {
                        "type": "error",
                        "message": f"{freq_hz/1e6:.3f} MHz is not currently monitored — "
                                   f"run a search first.",
                    })
                    continue
                # tune() broadcasts "tuned"/"nosignal" to every listener
                # queue itself (Vfo._broadcast_message); the return value is
                # not re-sent here to avoid a duplicate decoder-reset marker
                # and a second writer on the socket.
                await controller.vfo.tune(
                    freq_hz, controller.preset, controller.sample_rate
                )
            except (ValueError, TypeError, KeyError, AttributeError,
                    json.JSONDecodeError) as e:
                logger.debug(f"audio ws: malformed command {msg!r}: {e}")
                put_control(queue, {
                    "type": "error",
                    "message": "Malformed tune request.",
                })

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
