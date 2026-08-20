"""_apply_stations_locked's stale-channel sweep must never touch the VFO.

The sweep runs on every search and removes any channel on our destination
that the new station set does not want. The VFO's channel lives on that same
destination (it shares `self.destination` with the sensor channels), but it
is not a station and is never in the `desired` set the sweep computes -- so
without an explicit exemption, every search would remove the channel the
user is actually listening through. Re-creating that SSRC inside radiod's
~20 s purge window comes back dead, which is the exact failure this whole
VFO plan exists to eliminate (see backend/vfo.py's module docstring: "created
once and RETUNED, never recreated").

No radiod: discover_channels is monkeypatched to return a fixed set of fake
channels, and a fake control object records remove_channel calls so the
sweep's decisions can be asserted directly.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.radio_controller as rc_mod
from backend.radio_controller import RadioController
from backend.sources.base import Station
from ka9q import allocate_ssrc
from ka9q.types import Encoding


class FakeChannelInfo:
    def __init__(self, ssrc, frequency, multicast_address, encoding=Encoding.OPUS):
        self.ssrc = ssrc
        self.frequency = frequency
        self.multicast_address = multicast_address
        self.encoding = encoding
        self.port = 5004
        self.snr = 10.0


class FakeControl:
    """Records remove_channel calls; status_address feeds the SSRC hash."""

    def __init__(self, status_address="fake-radiod.local"):
        self.status_address = status_address
        self.removed = []
        self.squelched = []

    def remove_channel(self, ssrc):
        self.removed.append(ssrc)

    def set_squelch(self, ssrc, **kw):
        self.squelched.append(ssrc)

    def ensure_channel(self, **kw):
        raise AssertionError(
            "ensure_channel should not be called -- the wanted station's "
            "channel is already correct in `ours` and should be reused"
        )

    def set_output_encoding(self, ssrc, enc):
        raise AssertionError("set_output_encoding implies a fresh create")


def _make_controller():
    controller = RadioController(radiod_host="fake-radiod.local")
    controller.control = FakeControl()
    return controller


def test_sweep_removes_stale_but_never_the_vfo(monkeypatch):
    controller = _make_controller()

    wanted_freq = 146_000_000.0
    preset = "nfm"
    sample_rate = 48000

    wanted_ssrc = allocate_ssrc(
        frequency_hz=wanted_freq,
        preset=preset,
        sample_rate=sample_rate,
        agc=False,
        gain=0.0,
        destination=controller.destination,
        encoding=Encoding.OPUS,
        radiod_host=controller.control.status_address,
    )
    stale_ssrc = wanted_ssrc + 1        # deliberately not in the desired set
    vfo_ssrc = wanted_ssrc + 2          # the channel the user is listening to

    controller.vfo.ssrc = vfo_ssrc

    fake_existing = {
        wanted_ssrc: FakeChannelInfo(wanted_ssrc, wanted_freq, controller.destination),
        stale_ssrc: FakeChannelInfo(stale_ssrc, 999_000_000.0, controller.destination),
        vfo_ssrc: FakeChannelInfo(vfo_ssrc, 100_000_000.0, controller.destination),
    }

    def fake_discover_channels(host, timeout=1.0):
        return fake_existing

    monkeypatch.setattr(rc_mod, "discover_channels", fake_discover_channels)

    station = Station(id="test", name="Test", freq_hz=wanted_freq, lat=0.0, lon=0.0)
    controller.apply_stations([station], preset=preset, sample_rate=sample_rate)

    assert stale_ssrc in controller.control.removed, (
        "the genuinely stale channel must be swept"
    )
    assert vfo_ssrc not in controller.control.removed, (
        "the VFO's channel must survive a search's stale sweep"
    )
    assert wanted_ssrc not in controller.control.removed, (
        "a wanted, already-correct channel must be reused, not removed"
    )


def test_sweep_is_inert_when_vfo_has_no_ssrc_yet(monkeypatch):
    """Before the first tune of the session, vfo.ssrc is None; the exemption
    check must not crash or accidentally exempt some other channel."""
    controller = _make_controller()
    assert controller.vfo.ssrc is None

    wanted_freq = 162_475_000.0
    preset = "nfm"
    sample_rate = 48000

    wanted_ssrc = allocate_ssrc(
        frequency_hz=wanted_freq,
        preset=preset,
        sample_rate=sample_rate,
        agc=False,
        gain=0.0,
        destination=controller.destination,
        encoding=Encoding.OPUS,
        radiod_host=controller.control.status_address,
    )
    stale_ssrc = wanted_ssrc + 1

    fake_existing = {
        wanted_ssrc: FakeChannelInfo(wanted_ssrc, wanted_freq, controller.destination),
        stale_ssrc: FakeChannelInfo(stale_ssrc, 555_000_000.0, controller.destination),
    }

    monkeypatch.setattr(rc_mod, "discover_channels", lambda host, timeout=1.0: fake_existing)

    station = Station(id="test", name="Test", freq_hz=wanted_freq, lat=0.0, lon=0.0)
    controller.apply_stations([station], preset=preset, sample_rate=sample_rate)

    assert controller.control.removed == [stale_ssrc]
