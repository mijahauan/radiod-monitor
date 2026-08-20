"""The VFO's identity, ownership, and tuning order.

No radiod: a fake control records the commands issued so the ORDER can be
asserted. Order is the whole point -- a demodulator that starts while parked
at the window edge does not recover when the window later moves onto it, so
the window must be centred BEFORE the frequency is set.
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.vfo as vfo_mod
from backend.vfo import Vfo


class FakeChannelInfo:
    def __init__(self, ssrc, freq=1.0e6, multicast_address="239.0.0.1"):
        self.ssrc = ssrc
        self.frequency = freq
        self.multicast_address = multicast_address
        self.port = 5004
        self.sample_rate = 48000


class FakeControl:
    """Records calls. `known` is the set of SSRCs radiod admits to having."""

    def __init__(self, known=()):
        self.calls = []
        self.status_address = "fake-radiod.local"
        self.known = set(known)
        self._next_ssrc = 0x1234

    def create_channel(self, **kw):
        assert kw.get("ssrc") is None or "ssrc" not in kw, (
            "the app must not choose the SSRC -- let the library allocate it"
        )
        ssrc = self._next_ssrc
        self._next_ssrc += 1
        self.known.add(ssrc)
        self.calls.append(("create_channel", kw.get("frequency_hz"), kw.get("preset"),
                           kw.get("destination")))
        return ssrc

    def poll_channel(self, ssrc, **kw):
        self.calls.append(("poll_channel", ssrc))
        return FakeChannelInfo(ssrc) if ssrc in self.known else None

    def set_frequency(self, ssrc, hz):
        self.calls.append(("set_frequency", hz))

    def set_preset(self, ssrc, preset):
        self.calls.append(("set_preset", preset))

    def set_output_encoding(self, ssrc, enc):
        self.calls.append(("set_output_encoding", enc))

    def set_squelch(self, ssrc, **kw):
        self.calls.append(("set_squelch",))

    def remove_channel(self, ssrc):
        self.calls.append(("remove_channel", ssrc))


class FakeWindow:
    def __init__(self):
        self.low_edge_hz, self.high_edge_hz = -330_240.0, 330_240.0
        self.usable_bw_hz = 660_480.0
        self.anchor_ssrc = None

    def centre_on(self, control, freq_hz, destination, sample_rate, ssrc_hint=None):
        control.calls.append(("centre_on", freq_hz, destination))
        return True

    def release(self, control):
        control.calls.append(("release",))


class FakeVfo(Vfo):
    """A Vfo whose stream is a stand-in and whose RTP always arrives."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.started = 0

    async def _start_stream(self):
        self.started += 1
        self._stream = "fake-stream"

    async def _settle(self):
        self._frames_seen = 1  # pretend RTP followed the retune


class SilentVfo(FakeVfo):
    """A Vfo whose stream never delivers RTP -- exercises the nosignal path."""

    async def _settle(self):
        pass  # no RTP arrives; _frames_seen stays whatever tune() zeroed it to


def make():
    c = FakeControl()
    v = FakeVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                anchor_destination="239.9.9.9", settle_sec=0.01)
    return c, v


def test_the_app_never_computes_an_ssrc():
    src = open(os.path.join(os.path.dirname(vfo_mod.__file__), "vfo.py")).read()
    assert "allocate_ssrc" not in src, (
        "the SSRC is the library's business; the app holds the handle it returns"
    )


def test_the_ssrc_is_the_one_the_library_allocated(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    created = [x for x in c.calls if x[0] == "create_channel"]
    assert len(created) == 1
    assert v.ssrc in c.known


def test_tune_centres_the_window_before_setting_frequency(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    names = [x[0] for x in c.calls]
    assert "centre_on" in names and "set_frequency" in names
    assert names.index("centre_on") < names.index("set_frequency"), (
        "a demod that starts at the window edge never recovers"
    )


def test_anchor_and_vfo_use_different_destinations(monkeypatch):
    """The anchor carries no audio; adopting it as the VFO breaks centring."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    centre_calls = [x for x in c.calls if x[0] == "centre_on"]
    assert len(centre_calls) == 1
    assert centre_calls[0][2] == v.anchor_destination
    created = [x for x in c.calls if x[0] == "create_channel"]
    assert len(created) == 1
    assert created[0][3] == v.destination
    assert v.anchor_destination != v.destination


def test_second_tune_does_not_create_a_channel(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert not any(x[0] == "create_channel" for x in c.calls), (
        "the VFO is retuned, never recreated"
    )
    assert v.started == 1, "and its stream follows the SSRC, so it is not restarted"


def test_preset_is_sent_only_when_it_changes(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert not any(x[0] == "set_preset" for x in c.calls)
    c.calls.clear()
    asyncio.run(v.tune(162_450_000.0, "nfm", 48000))
    assert any(x[0] == "set_preset" for x in c.calls)


def test_tune_never_removes_the_vfo_channel(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert not any(x[0] == "remove_channel" for x in c.calls), (
        "removing it starts a ~20s purge; re-creating inside that window "
        "yields a dead channel"
    )


def test_an_existing_channel_on_our_destination_is_adopted(monkeypatch):
    """Surviving a restart of THIS app, not of radiod."""
    left_behind = FakeChannelInfo(0x9999, multicast_address="239.1.2.3")
    monkeypatch.setattr(vfo_mod, "discover_channels",
                        lambda *a, **k: {left_behind.ssrc: left_behind})
    c = FakeControl(known={0x9999})
    v = FakeVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                anchor_destination="239.9.9.9", settle_sec=0.01)
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert v.ssrc == 0x9999
    assert not any(x[0] == "create_channel" for x in c.calls), (
        "a channel already on our destination IS the VFO -- adopt it"
    )


def test_a_channel_radiod_has_forgotten_is_recreated(monkeypatch):
    """Surviving a restart of radiod."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    first = v.ssrc
    c.known.clear()          # radiod restarted; our channel is gone
    c.calls.clear()
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    assert any(x[0] == "create_channel" for x in c.calls)
    assert v.ssrc != first
    assert v.started == 2, "a new channel means a new stream to follow it"


def test_tune_reports_nosignal_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c = FakeControl()
    v = SilentVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                  anchor_destination="239.9.9.9", settle_sec=0.01)
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert result == {"type": "nosignal", "freq_hz": 102_300_000.0}
    tune_attempts = [x for x in c.calls if x[0] == "set_frequency"]
    assert len(tune_attempts) == vfo_mod.MAX_TUNE_ATTEMPTS, (
        "every attempt must retune in place, never tear down the channel"
    )
    assert not any(x[0] == "remove_channel" for x in c.calls)


def test_channels_reset_on_retune_so_a_stale_count_cannot_survive(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    v.channels = 2  # pretend the first station was stereo
    asyncio.run(v.tune(91_300_000.0, "wfm", 48000))
    # FakeVfo never calls _broadcast, so a fresh count is only re-derived once
    # a real frame arrives -- what matters here is that the stale stereo
    # count from the old station does not survive the retune.
    assert v.channels is None


def test_opus_channels_reads_the_toc_byte():
    assert vfo_mod.opus_channels(b"") is None
    assert vfo_mod.opus_channels(bytes([0b00000100])) == 2
    assert vfo_mod.opus_channels(bytes([0b00000000])) == 1


def test_broadcast_drops_oversized_payload_and_logs_once(caplog):
    c, v = make()
    q = asyncio.Queue()
    v.listeners.append(q)
    oversized = b"x" * (vfo_mod.MAX_OPUS_FRAME_BYTES + 1)
    with caplog.at_level("ERROR"):
        v._broadcast([oversized, oversized])
    assert q.empty(), "an oversized payload is PCM, not Opus -- never forward it"
    assert v._non_opus_warned
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, "warn once per stream, not once per dropped frame"


def test_broadcast_fans_out_frames_only_no_header():
    """_broadcast latches self.channels from the TOC byte but no longer
    emits a "tuned" header itself -- tune() is the sole source of that
    message now, so two listeners retuning would otherwise each see it
    twice (see test_tune_broadcasts_exactly_one_tuned_message_per_listener).
    """
    c, v = make()
    v.freq_hz = 102_300_000.0
    q1, q2 = asyncio.Queue(), asyncio.Queue()
    v.listeners.extend([q1, q2])
    mono_frame = bytes([0b00000000])
    v._broadcast([mono_frame, mono_frame])

    def drain(q):
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items1, items2 = drain(q1), drain(q2)
    assert items1 == [mono_frame, mono_frame]
    assert items2 == [mono_frame, mono_frame]
    assert v.channels == 1, "still latched from the TOC byte"


def test_tune_broadcasts_exactly_one_tuned_message_per_listener(monkeypatch):
    """Fix for the duplicate-boundary-marker bug: tune() must be the sole
    source of the "tuned"/"nosignal" message, and every attached listener
    -- not just the caller of tune() -- must receive exactly one."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    q1, q2 = asyncio.Queue(), asyncio.Queue()
    v.listeners.extend([q1, q2])
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert result == {"type": "tuned", "freq_hz": 102_300_000.0, "channels": 1}

    def drain(q):
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items1, items2 = drain(q1), drain(q2)
    assert items1 == [result]
    assert items2 == [result]


def test_tune_broadcasts_nosignal_to_every_listener(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c = FakeControl()
    v = SilentVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                  anchor_destination="239.9.9.9", settle_sec=0.01)
    q = asyncio.Queue()
    v.listeners.append(q)
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert result == {"type": "nosignal", "freq_hz": 102_300_000.0}
    assert q.get_nowait() == result
    assert q.empty()


def test_remove_listener_keeps_running_while_listeners_remain():
    c, v = make()
    q1, q2 = asyncio.Queue(), asyncio.Queue()
    v.listeners.extend([q1, q2])
    result = asyncio.run(v.remove_listener(q1))
    assert result is False
    assert v.listeners == [q2]


def test_remove_listener_stops_on_the_last_one():
    c, v = make()
    q = asyncio.Queue()
    v.listeners.append(q)
    v._stream = "fake-stream"
    result = asyncio.run(v.remove_listener(q))
    assert result is True
    assert v.listeners == []


def test_stop_releases_the_window_anchor():
    c, v = make()
    asyncio.run(v.stop())
    assert ("release",) in c.calls
