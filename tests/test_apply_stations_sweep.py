"""apply_stations creates nothing. The VFO is the only channel this app makes.

Building one channel per station so the map could show live SNR was never
viable on a 660 kHz window for a 20 MHz band, and measurement showed it was
harmful even when the set did fit: while any channel is live elsewhere in the
spectrum, the front end cannot be placed for the station the listener picked.
Measured, one channel each --

    nfm 162.400 alone                  LO 162.4000  snr 8.51  251 frames
    fresh wfm 91.300, nfm still alive  LO  91.5192  snr None    0 frames

-- 91.5192 being 219.2 kHz off target, radiod having edge-parked the wfm
channel rather than centring it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.radio_controller as rc


class FakeChannel:
    def __init__(self, addr, freq=1.0e6):
        self.multicast_address = addr
        self.frequency = freq


class FakeControl:
    def __init__(self):
        self.removed = []
        self.created = []
        self.status_address = "fake-radiod.local"

    def remove_channel(self, ssrc):
        self.removed.append(ssrc)

    def ensure_channel(self, **kw):
        self.created.append(kw)
        raise AssertionError("apply_stations must not create channels")

    def create_channel(self, **kw):
        self.created.append(kw)
        raise AssertionError("apply_stations must not create channels")


class Station:
    def __init__(self, freq):
        self.freq_hz = freq


def _controller(monkeypatch, discovered):
    monkeypatch.setattr(rc, "discover_channels", lambda *a, **k: discovered)
    c = rc.RadioController.__new__(rc.RadioController)
    c.control = FakeControl()
    c.radiod_host = "fake-radiod.local"
    c.destination = "239.1.2.3"
    c.vfo_destination = "239.1.2.4"
    c.anchor_destination = "239.1.2.5"
    c.active_channels = {}
    c.monitored_freqs = set()
    c.activity_available = True
    c.sample_rate = 48000
    c.preset = "nfm"
    c.audio_channels = 1
    c.snr_squelch_enabled = True
    import threading
    c._apply_lock = threading.Lock()
    return c


def test_apply_stations_creates_no_channels(monkeypatch):
    c = _controller(monkeypatch, {})
    c.apply_stations([Station(162_400_000.0), Station(162_450_000.0)], "nfm")
    assert c.control.created == [], "the VFO is the only channel"
    assert c.monitored_freqs == {162_400_000.0, 162_450_000.0}
    assert c.activity_available is False, (
        "no sensor channels means no live SNR to report"
    )


def test_a_leftover_sensor_channel_is_swept(monkeypatch):
    """A previous version of this app may have left one, and one live channel
    in another band holds the front end there. Swept once at connect, with a
    generous listen: discover_channels is a fixed-duration listen for status
    multicast and a 1.0 s window logged nothing while live nfm channels at
    162.4-162.55 sat there reading 8-9 dB."""
    discovered = {
        0xAAAA: FakeChannel("239.1.2.3", 162_400_000.0),   # sensor group
        0xBBBB: FakeChannel("239.1.2.4", 91_300_000.0),    # the VFO's own
        0xCCCC: FakeChannel("239.9.9.9", 10_000_000.0),    # someone else's
    }
    c = _controller(monkeypatch, discovered)
    c._sweep_sensor_group(listen=0.0)
    assert 0xAAAA in c.control.removed, "the leftover sensor goes"
    assert 0xBBBB not in c.control.removed, "the VFO's channel is not ours to sweep"
    assert 0xCCCC not in c.control.removed, "another client's channel is untouched"


def test_removals_callback_fires_even_with_no_connection(monkeypatch):
    """The audio path waits on this; it must never be left hanging."""
    c = _controller(monkeypatch, {})
    c.control = None
    fired = []
    c.apply_stations([Station(162_400_000.0)], "nfm",
                     on_removals_done=lambda: fired.append(True))
    assert fired == [True]
