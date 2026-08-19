"""
NOAA Weather Radio source.

The 7 standard NWR frequencies fit inside a single 150 kHz window centered
on 162.475 MHz, so there is nothing to segment (see controls_schema).
Station list is loaded from data/nws_stations.json, filtered by
great-circle distance from the user's location.
"""
import json
import logging
import os
from typing import Any, Dict, List

from ..geo import haversine_km
from .base import Source, Station

logger = logging.getLogger(__name__)

# Project data directory (radiod-monitor/data)
_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)

# Standard NWS frequencies in Hz (channels 1-7)
NWS_FREQUENCIES_HZ = [
    162_400_000, 162_425_000, 162_450_000, 162_475_000,
    162_500_000, 162_525_000, 162_550_000,
]


def _channel_label(freq_hz: int) -> str:
    try:
        return f"CH {NWS_FREQUENCIES_HZ.index(freq_hz) + 1}"
    except ValueError:
        return "?"


class NwsSource(Source):
    key = "nws"
    display_name = "NOAA Weather Radio"
    preset = "nfm"

    def __init__(self):
        self._cache: List[dict] | None = None

    def _load(self) -> List[dict]:
        if self._cache is not None:
            return self._cache
        path = os.path.join(_DATA_DIR, "nws_stations.json")
        if not os.path.isfile(path):
            logger.warning(f"NWS station database not found at {path}")
            self._cache = []
            return self._cache
        try:
            with open(path, "r") as f:
                self._cache = json.load(f)
            logger.info(f"Loaded {len(self._cache)} NWS stations from {path}")
        except Exception as e:
            logger.error(f"Failed to load NWS station database: {e}")
            self._cache = []
        return self._cache

    def controls_schema(self) -> Dict[str, Any]:
        # No per-source controls. The seven NWR channels span 162.400 to
        # 162.550 MHz -- 150 kHz -- which fits inside the window of every
        # receiver this app supports, so there is nothing to segment.
        return {}

    def list_stations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        params: Dict[str, Any],
    ) -> List[Station]:
        all_stations = self._load()
        results: List[Station] = []

        for s in all_stations:
            try:
                s_lat = float(s["latitude"])
                s_lon = float(s["longitude"])
                freq_hz = int(s["frequency"])
            except (KeyError, TypeError, ValueError):
                continue

            dist = haversine_km(lat, lon, s_lat, s_lon)
            if dist > radius_km:
                continue

            callsign = s.get("callsign", "NWS")
            results.append(Station(
                id=f"nws-{callsign}-{freq_hz}",
                name=callsign,
                freq_hz=float(freq_hz),
                lat=s_lat,
                lon=s_lon,
                distance_km=dist,
                extra={
                    "channel": _channel_label(freq_hz),
                    "note": s.get("Note", ""),
                },
            ))

        # Fallback: if the database has no stations in range, surface the
        # 7 standard frequencies at the user's location so the audio path
        # can still be exercised.
        if not results:
            for i, freq_hz in enumerate(NWS_FREQUENCIES_HZ):
                results.append(Station(
                    id=f"nws-standard-{i+1}",
                    name=f"NWS-CH{i+1}",
                    freq_hz=float(freq_hz),
                    lat=lat,
                    lon=lon,
                    distance_km=0.0,
                    extra={
                        "channel": f"CH {i+1}",
                        "note": "Standard frequency (no station within range)",
                    },
                ))

        results.sort(key=lambda s: s.distance_km)
        return results
