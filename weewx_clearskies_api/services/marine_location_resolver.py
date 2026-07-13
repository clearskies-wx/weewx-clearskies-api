"""Spatial deduplication and station substitution for marine locations (T1.3).

Called once at startup to group marine locations by grid cell and compute
station distances.  Results are stored in module-level dicts and queried
per-request via the accessor functions.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

_EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def resolve_grid_groups(
    locations: list[Any],
    dedup_radius_km: float,
) -> dict[str, str]:
    """Group locations by proximity for spatial dedup.

    Uses distance-based clustering: each location is assigned to the
    nearest existing group centroid within dedup_radius_km, or becomes
    a new group centroid if none is close enough.

    Each location object must expose .id, .lat, .lon attributes.

    Returns:
        {location_id: grid_group_key} where the key is a string derived
        from the group's centroid coordinates.
    """
    global _grid_groups, _centroids, _dedup_radius_km  # noqa: PLW0603
    centroids: list[tuple[str, float, float]] = []  # (group_key, lat, lon)
    groups: dict[str, str] = {}

    for loc in locations:
        assigned = False
        for group_key, clat, clon in centroids:
            if _haversine_km(loc.lat, loc.lon, clat, clon) <= dedup_radius_km:
                groups[loc.id] = group_key
                assigned = True
                break
        if not assigned:
            group_key = f"{round(loc.lat, 4)}_{round(loc.lon, 4)}"
            centroids.append((group_key, loc.lat, loc.lon))
            groups[loc.id] = group_key

    _grid_groups = groups
    _centroids = centroids
    _dedup_radius_km = dedup_radius_km
    logger.info(
        "Marine grid groups resolved: %d locations -> %d groups",
        len(locations),
        len(set(groups.values())),
    )
    return groups


def resolve_station_distances(
    locations: list[Any],
    station_lat: float,
    station_lon: float,
    dedup_radius_km: float,
) -> dict[str, dict[str, Any]]:
    """Compute haversine distance from the weewx station to each marine location.

    Each location object must expose .id, .lat, .lon attributes.

    Returns:
        {location_id: {"distance_km": float, "station_served": bool}}
        where station_served is True when distance <= dedup_radius_km.
    """
    global _station_distances  # noqa: PLW0603
    distances: dict[str, dict[str, Any]] = {}
    for loc in locations:
        dist = _haversine_km(station_lat, station_lon, loc.lat, loc.lon)
        distances[loc.id] = {
            "distance_km": dist,
            "station_served": dist <= dedup_radius_km,
        }
    _station_distances = distances
    logger.info("Marine station distances resolved: %d locations", len(distances))
    return distances


# ---------------------------------------------------------------------------
# Module-level state and accessors
# ---------------------------------------------------------------------------

_grid_groups: dict[str, str] = {}
_station_distances: dict[str, dict[str, Any]] = {}
_centroids: list[tuple[str, float, float]] = []
_dedup_radius_km: float = 2.5


def get_grid_group(location_id: str) -> str | None:
    """Return the grid group key for a location, or None if not resolved."""
    return _grid_groups.get(location_id)


def get_grid_group_by_coords(lat: float, lon: float) -> str | None:
    """Find the grid group key for arbitrary coordinates.

    Returns the key of the nearest resolved group centroid within
    dedup_radius_km, or None if no group has been resolved yet or none
    is close enough.
    """
    for group_key, clat, clon in _centroids:
        if _haversine_km(lat, lon, clat, clon) <= _dedup_radius_km:
            return group_key
    return None


def is_station_served(location_id: str) -> bool:
    """Return True if the location is within dedup radius of the weewx station."""
    entry = _station_distances.get(location_id)
    if entry is None:
        return False
    return entry["station_served"]


def get_station_distance(location_id: str) -> float | None:
    """Return distance in km from the weewx station, or None if not resolved."""
    entry = _station_distances.get(location_id)
    if entry is None:
        return None
    return entry["distance_km"]
