"""
Commercial FM broadcast source.

Loads the US FCC CDBS FM database (joined facility.dat + fm_eng_data.dat;
see scripts/fetch_fm_stations.py to refresh) from
data/fm_stations.json and filters by great-circle distance from the user
and the whole 88.0 – 108.0 MHz FM broadcast band. Which of those stations
the connected receiver can actually hear at once is RadioController's
problem, not this source's -- see RadioController for how it decides what
fits the current front-end window.

Uses radiod's "wfm" preset: wideband FM with 75 µs North American
de-emphasis, output forced to 48 kHz. Note the shipped preset sets
`mono = yes`, so audio_channels is only a hint — the browser's decoder is
configured from the first Opus frame (see Source.audio_channels).

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
from typing import Any, Dict, List

from ..geo import haversine_km
from .base import Source, Station

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)


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
    # Broadcast FM should not be gated on SNR. Note the controller cannot
    # honour this by sending enable=False: radiod's wfm.c sets
    # chan->squelch.snr_enable = true unconditionally when the demod thread
    # starts, reverting it. RadioController._squelch_args() therefore holds
    # the squelch open with a very low threshold instead.
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

    def list_stations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        params: Dict[str, Any],
    ) -> List[Station]:
        # The whole FM broadcast band. Which of these the receiver can hear at
        # once is RadioController's business, not this source's.
        low_hz, high_hz = 88.0e6, 108.0e6

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
