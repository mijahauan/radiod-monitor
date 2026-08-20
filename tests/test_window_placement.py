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
    # The Airspy R2 reports -4700..-600 kHz -- not centred on the LO.
    lo_edge, hi_edge = -4_700_000.0, -600_000.0
    got = choose_anchor_frequency(98_100_000.0, 98_100_000.0, lo_edge, hi_edge, MARGIN)
    assert got == 98_100_000.0 + (hi_edge - MARGIN)
