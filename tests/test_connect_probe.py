"""RadioController.connect() must probe the front end on the ANCHOR group.

The probe channel `FrontEndWindow.probe()` creates is throwaway but not
free: `remove_channel` is not instantaneous, so the removed channel lingers
in discovery for a moment. On the VFO's group that lingering channel is
exactly what the VFO's adopt-an-existing-channel scan would grab (see
backend/vfo.py's `_ensure_channel_exists`); on the sensor group it would
show up as a phantom station. `radio_controller.py`'s `connect()` therefore
passes `self.anchor_destination`, not `self.destination` or
`self.vfo_destination`, to `window.probe`.

Prior to this test that call site was asserted only by inspection: reverting
it to any other destination still passed the full suite. This test fails on
that revert because it drives `connect()` for real (with `RadiodControl`
replaced by a fake control that never touches the network) and asserts the
destination the fake control's `ensure_channel` actually received.
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.radio_controller as rc_mod
from backend.radio_controller import RadioController


class FakeChannel:
    def __init__(self, ssrc):
        self.ssrc = ssrc


class FakeControl:
    """No sockets, no radiod -- records what connect() -> probe() sends it."""

    def __init__(self, status_address="fake-radiod.local"):
        self.status_address = status_address
        self.ensure_channel_calls = []

    def ensure_channel(self, **kw):
        self.ensure_channel_calls.append(kw)
        return FakeChannel(ssrc=0xABCD)

    def poll_status(self, ssrc, timeout=2.0):
        return None  # no FE_LOW_EDGE/FE_HIGH_EDGE -- probe() tolerates this

    def remove_channel(self, ssrc):
        pass


def test_connect_probes_the_anchor_group_not_the_sensor_or_vfo_group(monkeypatch):
    controller = RadioController(radiod_host="fake-radiod.local")
    fake_control = FakeControl()
    monkeypatch.setattr(rc_mod, "RadiodControl", lambda host: fake_control)

    asyncio.run(controller.connect())

    calls = fake_control.ensure_channel_calls
    assert len(calls) == 1, "connect() probes the front end exactly once"
    destination = calls[0].get("destination")
    assert destination == controller.anchor_destination
    assert destination != controller.destination
    assert destination != controller.vfo_destination
