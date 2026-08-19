"""
Source protocol — the plugin contract for frequency providers.

A Source knows how to:
  - advertise its UI controls (controls_schema)
  - compute the radiod front-end center frequency for a given user params set
    (center_freq_hz)
  - return a filtered list of Station objects to monitor (list_stations)

The shared pipeline (control WS, RadioController.ensure_channel loop,
activity monitor, audio streamer) is source-agnostic — it only sees Station
objects and a center frequency.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Fraction of the receiver's usable window a band segment may span. radiod
# places the front end by nudging it just far enough to bring a channel
# inside the window ("Retune LO1 as little as possible", radio.c:set_freq),
# not by centring it, so a segment exactly as wide as the window would sit
# hard against the edges. The margin lets placement settle with room to
# spare however radiod happens to approach it.
SEGMENT_FILL = 0.8

# Used when radiod has not told us the window yet.  Roughly an Airspy R2 at
# 10 Msps -- wide enough to be useful, narrow enough not to promise a span
# a small receiver cannot deliver.
DEFAULT_USABLE_BW_HZ = 8_000_000.0


def segment_band(
    low_mhz: float,
    high_mhz: float,
    usable_bw_hz: Optional[float],
    key_prefix: str,
    label_prefix: str = "",
) -> List[Tuple[str, float, float, str]]:
    """Split a band into segments the receiver can actually cover at once.

    Returns (key, low_mhz, high_mhz, label) tuples.

    The width comes from the radio, not from a constant: radiod reports its
    own usable IF window (fe_low_edge..fe_high_edge), which is 660 kHz on an
    Airspy HF+ at 768 kHz, ~8.6 MHz on an Airspy R2 at 10 Msps, and the whole
    HF spectrum on a direct-sampling RX888. Sizing segments to it is what
    makes every station in a segment simultaneously receivable -- which is
    the difference between an activity map that means something and one whose
    markers go dark the moment you listen to any single station.
    """
    span = max(0.0, high_mhz - low_mhz)
    width = (usable_bw_hz or DEFAULT_USABLE_BW_HZ) * SEGMENT_FILL / 1e6
    if width <= 0:
        width = span
    n = max(1, int(span / width + 0.999999)) if span > 0 else 1
    # Even segments read better than a wide one plus a sliver remainder.
    step = span / n if n else span
    out: List[Tuple[str, float, float, str]] = []
    for i in range(n):
        lo = low_mhz + i * step
        hi = lo + step if i < n - 1 else high_mhz
        key = key_prefix if n == 1 else f"{key_prefix}_{i + 1}"
        label = f"{label_prefix}{lo:.1f} – {hi:.1f} MHz"
        out.append((key, lo, hi, label))
    return out


@dataclass
class Station:
    """
    One monitorable transmitter.

    `id` is a stable identifier used for per-station UI state; `extra` is a
    free-form dict of source-specific fields that the frontend renders into
    the marker popup and list row (e.g. {"channel": "CH 5", "note": "..."}
    for NWS, or {"offset": "-0.6", "tone": "100.0 Hz"} for a repeater).
    """
    id: str
    name: str
    freq_hz: float
    lat: float
    lon: float
    distance_km: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "freq_hz": self.freq_hz,
            "lat": self.lat,
            "lon": self.lon,
            "distance_km": round(self.distance_km, 2),
            "extra": self.extra,
        }


class Source:
    """
    Subclass contract. All methods receive a `params` dict containing the
    per-source control values the user selected in the UI; the valid keys
    are defined by the source's `controls_schema`.
    """

    # Registry key — must be unique across sources. Lowercase, no spaces.
    key: str = ""

    # Display name shown in the UI mode selector.
    display_name: str = ""

    # Demod preset passed to radiod ensure_channel. Most sources use nfm;
    # airband sources would use "am", HF for "usb"/"lsb", broadcast FM
    # uses "wfm" (which radiod forces to 48 kHz output).
    preset: str = "nfm"

    # Output sample rate radiod emits for this channel. This is the *output*
    # rate (post-demod), not the demodulator IF rate — radiod's samprate
    # setting in presets.conf is internal. wfm output is forced to 48 kHz by
    # the demodulator regardless of the requested rate, and nfm/am default
    # to 24 kHz/12 kHz but 48 kHz works and simplifies the pipeline.
    sample_rate: int = 48000

    # Channel count hint for the UI. It is NOT what configures the decoder:
    # a source cannot know the answer, because the wfm preset ships
    # `mono = yes` in some ka9q-radio installs and stereo in others, and
    # ChannelInfo carries no channel count. The audio plane reads the truth
    # from the first Opus frame's TOC byte and sends it to the browser --
    # see audio_streamer.opus_channels().
    audio_channels: int = 1

    # Whether SNR-based squelch is meaningful for this source. ka9q-radio's
    # wfm demodulator does not publish a valid SNR, so SNR-squelched wfm
    # channels never open and emit zero RTP packets — audio streams time out.
    # Narrowband modes (nfm/am/etc) do publish SNR and benefit from squelch.
    # When False, the controller leaves squelch wide open (power-based, very
    # low threshold) so audio always flows.
    snr_squelch: bool = True

    def controls_schema(self, usable_bw_hz: Optional[float] = None) -> Dict[str, Any]:
        """
        Return a JSON-serializable description of this source's UI controls.

        Currently supported keys:
          - `bandSegments`: [{value, label, center_mhz}, ...] — if present,
            the frontend shows a band dropdown and passes the selected value
            back in params["band"].
          - `defaultBand`: string — initial selection.

        `usable_bw_hz` is the receiver's usable IF window as radiod reports
        it, or None when it is not known yet. Sources that span more spectrum
        than a receiver can cover at once must size their segments to it —
        see segment_band(). Segments are therefore a property of the
        source *and* the connected radio, which is why this takes an
        argument at all: switching radiod hosts changes the answer.

        Return an empty dict for sources with no extra controls.
        """
        return {}

    def center_freq_hz(self, params: Dict[str, Any]) -> float:
        """Return the radiod front-end center frequency (Hz) for these params."""
        raise NotImplementedError

    def list_stations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        params: Dict[str, Any],
    ) -> List[Station]:
        """Return the filtered station list for this (location, radius, params)."""
        raise NotImplementedError
