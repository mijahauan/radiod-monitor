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

    def __init__(self, known=(), raise_on=None):
        self.calls = []
        self.status_address = "fake-radiod.local"
        self.known = set(known)
        self._next_ssrc = 0x1234
        # Name of a command that should blow up the way a closed control
        # socket does ("Not connected to radiod" is a RuntimeError).
        self.raise_on = raise_on

    def _maybe_raise(self, name):
        if self.raise_on == name:
            self.calls.append((f"{name}:raised",))
            raise RuntimeError("Not connected to radiod")

    def create_channel(self, **kw):
        self._maybe_raise("create_channel")
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
        self._maybe_raise("set_frequency")
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
    def __init__(self, centres=True):
        self.low_edge_hz, self.high_edge_hz = -330_240.0, 330_240.0
        self.usable_bw_hz = 660_480.0
        self.anchor_ssrc = None
        self.centres = centres

    def centre_on(self, control, freq_hz, destination, sample_rate, ssrc_hint=None):
        control.calls.append(("centre_on", freq_hz, destination))
        return self.centres

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
    """A lint, deliberately kept -- and deliberately not trusted alone.

    It guards Global Constraint 3 for the cost of a string search, but it is
    blind to the failure that actually happened: the library computes the very
    same hash inside create_channel(), so the VFO can collide with a sensor
    without the word `allocate_ssrc` ever appearing here. The behavioural half
    is test_the_ssrc_is_the_one_the_library_allocated below, and the collision
    half is in tests/test_apply_stations_sweep.py.
    """
    src = open(os.path.join(os.path.dirname(vfo_mod.__file__), "vfo.py")).read()
    assert "allocate_ssrc" not in src, (
        "the SSRC is the library's business; the app holds the handle it returns"
    )


def test_the_ssrc_is_the_one_the_library_allocated(monkeypatch):
    """Behavioural counterpart to the grep above: the SSRC the VFO ends up
    holding is exactly the integer create_channel() handed back, not something
    derived from the tune."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c = FakeControl()
    allocated = []
    real_create = c.create_channel

    def spy(**kw):
        ssrc = real_create(**kw)
        allocated.append(ssrc)
        return ssrc

    c.create_channel = spy
    v = FakeVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                anchor_destination="239.9.9.9", settle_sec=0.01)
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    created = [x for x in c.calls if x[0] == "create_channel"]
    assert len(created) == 1
    assert allocated == [v.ssrc], (
        "the VFO holds the library's handle verbatim"
    )
    assert v.ssrc in c.known


def test_tune_centres_the_window_after_setting_frequency(monkeypatch):
    """Centre LAST. Measured on the HF+ retuning 162.400 nfm -> 102.300 wfm,
    three runs each: centring first gives IF -219.2 kHz every time (radiod's
    edge-parking position for wfm), centring last gives IF +0.0 kHz every
    time. set_preset restarts the demod and wfm.c re-runs set_freq at demod
    start, which re-parks the channel and drags the LO off whatever we had
    centred; nothing re-runs set_freq after the preset, so centring there
    holds."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    names = [x[0] for x in c.calls]
    assert "centre_on" in names and "set_frequency" in names
    assert names.index("set_frequency") < names.index("centre_on"), (
        "centring before the preset restart does not survive it"
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


def test_an_existing_channel_on_the_vfo_destination_is_adopted(monkeypatch):
    """Surviving a restart of THIS app, not of radiod.

    The VFO's group holds the VFO and nothing else, so a channel found there
    IS the VFO and adopting it beats removing and re-creating (which starts
    radiod's ~20 s purge).
    """
    left_behind = FakeChannelInfo(0x9999, multicast_address="239.1.2.3")
    monkeypatch.setattr(vfo_mod, "discover_channels",
                        lambda *a, **k: {left_behind.ssrc: left_behind})
    c = FakeControl(known={0x9999})
    v = FakeVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                anchor_destination="239.9.9.9", settle_sec=0.01)
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert v.ssrc == 0x9999
    assert not any(x[0] == "create_channel" for x in c.calls), (
        "a channel already on the VFO's destination IS the VFO -- adopt it"
    )


def test_channels_on_other_groups_are_never_adopted(monkeypatch):
    """The blocker, from the VFO's side.

    Sensors, the anchor, and the front-end probe's throwaway channel all used
    to sit on the group this scan reads. `existing[0]` is dict-ordered, so the
    VFO adopted an arbitrary one of them and retuned it -- or, just after
    startup, adopted the probe channel radiod was still purging, which is a
    dead channel that produces no audio.
    """
    sensor = FakeChannelInfo(0x1111, multicast_address="239.1.2.99")
    anchor = FakeChannelInfo(0x2222, multicast_address="239.9.9.9")
    probe = FakeChannelInfo(0x3333, multicast_address="239.9.9.9")
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {
        ch.ssrc: ch for ch in (sensor, anchor, probe)
    })
    c, v = make()          # VFO on 239.1.2.3; nothing of ours is there
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert v.ssrc not in (0x1111, 0x2222, 0x3333)
    created = [x for x in c.calls if x[0] == "create_channel"]
    assert len(created) == 1, "nothing on our group -- create one"
    assert created[0][3] == "239.1.2.3"


def test_a_dying_channel_on_our_group_is_skipped_for_a_live_one(monkeypatch):
    """radiod reaps a channel only once its frequency reads zero, so a
    channel mid-purge on our OWN destination -- an app restart landing
    inside the ~20 s window close() starts -- still answers discovery.
    Adopting it hands the VFO a corpse, the exact failure this design
    exists to remove. A live channel on the same group must win instead."""
    dying = FakeChannelInfo(0x4444, freq=0.0, multicast_address="239.1.2.3")
    live = FakeChannelInfo(0x5555, freq=91_300_000.0, multicast_address="239.1.2.3")
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {
        ch.ssrc: ch for ch in (dying, live)
    })
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert v.ssrc == 0x5555, "the live channel must be adopted, not the corpse"
    assert not any(x[0] == "create_channel" for x in c.calls)


def test_only_a_dying_channel_on_our_group_forces_a_fresh_create(monkeypatch):
    """No live candidate on our group -- the zero-frequency one must be
    passed over entirely, and a new channel created rather than adopting a
    channel radiod is still tearing down."""
    dying = FakeChannelInfo(0x4444, freq=0.0, multicast_address="239.1.2.3")
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {
        dying.ssrc: dying
    })
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert v.ssrc != 0x4444
    created = [x for x in c.calls if x[0] == "create_channel"]
    assert len(created) == 1, "the only candidate was dying -- create fresh"
    assert created[0][3] == "239.1.2.3"


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


# ---------------------------------------------------------------------------
# Tuning ORDER. Both of these fail against the shipped implementation, which
# created the channel before centring and sent set_preset before
# set_frequency. Neither shows up as an error: a wfm demod started at the
# window edge produces plausible fragments of audio, which is exactly the
# failure mode this project has already been burned by.
# ---------------------------------------------------------------------------
def test_the_window_is_centred_after_the_channel_exists(monkeypatch):
    """The anchor's whole purpose is to rescue a channel radiod has ALREADY
    parked at the window edge, by moving the LO onto it so every channel
    recalculates its IF against the new LO. CLAUDE.md's original measurement
    (IF +219.2 / snr -inf / ~17 frames per 10 s before the anchor, IF +0.0 /
    snr 6.5 / 500 frames after) is a description of exactly this order."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    names = [x[0] for x in c.calls]
    assert names.index("create_channel") < names.index("centre_on")


def test_frequency_is_set_before_the_preset_restarts_the_demod(monkeypatch):
    """A preset command restarts the demodulator, and wfm.c re-runs set_freq
    at demod start. Restart it first and radiod re-places the channel from the
    PREVIOUS station's frequency, dragging the LO off what we just centred."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    c.calls.clear()
    asyncio.run(v.tune(162_450_000.0, "nfm", 48000))   # preset changes
    names = [x[0] for x in c.calls]
    assert "set_preset" in names, "a mode change must restart the demod"
    assert names.index("set_frequency") < names.index("set_preset"), (
        "restarting the demod on a stale frequency un-centres the window"
    )


def test_the_full_tune_order_is_create_frequency_preset_then_centre(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    names = [x[0] for x in c.calls]
    ordered = [n for n in names
               if n in ("centre_on", "create_channel", "set_frequency",
                        "set_output_encoding")]
    assert ordered == ["create_channel", "set_frequency",
                       "set_output_encoding", "centre_on"]


# ---------------------------------------------------------------------------
# radiod-side failures during a tune
# ---------------------------------------------------------------------------
def test_a_radiod_error_during_a_tune_becomes_nosignal(monkeypatch):
    """It must not propagate: the caller is one of two tasks sharing the audio
    WebSocket, so an escaping exception tears the socket down and the browser
    shows "audio connection lost" with no reason."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c = FakeControl(raise_on="create_channel")
    v = FakeVfo(control=c, window=FakeWindow(), destination="239.1.2.3",
                anchor_destination="239.9.9.9", settle_sec=0.01)
    q = asyncio.Queue()
    v.listeners.append(q)
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert result["type"] == "nosignal"
    assert result["freq_hz"] == 102_300_000.0
    assert "Not connected to radiod" in result["reason"]
    assert q.get_nowait() == result
    assert [x for x in c.calls if x[0] == "centre_on"] == [], (
        "the channel never came up, so there is nothing to centre on"
    )
    assert len([x for x in c.calls if x[0] == "create_channel:raised"]) == 1, (
        "reported and stopped -- not retried in a loop"
    )


def test_a_tune_with_no_control_connection_is_reported_not_crashed():
    """close() clears vfo.control. Without the guard this AttributeError'd and
    the user was told "Malformed tune request." -- a lie."""
    v = FakeVfo(control=None, window=FakeWindow(), destination="239.1.2.3",
                anchor_destination="239.9.9.9", settle_sec=0.01)
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert result["type"] == "nosignal"
    assert "radiod" in result["reason"]


def test_a_failure_to_centre_is_reported_as_the_reason(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c = FakeControl()
    v = SilentVfo(control=c, window=FakeWindow(centres=False),
                  destination="239.1.2.3", anchor_destination="239.9.9.9",
                  settle_sec=0.01)
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert result == {"type": "nosignal", "freq_hz": 102_300_000.0,
                      "reason": "could not centre the receiver's window"}


def test_a_centred_tune_that_hears_nothing_has_no_reason(monkeypatch):
    """"No signal" and "we could not point the radio at it" are different
    things and must stay distinguishable."""
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c = FakeControl()
    v = SilentVfo(control=c, window=FakeWindow(centres=True),
                  destination="239.1.2.3", anchor_destination="239.9.9.9",
                  settle_sec=0.01)
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    assert "reason" not in result


# ---------------------------------------------------------------------------
# Control messages vs. the frame queue
# ---------------------------------------------------------------------------
def test_a_control_message_displaces_audio_rather_than_being_dropped():
    """frontend/app.js ignores every frame until a "tuned" configures its
    decoder, so a dropped "tuned" is permanent silence; a dropped Opus frame
    is 20 ms nobody notices."""
    q = asyncio.Queue(maxsize=3)
    for _ in range(3):
        q.put_nowait(b"frame")
    msg = {"type": "tuned", "freq_hz": 102_300_000.0, "channels": 1}
    vfo_mod.put_control(q, msg)
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert msg in items
    assert len(items) == 3, "one frame was displaced, not appended"


def test_put_control_never_displaces_an_earlier_control_message():
    """The bug: displacing the queue HEAD can throw away an earlier control
    dict rather than an audio frame -- e.g. a "tuned" that a slow consumer
    hasn't drained yet -- leaving the browser ignoring every frame that
    follows because it never got the header it needed. Both control
    messages must survive; only a frame may be sacrificed."""
    q = asyncio.Queue(maxsize=4)
    early = {"type": "tuned", "freq_hz": 102_300_000.0, "channels": 1}
    q.put_nowait(early)          # an earlier control message, already queued
    q.put_nowait(b"f1")
    q.put_nowait(b"f2")
    q.put_nowait(b"f3")          # queue is now full

    new = {"type": "nosignal", "freq_hz": 91_300_000.0}
    vfo_mod.put_control(q, new)

    items = []
    while not q.empty():
        items.append(q.get_nowait())

    assert early in items, "the earlier control message must survive"
    assert new in items, "the new control message must be enqueued"
    frames = [x for x in items if isinstance(x, bytes)]
    assert len(frames) == 2, "exactly one frame was displaced to make room"
    assert len(items) == 4, "bounded by the queue's maxsize"


def test_put_control_leaves_a_roomy_queue_alone():
    q = asyncio.Queue(maxsize=10)
    q.put_nowait(b"frame")
    msg = {"type": "tuned"}
    vfo_mod.put_control(q, msg)
    assert q.get_nowait() == b"frame"
    assert q.get_nowait() == msg


def test_tuned_reaches_a_listener_whose_queue_is_full(monkeypatch):
    monkeypatch.setattr(vfo_mod, "discover_channels", lambda *a, **k: {})
    c, v = make()
    q = asyncio.Queue(maxsize=2)
    q.put_nowait(b"a")
    q.put_nowait(b"b")
    v.listeners.append(q)
    result = asyncio.run(v.tune(102_300_000.0, "wfm", 48000))
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert result in items
