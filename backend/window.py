"""Where the receiver's window sits, read from radiod. Nothing more.

This module used to place the window as well, with an "anchor": a decoy
channel positioned so that radiod's edge-parking rule would incidentally
leave the local oscillator on the station we wanted. All of that is gone.

radiod places the front end itself. Measured with a completely empty
receiver, one channel created and nothing else touched:

    Airspy R2 (4.1 MHz window), wfm 91.300
        LO 93.9500, IF -2650.0 kHz -- exactly the window centre
        snr 29.1, 601 frames in 6 s, voice/hiss 377.6, envelope-var 0.52

    Airspy HF+ (660 kHz window), nfm 162.400
        LO 162.0770, IF +323.0 kHz
        snr 12.0, 301 frames in 6 s, voice/hiss 7.80, envelope-var 0.42

The R2 case is the one that settles it: radiod centred the channel in the
window unaided, which is precisely what the anchor was built to achieve. The
HF+ case shows a narrowband channel does not even need centring -- it works
happily at +323 kHz.

What is left here reads what radiod reports so the frequency strip can draw
the window. It commands nothing.
"""
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class FrontEndWindow:
    """The receiver's usable window, as radiod reports it."""

    def __init__(self) -> None:
        self.low_edge_hz: Optional[float] = None
        self.high_edge_hz: Optional[float] = None
        self.usable_bw_hz: Optional[float] = None

    def note_edges(self, status) -> None:
        """Record the window edges from any channel status radiod sends."""
        fe = getattr(status, "frontend", None) or status
        low = getattr(fe, "low_edge", None)
        high = getattr(fe, "high_edge", None)
        if low is None or high is None:
            return
        if (low, high) != (self.low_edge_hz, self.high_edge_hz):
            self.low_edge_hz, self.high_edge_hz = float(low), float(high)
            self.usable_bw_hz = self.high_edge_hz - self.low_edge_hz
            logger.info(
                f"Receiver window: {self.usable_bw_hz/1e3:.1f} kHz "
                f"({self.low_edge_hz/1e3:+.1f}..{self.high_edge_hz/1e3:+.1f} kHz)"
            )

    def read(self, control, ssrc: Optional[int]) -> Optional[Tuple[float, float]]:
        """Absolute (low_hz, high_hz) of the window right now, or None.

        Costs one targeted status poll of a channel we already have. There is
        nothing to poll before the first tune, and nothing to draw either.
        """
        if control is None or ssrc is None:
            return None
        try:
            status = control.poll_status(ssrc, timeout=2.0)
        except Exception as e:
            logger.debug(f"window read: {e}")
            return None
        if status is None:
            return None
        self.note_edges(status)
        fe = getattr(status, "frontend", None) or status
        first_lo = getattr(fe, "first_lo", None)
        if first_lo is None or self.low_edge_hz is None:
            return None
        return (first_lo + self.low_edge_hz, first_lo + self.high_edge_hz)
