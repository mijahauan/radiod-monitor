"""
Commercial FM broadcast source.

Loads the US FCC CDBS FM database (joined facility.dat + fm_eng_data.dat;
see scripts/fetch_fm_stations.py to refresh) from
data/fm_stations.json and filters by great-circle distance and a
user-selected band segment.

Uses radiod's "wfm" preset: wideband FM with 75 µs North American
de-emphasis, output forced to 48 kHz. Note the shipped preset sets
`mono = yes`, so audio_channels is only a hint — the browser's decoder is
configured from the first Opus frame (see Source.audio_channels).

The US FM broadcast band is 88.0 – 108.0 MHz, wider than most front ends
can cover at once, so it is split into segments. The split is computed
from the connected receiver's usable window rather than fixed, because
the right answer differs per radio: a direct-sampling RX888 covers the
whole band, an Airspy R2 at 10 Msps needs a few segments, and an Airspy
HF+ at 768 kHz manages a few stations at a time. A segment wider than the
window does not just show unreachable stations — it makes them unlistenable,
since radiod can only place the front end over one window at a time.

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
from .base import Source, Station, segment_band

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)

# The US FM broadcast band. Segments are not fixed: they are cut from this
# range to fit whatever the connected receiver can cover in one front-end
# window (see base.segment_band). A direct-sampling RX888 takes the whole
# band in one; an Airspy R2 needs a few; an Airspy HF+ at 768 kHz gets a
# handful of stations at a time.
_BAND_LOW_MHZ = 88.0
_BAND_HIGH_MHZ = 108.0
_KEY_PREFIX = "fm"


def _segments(usable_bw_hz):
    return segment_band(_BAND_LOW_MHZ, _BAND_HIGH_MHZ, usable_bw_hz, _KEY_PREFIX)


def _segment_for(band, usable_bw_hz):
    """Resolve a band key to (low_mhz, high_mhz), tolerating a stale key.

    The key encodes a division of the band that depends on the receiver, so
    one saved by the browser -- or chosen before a host switch -- may no
    longer exist. Falling back to the middle segment keeps a search working
    instead of failing on a key the UI had every reason to believe in.
    """
    segs = _segments(usable_bw_hz)
    for key, low, high, _ in segs:
        if key == band:
            return low, high
    key, low, high, _ = segs[len(segs) // 2]
    return low, high


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
    # Declared for consistency, but understand that radiod ignores it here:
    # wfm.c sets chan->squelch.snr_enable = true unconditionally when the
    # demod starts, so the channel is SNR-squelched no matter what we send,
    # and no threshold opens it while the demod reports snr=-inf (which is
    # what it reports with no FM carrier present). A wfm channel with a real
    # signal opens on its own; one without stays shut whatever we ask for.
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

    def controls_schema(self, usable_bw_hz=None) -> Dict[str, Any]:
        segs = _segments(usable_bw_hz)
        return {
            "bandSegments": [
                {"value": key, "label": label, "center_mhz": (low + high) / 2.0}
                for key, low, high, label in segs
            ],
            # Middle of the band: the most populated part of the dial, and
            # a sane landing spot whatever the segment width turned out to be.
            "defaultBand": segs[len(segs) // 2][0],
        }

    def center_freq_hz(self, params: Dict[str, Any]) -> float:
        low, high = _segment_for(params.get("band"), params.get("usable_bw_hz"))
        return ((low + high) / 2.0) * 1e6

    def list_stations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        params: Dict[str, Any],
    ) -> List[Station]:
        low, high = _segment_for(params.get("band"), params.get("usable_bw_hz"))
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
