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
from typing import Any, Dict, List


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
    # future sources for airband would use "am", HF for "usb"/"lsb", etc.
    preset: str = "nfm"

    def controls_schema(self) -> Dict[str, Any]:
        """
        Return a JSON-serializable description of this source's UI controls.

        Currently supported keys:
          - `bandSegments`: [{value, label, center_mhz}, ...] — if present,
            the frontend shows a band dropdown and passes the selected value
            back in params["band"]. `center_mhz` is the receiver front-end
            center to tune for that segment.
          - `defaultBand`: string — initial selection.

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
