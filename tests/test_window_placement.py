"""Anchor side selection.

radiod retunes only for a channel whose filter falls OUTSIDE the current
window; a channel already in range is ignored. So an anchor placed on the
wrong side is a silent no-op, and which side is correct depends on where the
LO happens to sit. These are the numbers measured on the Airspy HF+
(+/-330240 Hz window) during the 2026-08-19 session.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.window import choose_anchor_frequency

LOW, HIGH = -330_240.0, 330_240.0
MARGIN = 6_000.0


def test_high_side_used_when_it_falls_outside_the_window():
    # LO far below the target: the high-side anchor is well outside.
    got = choose_anchor_frequency(102_300_000.0, 101_000_000.0, LOW, HIGH, MARGIN)
    assert got == 102_300_000.0 + (HIGH - MARGIN)


def test_low_side_used_when_the_high_side_would_be_inside():
    # The measured failure: the VFO itself pulled the LO to target+219.2 kHz,
    # so the high-side anchor lands at IF +105 kHz -- inside, and ignored.
    lo = 102_300_000.0 + 219_200.0
    got = choose_anchor_frequency(102_300_000.0, lo, LOW, HIGH, MARGIN)
    assert got == 102_300_000.0 + (LOW + MARGIN)


def test_returns_none_when_neither_side_is_outside():
    # A window far wider than the offsets (direct-sampling RX888): no anchor
    # can move it, and none is needed.
    got = choose_anchor_frequency(10_000_000.0, 10_000_000.0, -32e6, 32e6, MARGIN)
    assert got is None


def test_prefers_high_side_when_both_are_outside():
    # Deterministic choice keeps behaviour reproducible.
    got = choose_anchor_frequency(102_300_000.0, 90_000_000.0, LOW, HIGH, MARGIN)
    assert got == 102_300_000.0 + (HIGH - MARGIN)


def test_anchor_is_retuned_not_recreated():
    """The anchor is created once and moved thereafter.

    Recreating it per station churns channels radiod reaps only after ~20 s;
    a silent station once accumulated 38 dead channels that way and starved
    the control socket, which is what made switching take minutes.
    """
    import backend.window as W

    class Ctl:
        def __init__(self):
            self.creates = 0
            self.retunes = 0
        def ensure_channel(self, **kw):
            self.creates += 1
            class Ch:
                ssrc = 0xABCD
            return Ch()
        def set_frequency(self, ssrc, hz):
            self.retunes += 1
        def set_squelch(self, ssrc, **kw):
            pass
        def poll_status(self, ssrc, timeout=2.0):
            class FE:
                first_lo = 90_000_000.0
                fe_low_edge = LOW
                fe_high_edge = HIGH
            class S:
                frontend = FE()
            return S()

    w = W.FrontEndWindow()
    w.low_edge_hz, w.high_edge_hz = LOW, HIGH
    c = Ctl()
    assert w.centre_on(c, 102_300_000.0, "239.1.2.3", 48000, ssrc_hint=1) is True
    assert w.centre_on(c, 91_300_000.0, "239.1.2.3", 48000, ssrc_hint=1) is True
    assert c.creates == 1, "the anchor must be created once"
    assert c.retunes >= 1, "subsequent placements must retune it"


def test_asymmetric_window_is_honoured():
    # The Airspy R2 reports -4700..-600 kHz -- not centred on the LO. The
    # candidate offset is the window's HALF-WIDTH minus the margin, not
    # (high_edge - margin): that edge-based form only centres a window
    # symmetric about the LO. half_span = (hi_edge - lo_edge)/2 - MARGIN
    # = 2_044_000; landing here at IF +2_044_000 is genuinely outside the
    # window, unlike the edge-based 97_494_000 (IF -606_000, 6 kHz *inside*
    # the high edge -- a silent no-op).
    lo_edge, hi_edge = -4_700_000.0, -600_000.0
    got = choose_anchor_frequency(98_100_000.0, 98_100_000.0, lo_edge, hi_edge, MARGIN)
    assert got == 100_144_000.0


def test_low_side_preferred_when_high_side_would_land_inside_symmetric_window():
    # LO sits 400 kHz above the target -- within (H, 2H) of it, the band
    # where the high-side candidate is still inside the window (offset
    # -75,760) but the low-side candidate has already been pushed outside
    # (offset -724,240). Only the loop's low-side branch gets this right;
    # a naive "always prefer high" shortcut returns a no-op here.
    target = 102_300_000.0
    lo = target + 400_000.0
    got = choose_anchor_frequency(target, lo, LOW, HIGH, MARGIN)
    assert got == target - (HIGH - MARGIN)
    assert got == 101_975_760.0


def test_low_side_preferred_on_the_asymmetric_r2_window_too():
    # Same shape as the symmetric case above, but on the R2's asymmetric
    # -4700..-600 kHz window: LO 4 MHz above target puts the high-side
    # candidate's offset (-1,956,000) inside the window and the low-side
    # candidate's offset (-6,044,000) outside it.
    lo_edge, hi_edge = -4_700_000.0, -600_000.0
    target = 98_100_000.0
    lo = target + 4_000_000.0
    got = choose_anchor_frequency(target, lo, lo_edge, hi_edge, MARGIN)
    assert got == 96_056_000.0


def test_centre_on_breaks_the_first_listen_deadlock():
    """Directory mode's first Listen: no anchor, no active channel.

    read() needs an SSRC to poll and there is none yet, so it returns None
    forever -- not because status is slow, but because nothing exists to ask
    about. centre_on() must notice this specific case (no anchor yet) and
    create one at freq_hz just to get a pollable SSRC, then retune it to the
    frequency the module actually computes -- not leave it parked at
    freq_hz, which is what a naive "give up when read() fails" would do.
    """
    import backend.window as W

    lo_edge, hi_edge = -4_700_000.0, -600_000.0

    class DeadlockCtl:
        def __init__(self):
            self.channel_created = False
            self.created_freqs = []
            self.retuned_freqs = []

        def ensure_channel(self, **kw):
            self.channel_created = True
            self.created_freqs.append(kw["frequency_hz"])
            class Ch:
                ssrc = 0xF00D
            return Ch()

        def set_frequency(self, ssrc, hz):
            self.retuned_freqs.append(hz)

        def set_squelch(self, ssrc, **kw):
            pass

        def poll_status(self, ssrc, timeout=2.0):
            if not self.channel_created:
                # No channel exists yet -- nothing to poll status from.
                raise RuntimeError("no channel yet")
            class FE:
                first_lo = 102_100_000.0
                fe_low_edge = lo_edge
                fe_high_edge = hi_edge
            class S:
                frontend = FE()
            return S()

    w = W.FrontEndWindow()
    w.low_edge_hz, w.high_edge_hz = lo_edge, hi_edge
    c = DeadlockCtl()

    # Same numbers as test_low_side_preferred_on_the_asymmetric_r2_window_too:
    # with first_lo=102_100_000 the computed anchor lands on the low side,
    # 96_056_000 -- nowhere near freq_hz itself.
    got = w.centre_on(c, 98_100_000.0, "239.1.2.3", 48000, ssrc_hint=None)

    assert got is True
    assert c.created_freqs == [98_100_000.0], (
        "the deadlock-breaking channel is created at freq_hz first"
    )
    assert c.retuned_freqs == [96_056_000.0], (
        "and then retuned to the computed anchor frequency, not left at freq_hz"
    )
    assert w.anchor_ssrc == 0xF00D
