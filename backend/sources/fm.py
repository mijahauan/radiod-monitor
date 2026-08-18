"""
Commercial FM broadcast source.

Loads the US FCC CDBS FM database (joined facility.dat + fm_eng_data.dat;
see scripts/fetch_fm_stations.py to refresh) from
data/fm_stations.json and filters by great-circle distance and a
user-selected band segment.

Uses radiod's "wfm" preset, which per ka9q-radio/share/presets.conf is
wideband FM demodulation with 75 µs North American de-emphasis and
output forced to 48 kHz **stereo** — hence audio_channels = 2.

The US FM broadcast band is 88.0 – 108.0 MHz (20 MHz wide). Most
popular SDR front ends (Airspy R2: ~10 MHz, RTL-SDR: ~2.4 MHz) can't
cover the whole band at once, so the source exposes configurable
5 MHz band segments. If you're on a wider-bandwidth SDR you can select
any segment without retuning; on a narrower one you'll pick the band
you want to scan.

data/fm_stations.json schema (flat list):
    {
      "callsign": "WRCT",
      "frequency": 88300000,         # Hz
      "latitude": 40.444,
      "longitude": -79.945,
      "community": "PITTSBURGH",
      "state": "PA",
      "station_class": "A",
      "erp_kw": 1.8,                 # or null
    }
"""
import json
import logging
import os
from typing import Any, Dict, List, Tuple

from ..geo import haversine_km
from .base import Source, Station

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)

# Tuple form: (low_mhz, high_mhz). 5 MHz chunks so an Airspy R2 can
# cover any one segment in one front-end tune.
_BAND_SEGMENTS: Dict[str, Tuple[float, float]] = {
    "fm_1": (88.0, 93.0),
    "fm_2": (93.0, 98.0),
    "fm_3": (98.0, 103.0),
    "fm_4": (103.0, 108.0),
}

_BAND_LABELS = {
    "fm_1": "88.0 – 93.0 MHz",
    "fm_2": "93.0 – 98.0 MHz",
    "fm_3": "98.0 – 103.0 MHz",
    "fm_4": "103.0 – 108.0 MHz",
}

_DEFAULT_BAND = "fm_2"  # 93–98 MHz: includes the most populated slots


class FmSource(Source):
    key = "fm"
    display_name = "Commercial FM"
    preset = "wfm"
    # Hint only — the decoder is configured from the wire (see Source docs).
    # Both the in-tree and installed wfm presets currently ship `mono = yes`,
    # so this is 2 for installs whose preset enables stereo, and harmless
    # where it does not.
    audio_channels = 2
    sample_rate = 48000  # wfm output is always 48 kHz (forced by demod)
    # ka9q-radio's wfm demod does not publish a valid SNR (always -inf),
    # so SNR-based squelch would clamp the channel shut and no RTP packets
    # would ever flow. Broadcast FM doesn't need squelch anyway.
    snr_squelch = False

    def __init__(self):
        self._cache: List[dict] | None = None

    def _load(self) -> List[dict]:
        if self._cache is not None:
            return self._cache
        path = os.path.join(_DATA_DIR, "fm_stations.json")
        if not os.path.isfile(path):
            logger.warning(
                f"FM station database not found at {path}. Run "
                f"scripts/fetch_fm_stations.py to build it."
            )
            self._cache = []
            return self._cache
        try:
            with open(path, "r") as f:
                self._cache = json.load(f)
            logger.info(f"Loaded {len(self._cache):,} FM stations from {path}")
        except Exception as e:
            logger.error(f"Failed to load FM station database: {e}")
            self._cache = []
        return self._cache

    def controls_schema(self) -> Dict[str, Any]:
        return {
            "bandSegments": [
                {
                    "value": key,
                    "label": _BAND_LABELS[key],
                    "center_mhz": (low + high) / 2.0,
                }
                for key, (low, high) in _BAND_SEGMENTS.items()
            ],
            "defaultBand": _DEFAULT_BAND,
        }

    def center_freq_hz(self, params: Dict[str, Any]) -> float:
        band = params.get("band", _DEFAULT_BAND)
        low, high = _BAND_SEGMENTS.get(band, _BAND_SEGMENTS[_DEFAULT_BAND])
        return ((low + high) / 2.0) * 1e6

    def list_stations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        params: Dict[str, Any],
    ) -> List[Station]:
        band = params.get("band", _DEFAULT_BAND)
        low, high = _BAND_SEGMENTS.get(band, _BAND_SEGMENTS[_DEFAULT_BAND])
        low_hz, high_hz = low * 1e6, high * 1e6

        results: List[Station] = []
        for s in self._load():
            try:
                freq_hz = float(s["frequency"])
                s_lat = float(s["latitude"])
                s_lon = float(s["longitude"])
            except (KeyError, TypeError, ValueError):
                continue

            if not (low_hz <= freq_hz <= high_hz):
                continue

            dist = haversine_km(lat, lon, s_lat, s_lon)
            if dist > radius_km:
                continue

            callsign = s.get("callsign", "?")
            community = s.get("community", "")
            state = s.get("state", "")
            station_class = s.get("station_class", "")
            erp_kw = s.get("erp_kw")

            extra: Dict[str, Any] = {}
            if community and state:
                extra["city"] = f"{community.title()}, {state}"
            elif community:
                extra["city"] = community.title()
            if station_class:
                extra["class"] = station_class
            if erp_kw:
                extra["erp"] = f"{erp_kw:g} kW"

            results.append(Station(
                id=f"fm-{callsign}-{int(freq_hz)}",
                name=callsign,
                freq_hz=freq_hz,
                lat=s_lat,
                lon=s_lon,
                distance_km=dist,
                extra=extra,
            ))

        results.sort(key=lambda s: s.distance_km)
        return results
