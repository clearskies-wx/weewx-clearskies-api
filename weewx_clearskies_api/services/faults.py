"""Fault line service — loads and clips GEM Active Faults GeoJSON (CC-BY-SA 4.0).

The GEM Global Active Faults Database (https://github.com/GEMScienceTools/
gem-global-active-faults) is a community-maintained catalog of active fault
traces distributed as GeoJSON.  This module:

  1. Lazy-loads the GeoJSON file from data/gem_active_faults.geojson once and
     caches it in a module-level variable.
  2. Clips the fault feature collection to only features with at least one
     vertex within a caller-specified radius of a station lat/lon.

If the GeoJSON file is absent the module logs a warning and returns an empty
FeatureCollection — the endpoint still responds 200 so the dashboard can
display the map without fault overlays rather than showing an error.

License note: GEM data is CC-BY-SA 4.0.  The /earthquakes/faults endpoint
includes an attribution field in the response body.  The operator must not
redistribute the GEM data under a more restrictive licence.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level cache — loaded once at first call; None until then.
_faults_data: dict[str, Any] | None = None


def _load_faults() -> dict[str, Any]:
    """Load the GEM fault GeoJSON from disk, caching in module state.

    Returns the full GeoJSON dict.  On missing file returns an empty
    FeatureCollection so callers can always iterate .get("features", []).
    """
    global _faults_data  # noqa: PLW0603
    if _faults_data is not None:
        return _faults_data

    path = Path(__file__).parent.parent / "data" / "gem_active_faults.geojson"
    if not path.exists():
        logger.warning(
            "GEM fault data not found at %s — /earthquakes/faults will return empty. "
            "Download the full dataset from "
            "https://github.com/GEMScienceTools/gem-global-active-faults",
            path,
        )
        _faults_data = {"type": "FeatureCollection", "features": []}
        return _faults_data

    with open(path, encoding="utf-8") as f:
        _faults_data = json.load(f)

    feature_count = len(_faults_data.get("features", []))
    logger.info("Loaded %d fault features from %s", feature_count, path)
    return _faults_data


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in km between two WGS84 points.

    Uses the haversine formula.  Both lat/lon pairs are decimal degrees.
    GeoJSON coordinates are [lon, lat] order; callers must unpack accordingly.
    """
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_faults_within_radius(lat: float, lon: float, radius_km: float) -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of faults within radius_km of (lat, lon).

    A fault feature is included if any of its vertices lies within radius_km
    of the given point.  This is an approximation — a fault segment that passes
    through the radius without any vertex inside will be missed — but it is fast
    and appropriate for display purposes.

    Handles both LineString and MultiLineString geometry types.  Features with
    other geometry types (e.g. Point, Polygon) are silently skipped; they are
    not present in the GEM dataset.

    Args:
        lat: Station latitude in decimal degrees (WGS84).
        lon: Station longitude in decimal degrees (WGS84).
        radius_km: Search radius in kilometres.

    Returns:
        GeoJSON FeatureCollection dict with matching fault features.
    """
    all_faults = _load_faults()
    features: list[dict[str, Any]] = []

    for feature in all_faults.get("features", []):
        geom = feature.get("geometry") or {}
        geom_type = geom.get("type", "")
        coords = geom.get("coordinates", [])

        # Build a flat list of coordinate lists to iterate uniformly.
        if geom_type == "LineString":
            # coords is [[lon, lat], [lon, lat], ...]
            coord_lists = [coords]
        elif geom_type == "MultiLineString":
            # coords is [[[lon, lat], ...], [[lon, lat], ...], ...]
            coord_lists = coords
        else:
            # Skip unsupported geometry types.
            continue

        # Check if any vertex is within the radius.
        found = False
        for coord_list in coord_lists:
            for coord in coord_list:
                # GeoJSON coordinate order is [longitude, latitude].
                if _haversine_km(lat, lon, coord[1], coord[0]) <= radius_km:
                    found = True
                    break
            if found:
                break

        if found:
            features.append(feature)

    return {"type": "FeatureCollection", "features": features}


def reset_faults_for_tests() -> None:
    """Reset module-level fault cache.  Used in tests only."""
    global _faults_data  # noqa: PLW0603
    _faults_data = None
