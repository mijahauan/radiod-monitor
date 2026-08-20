"""_apply_stations_locked's stale-channel sweep must never touch the VFO.

The sweep runs on every search and removes any channel on the SENSOR
destination that the new station set does not want. The VFO is not a station
and is never in the `desired` set the sweep computes, so if it lived on that
destination every search would remove the channel the user is actually
listening through -- and re-creating that SSRC inside radiod's ~20 s purge
window comes back dead, the exact failure this whole VFO plan exists to
eliminate (see backend/vfo.py: "created once and RETUNED, never recreated").

The protection is STRUCTURAL: the VFO has its own multicast group, so its
channel never appears in the swept set at all. There is deliberately no
`ssrc == vfo.ssrc` guard in the sweep -- the earlier one was worse than
useless, because on a shared destination the VFO's SSRC aliased exactly onto
the sensor's (`destination` is an input to allocate_ssrc) and so was in
`desired`, meaning the guard never ran in the one case it was written for.
These tests therefore assert the sweep never SEES the VFO, not that it
recognises it.

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
    anchor_ssrc = wanted_ssrc + 3       # the channel holding the front end

    controller.vfo.ssrc = vfo_ssrc
    controller.window.anchor_ssrc = anchor_ssrc

    fake_existing = {
        wanted_ssrc: FakeChannelInfo(wanted_ssrc, wanted_freq, controller.destination),
        stale_ssrc: FakeChannelInfo(stale_ssrc, 999_000_000.0, controller.destination),
        # On their OWN groups -- which is the whole protection.
        vfo_ssrc: FakeChannelInfo(vfo_ssrc, 100_000_000.0,
                                  controller.vfo_destination),
        anchor_ssrc: FakeChannelInfo(anchor_ssrc, 102_500_000.0,
                                     controller.anchor_destination),
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
    assert anchor_ssrc not in controller.control.removed, (
        "so must the anchor -- releasing it mid-listen un-centres the window"
    )
    assert wanted_ssrc not in controller.control.removed, (
        "a wanted, already-correct channel must be reused, not removed"
    )


def test_the_three_groups_are_distinct():
    """`destination` is an input to allocate_ssrc, so distinct groups are the
    mechanism that keeps the VFO's SSRC from aliasing onto a sensor's."""
    controller = _make_controller()
    groups = {controller.destination, controller.vfo_destination,
              controller.anchor_destination}
    assert len(groups) == 3
    assert controller.vfo.destination == controller.vfo_destination
    assert controller.vfo.anchor_destination == controller.anchor_destination


def test_the_vfo_cannot_alias_onto_a_sensor_ssrc():
    """The blocker, stated as a property.

    ka9q-python's create_channel() auto-allocates with allocate_ssrc(
    frequency, preset, sample_rate, agc, gain, destination, encoding,
    radiod_host) -- exactly the argument list _apply_stations_locked uses to
    compute the sensor set. Same tune, same everything but the group.
    """
    controller = _make_controller()
    freq = 162_475_000.0

    def ssrc_on(destination):
        return allocate_ssrc(
            frequency_hz=freq, preset="nfm", sample_rate=48000, agc=False,
            gain=0.0, destination=destination, encoding=Encoding.OPUS,
            radiod_host=controller.control.status_address,
        )

    assert ssrc_on(controller.destination) != ssrc_on(controller.vfo_destination), (
        "a VFO tuned to a monitored station must not BE that station's sensor"
    )
    assert ssrc_on(controller.destination) != ssrc_on(controller.anchor_destination)


def test_close_sweeps_all_three_groups(monkeypatch):
    """Anything missed here is an orphan in `control` until radiod restarts."""
    import asyncio

    controller = _make_controller()
    channels = {
        1: FakeChannelInfo(1, 146e6, controller.destination),
        2: FakeChannelInfo(2, 102.3e6, controller.vfo_destination),
        3: FakeChannelInfo(3, 102.5e6, controller.anchor_destination),
        4: FakeChannelInfo(4, 7.2e6, "239.255.255.255"),   # somebody else's
    }
    monkeypatch.setattr(rc_mod, "discover_channels", lambda host, timeout=1.0: channels)
    fake = controller.control          # close() clears the attribute
    fake.close = lambda: None
    asyncio.run(controller.close())
    assert set(fake.removed) == {1, 2, 3}
    assert controller.vfo.control is None, (
        "a VFO left holding a closed RadiodControl fails every later tune"
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
