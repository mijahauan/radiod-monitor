"""Where the receiver's front-end window sits, and how to move it.

radiod owns front-end placement and derives it from the frequencies of the
channels it is asked for. It retunes only when a channel's filter would fall
outside the window, and then minimally -- parking the channel
`filter.max_IF + fudge` from the edge (radio.c:set_freq).

That is fine for narrowband demods, whose filters are a few kHz wide. It is
not fine for `wfm`: wfm.c demodulates through a 384 kHz composite path needing
+/-192 kHz around the channel, while radiod reserves only 110 kHz for it. A
wfm channel placed by radiod therefore cannot demodulate -- verified with
radiod's own `tune` utility, which places it at IF +219.2 kHz and reports
snr=-inf.

The workaround is an "anchor": a narrow `am` channel positioned so that
radiod's edge-parking of *the anchor* leaves the window centred on the
frequency we actually want. When radiod reserves the demodulator's real
bandwidth, this whole module reduces to probe() and read().
"""
import logging
from typing import Optional, Tuple

from ka9q.types import Encoding

logger = logging.getLogger(__name__)

# Filter half-width radiod reserves for the anchor's own `am` preset (~5 kHz)
# plus set_freq's 1 kHz fudge.
ANCHOR_MARGIN_HZ = 6_000.0

# How far off centre a station may sit before it is worth moving the window.
# Moving the LO restarts radiod's placement and disturbs a demodulator that is
# already producing audio, so re-centring for a station that is comfortably
# inside the window costs audio and buys nothing. As a fraction of the window
# half-width: at 25% on the HF+'s 660.5 kHz window that is +/-82.5 kHz, which
# still leaves wfm's +/-192 kHz composite path inside the window
# (82.5 + 192 = 274.5 < 330.2), so the tolerance is safe for the widest demod
# this app uses. All seven NWR channels sit within 75 kHz of each other, so a
# station-to-station switch inside that band now leaves the LO alone.
RECENTRE_TOLERANCE = 0.25

# Assumed window when radiod does not report FE_LOW_EDGE/FE_HIGH_EDGE.
DEFAULT_USABLE_BW_HZ = 8_000_000.0

# Frequency for the throwaway channel probe() needs: radiod reports the
# front-end limits only alongside per-channel status.
PROBE_FREQ_HZ = 10_000_000.0


def choose_anchor_frequency(
    target_hz: float,
    current_lo_hz: float,
    low_edge_hz: float,
    high_edge_hz: float,
    margin_hz: float = ANCHOR_MARGIN_HZ,
) -> Optional[float]:
    """Where to put the anchor so radiod moves the window onto `target_hz`.

    radiod ignores a channel already inside the window, so the anchor only
    works from a frequency that is outside it. Which side qualifies depends on
    where the LO currently sits -- get it wrong and the anchor is a silent
    no-op, which costs the listener minutes of silence.

    The candidate offset from target_hz is the window's HALF-WIDTH minus the
    margin, not either edge individually: `target + (high_edge - margin)`
    only centres the window when it happens to be symmetric about the LO
    (low_edge == -high_edge). In general, when radiod parks the anchor
    `margin_hz` inside whichever edge it retuned to, the LO ends up at
    `candidate - (edge - margin)` -- which lands on target_hz from either
    side only when the candidate is built from the half-span of the window,
    not from one edge alone. Identical to the old edge-based expressions in
    the symmetric case; correct for an asymmetric window too (e.g. the
    Airspy R2's -4700..-600 kHz, which is nowhere near centred on the LO).

    Returns None when neither side is outside, which means the window is far
    wider than the offsets and needs no centring.
    """
    half_span = (high_edge_hz - low_edge_hz) / 2.0 - margin_hz
    high_side = target_hz + half_span
    low_side = target_hz - half_span

    for candidate in (high_side, low_side):
        offset = candidate - current_lo_hz
        if offset > high_edge_hz or offset < low_edge_hz:
            return candidate
    return None


class FrontEndWindow:
    """Measured position and width of the receiver's usable window."""

    def __init__(self) -> None:
        self.low_edge_hz: Optional[float] = None
        self.high_edge_hz: Optional[float] = None
        self.usable_bw_hz: Optional[float] = None
        self.anchor_ssrc: Optional[int] = None

    def probe(self, control, destination: str, sample_rate: int) -> Optional[float]:
        """Read FE_LOW_EDGE/FE_HIGH_EDGE. Costs one throwaway channel."""
        if control is None:
            return None
        # Imported lazily: radio_controller imports FrontEndWindow at module
        # level, so a top-level import here would be circular. See
        # radio_controller.py's CHANNEL_LIFETIME_FRAMES docstring for why
        # this matters -- without it radiod parks this channel at
        # DEFAULT_LIFETIME (20 s) after we remove it, instead of reaping it
        # within ~1 s.
        from .radio_controller import CHANNEL_LIFETIME_FRAMES
        ssrc = None
        try:
            probe = control.ensure_channel(
                frequency_hz=PROBE_FREQ_HZ, preset="am", sample_rate=sample_rate,
                gain=0.0, destination=destination, encoding=Encoding.OPUS, timeout=5.0,
                lifetime=CHANNEL_LIFETIME_FRAMES,
            )
            ssrc = probe.ssrc
            fe = self._frontend_of(control, ssrc)
            low = getattr(fe, "fe_low_edge", None) if fe else None
            high = getattr(fe, "fe_high_edge", None) if fe else None
            if low is not None and high is not None and high > low:
                self.low_edge_hz, self.high_edge_hz = float(low), float(high)
                self.usable_bw_hz = float(high - low)
                logger.info(
                    f"{getattr(fe, 'description', 'front end')}: usable window "
                    f"{self.usable_bw_hz/1e3:.1f} kHz "
                    f"({low/1e3:+.1f}..{high/1e3:+.1f} kHz)"
                )
            else:
                logger.warning("radiod did not report FE_LOW_EDGE/FE_HIGH_EDGE")
        except Exception as e:
            logger.warning(f"Could not probe the front end: {e}")
        finally:
            if ssrc is not None:
                try:
                    control.remove_channel(ssrc)
                except Exception:
                    pass
        return self.usable_bw_hz

    @staticmethod
    def _frontend_of(control, ssrc: int):
        try:
            status = control.poll_status(ssrc, timeout=2.0)
        except Exception as e:
            logger.debug(f"poll_status({ssrc:08x}) failed: {e}")
            return None
        return getattr(status, "frontend", None) or status

    def read(self, control, ssrc_hint: Optional[int]) -> Optional[Tuple[float, float]]:
        """Absolute (low_hz, high_hz) of the window, measured. None if unknown."""
        if control is None or self.low_edge_hz is None or self.high_edge_hz is None:
            return None
        ssrc = self.anchor_ssrc if self.anchor_ssrc is not None else ssrc_hint
        if ssrc is None:
            return None
        fe = self._frontend_of(control, ssrc)
        first_lo = getattr(fe, "first_lo", None) if fe else None
        if first_lo is None:
            return None
        return (first_lo + self.low_edge_hz, first_lo + self.high_edge_hz)

    def centre_on(self, control, freq_hz: float, destination: str,
                  sample_rate: int, ssrc_hint: Optional[int] = None) -> bool:
        """Move the window so `freq_hz` sits near its centre.

        Returns True when the window is (or already was) usable for freq_hz.
        """
        if control is None or self.low_edge_hz is None or self.high_edge_hz is None:
            return False
        from .radio_controller import CHANNEL_LIFETIME_FRAMES

        window = self.read(control, ssrc_hint)
        if window is None:
            if self.anchor_ssrc is not None:
                # An anchor exists but its status is unreadable right now --
                # don't create a second one under it.
                return False
            # Directory mode's first Listen: no anchor and no active channel
            # exist yet, so read() has no SSRC to poll at all and can never
            # succeed no matter how long we wait. Break the deadlock by
            # creating the anchor at freq_hz itself -- not yet where it
            # belongs, but enough to give read() something to poll -- then
            # fall through to the normal path, which will retune it to the
            # frequency actually computed below.
            try:
                anchor = control.ensure_channel(
                    frequency_hz=freq_hz, preset="am", sample_rate=sample_rate,
                    gain=0.0, destination=destination,
                    encoding=Encoding.OPUS, timeout=5.0,
                    lifetime=CHANNEL_LIFETIME_FRAMES,
                )
                self.anchor_ssrc = anchor.ssrc
                control.set_squelch(self.anchor_ssrc, enable=True,
                                    open_snr_db=80.0, close_snr_db=75.0)
            except Exception as e:
                logger.warning(
                    f"Could not place the initial anchor at "
                    f"{freq_hz/1e6:.3f} MHz: {e}"
                )
                return False
            window = self.read(control, ssrc_hint)
            if window is None:
                logger.warning(
                    "centre_on: anchor created but its window is still "
                    "unreadable"
                )
                return False

        low_hz, high_hz = window
        centre = (low_hz + high_hz) / 2.0
        current_lo = centre - (self.low_edge_hz + self.high_edge_hz) / 2.0
        half_span = (self.high_edge_hz - self.low_edge_hz) / 2.0
        if abs(freq_hz - current_lo) <= half_span * RECENTRE_TOLERANCE:
            # Already comfortably inside. Leave the LO alone -- see
            # RECENTRE_TOLERANCE.
            logger.debug(
                f"centre_on: {freq_hz/1e6:.3f} MHz is "
                f"{(freq_hz-current_lo)/1e3:+.1f} kHz off centre; not moving "
                f"the window"
            )
            return True

        anchor_hz = choose_anchor_frequency(
            freq_hz, current_lo, self.low_edge_hz, self.high_edge_hz
        )
        if anchor_hz is None:
            # The window is far wider than the offsets; nothing to centre.
            return True
        try:
            if self.anchor_ssrc is None:
                # Created once, then RETUNED -- never recreated per station.
                # Recreating it per station churns channels radiod reaps only
                # after ~20 s, which is how a silent station accumulated 38
                # dead channels and starved the control socket.
                anchor = control.ensure_channel(
                    frequency_hz=anchor_hz, preset="am", sample_rate=sample_rate,
                    gain=0.0, destination=destination,
                    encoding=Encoding.OPUS, timeout=5.0,
                    lifetime=CHANNEL_LIFETIME_FRAMES,
                )
                self.anchor_ssrc = anchor.ssrc
                control.set_squelch(self.anchor_ssrc, enable=True,
                                    open_snr_db=80.0, close_snr_db=75.0)
            else:
                control.set_frequency(self.anchor_ssrc, anchor_hz)
        except Exception as e:
            logger.warning(f"Could not place the anchor at {anchor_hz/1e6:.3f} MHz: {e}")
            return False
        # self.anchor_ssrc, not the local `anchor` var: the retune branch
        # above never binds one.
        logger.info(
            f"Window centred on {freq_hz/1e6:.3f} MHz via anchor at "
            f"{anchor_hz/1e6:.3f} MHz (SSRC {self.anchor_ssrc:08x})"
        )
        return True

    def release(self, control) -> None:
        """Drop the anchor. Users see a lingering one as a phantom AM station."""
        ssrc, self.anchor_ssrc = self.anchor_ssrc, None
        if ssrc is not None and control is not None:
            try:
                control.remove_channel(ssrc)
            except Exception:
                pass
