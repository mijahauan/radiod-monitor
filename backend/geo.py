"""Shared geographic helpers."""
import math
from typing import Optional, Tuple

import maidenhead


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points (decimal degrees) in kilometers."""
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return c * 6371.0


def parse_location(location_input: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Parse a user-supplied location: either "lat,lon" or a Maidenhead grid.
    Returns (lat, lon) or (None, None) if unparseable.
    """
    if not location_input:
        return None, None

    # Try "lat,lon" first.
    parts = location_input.replace(" ", "").split(",")
    if len(parts) == 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass

    # Fall back to Maidenhead grid.
    try:
        lat, lon = maidenhead.to_location(location_input.strip().upper())
        return lat, lon
    except Exception:
        return None, None
