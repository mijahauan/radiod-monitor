"""fits_window decides whether a whole station set can be monitored at once.

Pure logic: no radiod, no network. The rule is span <= usable_bw_hz *
WINDOW_FILL, where span is max(freq) - min(freq).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.radio_controller import RadioController


def _controller(usable_bw_hz):
    c = RadioController.__new__(RadioController)   # no radiod connection
    c.usable_bw_hz = usable_bw_hz
    return c


def test_nws_band_fits_the_narrowest_receiver():
    # 7 NWR channels span 150 kHz; the Airspy HF+ window is 660.5 kHz.
    nws = [162.400e6, 162.425e6, 162.450e6, 162.475e6, 162.500e6, 162.525e6, 162.550e6]
    assert _controller(660_500.0).fits_window(nws) is True


def test_fm_band_does_not_fit_a_narrow_receiver():
    assert _controller(660_500.0).fits_window([88.1e6, 107.9e6]) is False


def test_fm_band_does_not_fit_even_a_wide_receiver():
    # The Airspy R2 reports a 4.1 MHz window; the FM band is 20 MHz.
    assert _controller(4_100_000.0).fits_window([88.1e6, 107.9e6]) is False


def test_two_m_fits_the_r2_but_not_the_hf_plus():
    two_m = [144.0e6, 148.0e6]
    assert _controller(4_100_000.0).fits_window(two_m) is False   # 4.0 > 4.1*0.8
    assert _controller(660_500.0).fits_window(two_m) is False


def test_single_station_always_fits():
    assert _controller(660_500.0).fits_window([102.3e6]) is True


def test_empty_set_fits():
    assert _controller(660_500.0).fits_window([]) is True


def test_fill_margin_is_applied():
    # Exactly the raw window does NOT fit; 80% of it does.
    assert _controller(1_000_000.0).fits_window([100.0e6, 101.0e6]) is False
    assert _controller(1_000_000.0).fits_window([100.0e6, 100.8e6]) is True


def test_unknown_window_falls_back_to_the_default():
    c = _controller(None)
    assert c.fits_window([100.0e6, 106.0e6]) is True     # 6 MHz <= 8 MHz * 0.8
    assert c.fits_window([88.1e6, 107.9e6]) is False
