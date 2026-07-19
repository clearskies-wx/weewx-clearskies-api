"""GSFM shelf boundary query — find distance from a point to the continental shelf edge.

Data source: Harris & Macmillan-Lawler (2014) Global Seafloor Geomorphic Features Map.
The pre-processed shelf/slope boundary polyline is shipped as a static data file at
``data/gsfm_shelf_boundary.json``.

The data file contains ONLY the shared boundary between GSFM Shelf and Slope
polygon layers — the physical line where the flat continental shelf transitions
to the steep continental slope. Coastline segments are not present.
"""

from __future__ import annotations

import json
import logging
import math
from functools import lru_cache
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import nearest_points

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).parent.parent / "data" / "gsfm_shelf_boundary.json"

_EARTH_RADIUS_KM = 6371.0


@lru_cache(maxsize=1)
def _load_boundary():
    """Load the shelf/slope boundary geometry from the static data file."""
    if not _DATA_FILE.exists():
        logger.warning(
            "GSFM shelf boundary data not found at %s — "
            "find_shelf_distance() will return None",
            _DATA_FILE,
        )
        return None

    with open(_DATA_FILE) as f:
        data = json.load(f)

    geom = shape({"type": data["type"], "coordinates": data["coordinates"]})
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def find_shelf_distance(lat: float, lon: float) -> float | None:
    """Return the distance in km from (lat, lon) to the continental shelf edge.

    Uses the pre-processed GSFM shelf/slope boundary polyline (the shared edge
    between Shelf and Slope polygons). This is the physical transition from
    flat continental shelf to steep continental slope — where WW3 deep-water
    assumptions fail and SWAN nearshore physics become essential.

    Returns None if the data file is not available.
    """
    boundary = _load_boundary()
    if boundary is None:
        return None

    query_point = Point(lon, lat)
    nearest_pt = nearest_points(boundary, query_point)[0]

    return _haversine_km(lat, lon, nearest_pt.y, nearest_pt.x)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in km between two WGS84 points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))
