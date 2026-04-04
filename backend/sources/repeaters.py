"""
VHF/UHF amateur repeater source.

Loads a RepeaterBook KML export from data/repeaters*.kml, filters by
great-circle distance from the user and by a user-selected 5 MHz band
segment (the Airspy R2 covers ~5 MHz at a time; most SDRs have similar
limits relative to the ham bands). The center frequency tuned on radiod
is the midpoint of the selected band segment.
"""
import glob
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Tuple

from ..geo import haversine_km
from .base import Source, Station

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)

# Tuple form: (low_mhz, high_mhz)
_BAND_SEGMENTS: Dict[str, Tuple[float, float]] = {
    "2m":     (144.0, 148.0),
    "1.25m":  (222.0, 225.0),
    "70cm_1": (420.0, 425.0),
    "70cm_2": (425.0, 430.0),
    "70cm_3": (430.0, 435.0),
    "70cm_4": (435.0, 440.0),
    "70cm_5": (440.0, 445.0),
    "70cm_6": (445.0, 450.0),
}

_BAND_LABELS = {
    "2m":     "2m (144.0 – 148.0 MHz)",
    "1.25m":  "1.25m (222.0 – 225.0 MHz)",
    "70cm_1": "70cm  420.0 – 425.0 MHz",
    "70cm_2": "70cm  425.0 – 430.0 MHz",
    "70cm_3": "70cm  430.0 – 435.0 MHz",
    "70cm_4": "70cm  435.0 – 440.0 MHz",
    "70cm_5": "70cm  440.0 – 445.0 MHz",
    "70cm_6": "70cm  445.0 – 450.0 MHz",
}

_DEFAULT_BAND = "2m"

# Warn when the shipped KML is older than this (days).
_KML_STALE_DAYS = 180


def _find_kml_file() -> str:
    """Return the newest repeaters*.kml in the data dir, or '' if none."""
    patterns = [
        os.path.join(_DATA_DIR, "repeaters*.kml"),
    ]
    matches: List[str] = []
    for pat in patterns:
        matches.extend(glob.glob(pat))
    if not matches:
        return ""
    # Newest by mtime, with name as tiebreaker so deterministic replays work.
    matches.sort(key=lambda p: (os.path.getmtime(p), p))
    return matches[-1]


class RepeaterSource(Source):
    key = "repeaters"
    display_name = "VHF/UHF Repeaters"
    preset = "nfm"

    def __init__(self):
        self._cache: List[dict] | None = None

    # ------------------------------------------------------------------
    def _load(self) -> List[dict]:
        if self._cache is not None:
            return self._cache

        path = _find_kml_file()
        if not path:
            logger.warning(
                f"No repeaters*.kml file found in {_DATA_DIR}; searches will "
                f"return no results. Download a KML export from "
                f"https://www.repeaterbook.com and drop it in that directory."
            )
            self._cache = []
            return self._cache

        age_days = (time.time() - os.path.getmtime(path)) / 86400.0
        if age_days > _KML_STALE_DAYS:
            logger.warning(
                f"Repeater KML {os.path.basename(path)} is {age_days:.0f} "
                f"days old — consider refreshing from repeaterbook.com."
            )

        repeaters: List[dict] = []
        try:
            logger.info(f"Loading repeater data from {os.path.basename(path)} "
                        f"(age {age_days:.0f}d)")
            tree = ET.parse(path)
            root = tree.getroot()
            ns = {"kml": "http://www.opengis.net/kml/2.2"}

            for placemark in root.findall(".//kml:Placemark", ns):
                rep: Dict[str, Any] = {}
                name_elem = placemark.find("kml:name", ns)
                if name_elem is not None:
                    rep["callsign"] = (name_elem.text or "").strip()

                coords_elem = placemark.find(".//kml:coordinates", ns)
                if coords_elem is not None and coords_elem.text:
                    parts = coords_elem.text.strip().split(",")
                    if len(parts) >= 2:
                        try:
                            rep["longitude"] = float(parts[0])
                            rep["latitude"] = float(parts[1])
                        except ValueError:
                            continue

                desc_elem = placemark.find("kml:description", ns)
                if desc_elem is not None and desc_elem.text:
                    desc = desc_elem.text
                    # RepeaterBook description CDATA contains lines like
                    # "<br>147.255000+ 100.0<br>" — first number is the
                    # downlink in MHz, optional +/- is the offset sign,
                    # following number is the PL tone.
                    m = re.search(
                        r"<br>\s*([\d\.]+)\s*([+-])?\s*([\d\.]+)?",
                        desc,
                    )
                    if m:
                        try:
                            rep["frequency"] = float(m.group(1)) * 1e6
                            if m.group(2):
                                rep["offset_sign"] = m.group(2)
                            if m.group(3):
                                rep["tone"] = m.group(3)
                        except ValueError:
                            pass

                if "latitude" in rep and "longitude" in rep and "frequency" in rep:
                    repeaters.append(rep)

            logger.info(f"Loaded {len(repeaters)} repeaters from KML")
            self._cache = repeaters
        except Exception as e:
            logger.error(f"Failed to parse KML data: {e}", exc_info=True)
            self._cache = []

        return self._cache

    # ------------------------------------------------------------------
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

        results: List[Station] = []
        for r in self._load():
            try:
                r_lat = r["latitude"]
                r_lon = r["longitude"]
                freq_hz = float(r["frequency"])
            except (KeyError, TypeError, ValueError):
                continue

            freq_mhz = freq_hz / 1e6
            if not (low <= freq_mhz <= high):
                continue

            dist = haversine_km(lat, lon, r_lat, r_lon)
            if dist > radius_km:
                continue

            callsign = r.get("callsign") or "RPT"
            extra: Dict[str, Any] = {}
            if "offset_sign" in r:
                extra["offset"] = r["offset_sign"]
            if "tone" in r:
                extra["tone"] = f"{r['tone']} Hz"

            results.append(Station(
                id=f"rpt-{callsign}-{int(freq_hz)}",
                name=callsign,
                freq_hz=freq_hz,
                lat=r_lat,
                lon=r_lon,
                distance_km=dist,
                extra=extra,
            ))

        results.sort(key=lambda s: s.distance_km)
        return results
