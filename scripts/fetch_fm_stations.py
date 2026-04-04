#!/usr/bin/env python3
"""
Fetch and compile the US commercial FM station database from the FCC's
public CDBS dump.

The FCC froze CDBS on 2023-10-01 in favor of LMS, so the data is stable
but not actively updated. For "monitor what's on the air today" that's
fine — broadcast FM assignments change on the scale of years, not weeks.

This script downloads two pipe-delimited CDBS tables and joins them on
facility_id:

  facility.dat     — callsign, frequency, community, state, license status
  fm_eng_data.dat  — transmitter lat/lon (D/M/S), station class, effective
                     ERP, current/archived record flag

Output: data/fm_stations.json — a flat list of dicts with the fields
backend/sources/fm.py expects:

    [
      {
        "callsign": "WRCT",
        "frequency": 88300000,             # Hz
        "latitude": 40.444,
        "longitude": -79.945,
        "community": "PITTSBURGH",
        "state": "PA",
        "station_class": "A",
        "erp_kw": 1.8,
      },
      ...
    ]

Run:  scripts/fetch_fm_stations.py        # writes data/fm_stations.json
      scripts/fetch_fm_stations.py --dry  # prints count and first few rows
"""
import argparse
import io
import json
import logging
import os
import sys
import urllib.request
import zipfile
from typing import Dict, Iterable, List, Optional

CDBS_BASE = "https://transition.fcc.gov/Bureaus/MB/Databases/cdbs"
FACILITY_URL     = f"{CDBS_BASE}/facility.zip"
FM_ENG_DATA_URL  = f"{CDBS_BASE}/fm_eng_data.zip"

# Field indices are 1-based in the FCC schema docs; we subtract 1 when
# using them against Python's 0-based lists.
FAC = {
    "comm_city":     1,
    "comm_state":    2,
    "fac_callsign":  6,
    "fac_channel":   7,
    "fac_frequency": 10,
    "fac_service":   11,
    "facility_id":   15,
    "fac_status":    17,
}

ENG = {
    "effective_erp":    17,
    "eng_record_type":  20,
    "facility_id":      21,
    "lat_deg":          31,
    "lat_dir":          32,
    "lat_min":          33,
    "lat_sec":          34,
    "lon_deg":          35,
    "lon_dir":          36,
    "lon_min":          37,
    "lon_sec":          38,
    "station_class":    50,
}

# Only these license statuses are "on the air now". The CDBS uses short
# codes; LICEN = Licensed, LIC = Licensed, APP = application, CP =
# construction permit, CANCEL/EXP = dead. We keep Licensed + any blanks
# that have a current engineering record.
LIVE_FAC_STATUSES = {"LICEN", "LIC", ""}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _dl_dat(url: str, member_suffix: str) -> bytes:
    """Download a CDBS .zip and return the uncompressed .dat bytes."""
    _log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        raw = resp.read()
    _log(f"  got {len(raw):,} bytes")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    for name in zf.namelist():
        if name.endswith(member_suffix):
            return zf.read(name)
    raise RuntimeError(f"{url}: no *{member_suffix} inside zip")


def _rows(dat: bytes) -> Iterable[List[str]]:
    """Yield pipe-split rows from a CDBS .dat file (Latin-1)."""
    text = dat.decode("latin-1", errors="replace")
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        # Rows end with "^|" — strip the sentinel.
        if line.endswith("|"):
            line = line[:-1]
        parts = line.split("|")
        if len(parts) >= 2:
            yield parts


def _get(row: List[str], one_based_idx: int) -> str:
    i = one_based_idx - 1
    if 0 <= i < len(row):
        return row[i].strip()
    return ""


def _dms_to_decimal(
    deg: str, minutes: str, secs: str, direction: str
) -> Optional[float]:
    """Convert FCC-style degrees/minutes/seconds to signed decimal degrees."""
    try:
        d = int(deg) if deg else 0
        m = int(minutes) if minutes else 0
        s = float(secs) if secs else 0.0
    except ValueError:
        return None
    value = d + m / 60.0 + s / 3600.0
    direction = direction.strip().upper()
    if direction in ("S", "W"):
        value = -value
    elif direction not in ("N", "E", ""):
        return None
    # Sanity: lat ∈ [-90, 90], lon ∈ [-180, 180]. CDBS sometimes carries
    # zero rows which we want to drop — leave that to the caller.
    if value == 0.0:
        return None
    return round(value, 6)


def _load_facilities() -> Dict[int, dict]:
    """Return facility_id → facility record, restricted to FM service."""
    dat = _dl_dat(FACILITY_URL, "facility.dat")
    out: Dict[int, dict] = {}
    for row in _rows(dat):
        if _get(row, FAC["fac_service"]) != "FM":
            continue
        status = _get(row, FAC["fac_status"])
        if status not in LIVE_FAC_STATUSES:
            continue
        try:
            fid = int(_get(row, FAC["facility_id"]))
        except ValueError:
            continue
        try:
            freq_mhz = float(_get(row, FAC["fac_frequency"]))
        except ValueError:
            continue
        if not (87.5 <= freq_mhz <= 108.1):
            continue  # out of the FM broadcast band
        callsign = _get(row, FAC["fac_callsign"])
        if not callsign or callsign == "NEW":
            continue
        out[fid] = {
            "callsign":  callsign,
            "frequency": int(round(freq_mhz * 1e6)),  # MHz → Hz
            "community": _get(row, FAC["comm_city"]),
            "state":     _get(row, FAC["comm_state"]),
            "status":    status,
        }
    _log(f"  facility.dat: {len(out):,} live FM facilities")
    return out


def _load_engineering(facilities: Dict[int, dict]) -> List[dict]:
    """Join fm_eng_data current records onto the facility set."""
    dat = _dl_dat(FM_ENG_DATA_URL, "fm_eng_data.dat")

    # A facility can have multiple engineering records (main, auxiliary,
    # archived). We prefer the highest effective_erp "C"urrent record.
    best: Dict[int, dict] = {}

    kept = dropped = 0
    for row in _rows(dat):
        if _get(row, ENG["eng_record_type"]) != "C":
            continue
        try:
            fid = int(_get(row, ENG["facility_id"]))
        except ValueError:
            continue
        if fid not in facilities:
            continue

        lat = _dms_to_decimal(
            _get(row, ENG["lat_deg"]),
            _get(row, ENG["lat_min"]),
            _get(row, ENG["lat_sec"]),
            _get(row, ENG["lat_dir"]),
        )
        lon = _dms_to_decimal(
            _get(row, ENG["lon_deg"]),
            _get(row, ENG["lon_min"]),
            _get(row, ENG["lon_sec"]),
            _get(row, ENG["lon_dir"]),
        )
        if lat is None or lon is None:
            dropped += 1
            continue

        try:
            erp_kw = float(_get(row, ENG["effective_erp"]))
        except ValueError:
            erp_kw = 0.0

        station_class = _get(row, ENG["station_class"])

        current = best.get(fid)
        if current is None or erp_kw > current.get("_erp_rank", -1):
            fac = facilities[fid]
            best[fid] = {
                "callsign":      fac["callsign"],
                "frequency":     fac["frequency"],
                "latitude":      lat,
                "longitude":     lon,
                "community":     fac["community"],
                "state":         fac["state"],
                "station_class": station_class,
                "erp_kw":        round(erp_kw, 2) if erp_kw else None,
                "_erp_rank":     erp_kw,
            }
            kept += 1

    results = list(best.values())
    for r in results:
        r.pop("_erp_rank", None)

    _log(f"  fm_eng_data.dat: {kept:,} joined (dropped {dropped:,} without lat/lon)")
    return results


def fetch() -> List[dict]:
    facilities = _load_facilities()
    rows = _load_engineering(facilities)
    rows.sort(key=lambda r: (r["state"], r["community"], r["callsign"]))
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "-o", "--output",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "fm_stations.json",
        ),
        help="Output JSON path (default: <project>/data/fm_stations.json)",
    )
    p.add_argument(
        "--dry", action="store_true",
        help="Print count and first 5 rows; do not write the output file",
    )
    args = p.parse_args()

    rows = fetch()

    if args.dry:
        print(f"total: {len(rows):,} stations")
        for r in rows[:5]:
            print(json.dumps(r, indent=2))
        return 0

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(rows, f, separators=(",", ":"))
    _log(f"wrote {args.output} ({len(rows):,} stations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
