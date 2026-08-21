"""The audio WebSocket and the search handler, with no radiod anywhere.

`backend.app` holds its RadioController in a module global, so these tests
swap in a fake before FastAPI's lifespan runs. Nothing here opens a socket to
a radio: the fake VFO fabricates the RTP the real one waits for.

What is worth testing here is the seams the per-module tests cannot see --
that a malformed command does not close the socket, that a control message
survives the trip, and that a search publishes what a Listen click is
validated against *before* the browser is told the stations exist.
"""
import asyncio
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import backend.app as app_mod
import backend.vfo as vfo_mod
from backend.vfo import Vfo

MONO_FRAME = bytes([0b00000000]) + b"opus"


class FakeWindow:
    def __init__(self):
        self.low_edge_hz, self.high_edge_hz = -330_240.0, 330_240.0
        self.usable_bw_hz = 660_480.0
        self.anchor_ssrc = None
        self.released = 0

    def centre_on(self, control, freq_hz, destination, sample_rate, ssrc_hint=None):
        return True

    def read(self, control, ssrc_hint):
        return None

    def release(self, control):
        self.released += 1


class FakeControl:
    status_address = "fake-radiod.local"

    def create_channel(self, **kw):
        return 0x4242

    def poll_channel(self, ssrc, **kw):
        return object()

    def set_frequency(self, ssrc, hz):
        pass

    def set_preset(self, ssrc, preset):
        pass

    def set_output_encoding(self, ssrc, enc):
        pass

    def set_squelch(self, ssrc, **kw):
        pass

    def remove_channel(self, ssrc):
        pass


class FakeVfo(Vfo):
    """RTP arrives the moment we settle, which is what the real one waits for."""

    async def _start_stream(self):
        self._stream = "fake-stream"

    async def _settle(self):
        self._broadcast([MONO_FRAME])


class FakeController:
    """Only the surface /ws/audio and _handle_search actually touch."""

    def __init__(self):
        self.radiod_host = "fake-radiod.local"
        self.control = FakeControl()
        self.window = FakeWindow()
        self.active_channels = {}
        self.monitored_freqs = set()
        self.preset = "nfm"
        self.sample_rate = 48000
        self.applied = []
        self.converge_task = None
        self.squelch = []
        self.vfo = FakeVfo(control=self.control, window=self.window,
                           destination="239.1.2.3", settle_sec=0.0)

    async def connect(self):
        pass

    async def close(self):
        pass

    def fits_window(self, freqs):
        return True

    def set_squelch(self, db):
        self.squelch.append(db)

    def apply_stations(self, stations, preset, audio_channels, snr_squelch,
                       sample_rate, on_removals_done=None):
        if on_removals_done is not None:
            on_removals_done()
        self.applied.append((preset, sample_rate))
        self.monitored_freqs = {float(s.freq_hz) for s in stations}


@pytest.fixture
def fake(monkeypatch):
    # The VFO's adopt scan is the one place left that would reach the network
    # (discover_channels listens on the status multicast group). Stub it: no
    # test here may contact a radio.
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    controller = FakeController()
    monkeypatch.setattr(app_mod, "controller", controller)
    return controller


def _drain(ws, count):
    """Read `count` raw messages; return (json items, binary items)."""
    dicts, blobs = [], []
    for _ in range(count):
        msg = ws.receive()
        if msg.get("text") is not None:
            import json
            dicts.append(json.loads(msg["text"]))
        elif msg.get("bytes") is not None:
            blobs.append(msg["bytes"])
    return dicts, blobs


# ---------------------------------------------------------------------------
# /ws/audio
# ---------------------------------------------------------------------------
def test_tune_yields_a_tuned_message_and_audio(fake):
    with TestClient(app_mod.app) as client:
        fake.monitored_freqs = {162_475_000.0}
        with client.websocket_connect("/ws/audio") as ws:
            ws.send_json({"tune": 162_475_000.0})
            dicts, blobs = _drain(ws, 2)
    assert dicts == [{"type": "tuned", "freq_hz": 162_475_000.0, "channels": 1}]
    assert blobs == [MONO_FRAME]
    assert fake.vfo.ssrc == 0x4242, "the SSRC is whatever the library allocated"


def test_a_malformed_command_errors_without_closing_the_socket(fake):
    with TestClient(app_mod.app) as client:
        fake.monitored_freqs = {162_475_000.0}
        with client.websocket_connect("/ws/audio") as ws:
            ws.send_text("this is not json")
            first = ws.receive_json()
            # ...and the socket is still usable afterwards.
            ws.send_json({"tune": 162_475_000.0})
            dicts, blobs = _drain(ws, 2)
    assert first == {"type": "error", "message": "Malformed tune request."}
    assert {"type": "tuned", "freq_hz": 162_475_000.0, "channels": 1} in dicts


def test_an_unmonitored_frequency_is_refused_not_tuned(fake):
    with TestClient(app_mod.app) as client:
        fake.monitored_freqs = {162_475_000.0}
        with client.websocket_connect("/ws/audio") as ws:
            ws.send_json({"tune": 7_200_000.0})
            msg = ws.receive_json()
            # Checked inside the socket's lifetime: closing it drops the last
            # listener, which clears freq_hz whether or not a tune happened.
            assert fake.vfo.freq_hz is None, "nothing was tuned"
            assert fake.vfo.ssrc is None, "and no channel was created"
    assert msg["type"] == "error"
    assert "not currently monitored" in msg["message"]


def test_a_late_joiner_is_handed_the_current_station(fake):
    with TestClient(app_mod.app) as client:
        fake.monitored_freqs = {162_475_000.0}
        with client.websocket_connect("/ws/audio") as first:
            first.send_json({"tune": 162_475_000.0})
            _drain(first, 2)
            with client.websocket_connect("/ws/audio") as second:
                joined = second.receive_json()
    assert joined == {"type": "tuned", "freq_hz": 162_475_000.0, "channels": 1}


# ---------------------------------------------------------------------------
# _handle_search
# ---------------------------------------------------------------------------
class FakeWebSocket:
    """Records what the browser would have received, and when."""

    def __init__(self, controller):
        self.controller = controller
        self.sent = []
        self.state_at_send = []

    async def send_json(self, msg):
        self.sent.append(msg)
        self.state_at_send.append(
            (set(self.controller.monitored_freqs), self.controller.preset)
        )


async def _run_search(controller, data):
    ws = FakeWebSocket(controller)
    await app_mod._handle_search(ws, data)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)
    return ws


def test_a_listen_click_racing_the_results_message_is_not_rejected(fake):
    """monitored_freqs and preset must be published BEFORE the results reach
    the browser: the browser offers the station the instant results land, and
    _converge runs in a worker thread behind a lock a previous search may
    still hold."""
    ws = asyncio.run(_run_search(fake, {
        "mode": "nws", "location": "38.9,-92.3", "radius": 500, "squelch": 10,
    }))
    freqs_when_sent, preset_when_sent = ws.state_at_send[0]
    assert freqs_when_sent, "the audio plane had nothing to validate against"
    assert preset_when_sent == "nfm"
    station_freqs = {s["freq_hz"] for s in ws.sent[0]["stations"]}
    assert freqs_when_sent == station_freqs, (
        "every station the browser can click must already be tunable"
    )


def test_a_mode_switch_tells_a_listener_their_station_is_gone(fake):
    """Rebuilding sensors on a new band moves the front end. Without this the
    audio just stops -- no nosignal, no UI change, nothing above DEBUG."""
    q: asyncio.Queue = asyncio.Queue()

    async def scenario():
        fake.vfo.listeners.append(q)
        fake.vfo.freq_hz = 102_300_000.0     # listening to an FM station
        await _run_search(fake, {
            "mode": "nws", "location": "38.9,-92.3", "radius": 500,
            "squelch": 10,
        })

    asyncio.run(scenario())
    msg = q.get_nowait()
    assert msg == {"type": "nosignal", "freq_hz": 102_300_000.0,
                   "reason": "station is not in the current search"}
    assert fake.vfo.freq_hz is None, "the VFO stopped"


def test_a_listener_on_a_station_still_in_the_search_is_left_alone(fake):
    q: asyncio.Queue = asyncio.Queue()

    async def scenario():
        fake.vfo.listeners.append(q)
        ws = await _run_search(fake, {
            "mode": "nws", "location": "38.9,-92.3", "radius": 500,
            "squelch": 10,
        })
        # Pretend the user was already listening to one of the results.
        return ws

    ws = asyncio.run(scenario())
    surviving = ws.sent[0]["stations"][0]["freq_hz"]

    async def scenario2():
        fake.vfo.freq_hz = surviving
        await _run_search(fake, {
            "mode": "nws", "location": "38.9,-92.3", "radius": 500,
            "squelch": 10,
        })

    asyncio.run(scenario2())
    assert q.empty(), "a re-search must not cut off audio it still covers"
    assert fake.vfo.freq_hz == surviving
