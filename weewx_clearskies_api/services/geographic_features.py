"""Geographic features service — Overpass API query builder and cache layer.

Queries OpenStreetMap via the Overpass API for administrative boundaries,
roads, and water features within a configurable bounding box.  Results are
returned as a GeoJSON FeatureCollection and cached with a long TTL (default
90 days) because OSM base data changes slowly.

Bounds cascade (resolved in get_geographic_features):
  1. settings.bounds — explicit "south,west,north,east" CSV (highest priority)
  2. radar_settings.librewxr_bounds — reuse operator's LibreWxR bounding box
  3. Computed from station lat/lon ± radius_km (fallback)

Overpass response → GeoJSON conversion:
  out geom; returns geometry inline on each element.  way elements produce
  LineString features; relation elements produce MultiLineString features.
  Each Feature gets a type property: "boundary", "road", or "water".

On any error (HTTP, parse, timeout) the service logs a WARNING and returns an
empty FeatureCollection — the endpoint still returns HTTP 200 so the dashboard
renders without the overlay rather than showing an error.

Attribution: © OpenStreetMap contributors (ODbL)
  https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import TYPE_CHECKING, Any

import httpx

from weewx_clearskies_api.providers._common.cache import get_cache

if TYPE_CHECKING:
    from weewx_clearskies_api.config.settings import GeographicFeaturesSettings, RadarSettings

logger = logging.getLogger(__name__)

_EMPTY_FC: dict[str, Any] = {"type": "FeatureCollection", "features": []}


def build_overpass_query(south: float, west: float, north: float, east: float) -> str:
    """Return an Overpass QL query for the given bounding box.

    Fetches:
      - Administrative boundaries (relation, admin_level 2 or 4)
      - Major roads (way, highway motorway/trunk/primary)
      - Natural water bodies (relation, natural=water)
      - Rivers (way, waterway=river)

    Uses ``out geom;`` so geometry is included inline — no separate geometry
    lookup step needed.

    Args:
        south: Southern latitude of the bounding box (decimal degrees).
        west: Western longitude of the bounding box (decimal degrees).
        north: Northern latitude of the bounding box (decimal degrees).
        east: Eastern longitude of the bounding box (decimal degrees).

    Returns:
        Overpass QL query string ready to POST.
    """
    bbox = f"{south},{west},{north},{east}"
    return (
        f'[out:json][timeout:60];\n'
        f'(\n'
        f'  relation["boundary"="administrative"]["admin_level"~"2|4"]({bbox});\n'
        f'  way["highway"~"motorway|trunk|primary"]({bbox});\n'
        f'  relation["natural"="water"]({bbox});\n'
        f'  way["waterway"="river"]({bbox});\n'
        f');\n'
        f'out geom;\n'
    )


def _element_to_feature(element: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a single Overpass element to a GeoJSON Feature.

    Handles ``way`` elements (LineString) and ``relation`` elements
    (MultiLineString built from member ways).  Returns None for elements
    that cannot be converted (missing geometry, empty coordinates, etc.).

    Args:
        element: A single element dict from the Overpass JSON response.

    Returns:
        GeoJSON Feature dict, or None if the element should be skipped.
    """
    tags: dict[str, str] = element.get("tags") or {}
    elem_type = element.get("type", "")

    # Determine the feature type from tags.
    if tags.get("boundary") == "administrative":
        feature_type = "boundary"
    elif "highway" in tags:
        feature_type = "road"
    elif tags.get("natural") == "water" or "waterway" in tags:
        feature_type = "water"
    else:
        # Unclassifiable — skip.
        return None

    properties: dict[str, Any] = {"type": feature_type}
    name = tags.get("name")
    if name:
        properties["name"] = name

    geometry: dict[str, Any] | None = None

    if elem_type == "way":
        raw_geom = element.get("geometry") or []
        coords = [[pt["lon"], pt["lat"]] for pt in raw_geom if "lat" in pt and "lon" in pt]
        if len(coords) < 2:
            return None
        geometry = {"type": "LineString", "coordinates": coords}

    elif elem_type == "relation":
        members: list[dict[str, Any]] = element.get("members") or []
        lines: list[list[list[float]]] = []
        for member in members:
            if member.get("type") != "way":
                continue
            raw_geom = member.get("geometry") or []
            coords = [[pt["lon"], pt["lat"]] for pt in raw_geom if "lat" in pt and "lon" in pt]
            if len(coords) >= 2:
                lines.append(coords)
        if not lines:
            return None
        geometry = {"type": "MultiLineString", "coordinates": lines}

    else:
        return None

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": properties,
    }


def fetch_overpass(query: str, endpoint: str) -> dict[str, Any]:
    """POST the Overpass QL query and convert the response to GeoJSON.

    Uses httpx.Client (sync) with a 60-second timeout.  On any failure
    (HTTP error, parse error, timeout) logs a WARNING and returns an empty
    FeatureCollection so the calling endpoint can still respond HTTP 200.

    Args:
        query: Overpass QL query string (from build_overpass_query).
        endpoint: Overpass API URL (e.g. "https://overpass-api.de/api/interpreter").

    Returns:
        GeoJSON FeatureCollection dict.
    """
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(endpoint, data={"data": query})
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        logger.warning(
            "Overpass API request timed out after 60 s: %s — returning empty FeatureCollection",
            exc,
        )
        return dict(_EMPTY_FC)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Overpass API returned HTTP %d — returning empty FeatureCollection: %s",
            exc.response.status_code,
            exc,
        )
        return dict(_EMPTY_FC)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Overpass API request failed (%s: %s) — returning empty FeatureCollection",
            type(exc).__name__,
            exc,
        )
        return dict(_EMPTY_FC)

    elements: list[dict[str, Any]] = payload.get("elements", [])
    features: list[dict[str, Any]] = []
    for element in elements:
        feature = _element_to_feature(element)
        if feature is not None:
            features.append(feature)

    logger.info(
        "Overpass query returned %d elements → %d GeoJSON features",
        len(elements),
        len(features),
    )
    return {"type": "FeatureCollection", "features": features}


def get_geographic_features(
    settings: GeographicFeaturesSettings,
    radar_settings: RadarSettings,
    station_lat: float,
    station_lon: float,
) -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of geographic features, cache-first.

    Bounds cascade (highest to lowest priority):
      1. settings.bounds — explicit "south,west,north,east" CSV
      2. radar_settings.librewxr_bounds — operator's LibreWxR bounding box
      3. Computed from station_lat/station_lon ± settings.radius_km

    Cache key is a SHA-256 hash of the resolved bounding box coordinates so
    different bbox configurations share no entries.

    Args:
        settings: GeographicFeaturesSettings from api.conf [geographic_features].
        radar_settings: RadarSettings; provides librewxr_bounds for the cascade.
        station_lat: Station latitude in decimal degrees (WGS84).
        station_lon: Station longitude in decimal degrees (WGS84).

    Returns:
        GeoJSON FeatureCollection dict (possibly empty on errors).
    """
    # Resolve bounds via cascade.
    south: float
    west: float
    north: float
    east: float

    if settings.bounds is not None:
        try:
            parts = settings.bounds.split(",")
            south, west, north, east = (float(p) for p in parts)
        except (ValueError, TypeError):
            logger.warning(
                "geographic_features bounds %r is not a valid 'south,west,north,east' CSV — "
                "falling back to radius_km computation",
                settings.bounds,
            )
            south, west, north, east = _bounds_from_radius(
                station_lat, station_lon, settings.radius_km
            )
    elif radar_settings.librewxr_bounds is not None:
        try:
            parts = radar_settings.librewxr_bounds.split(",")
            south, west, north, east = (float(p) for p in parts)
            logger.debug(
                "geographic_features: using librewxr_bounds %r", radar_settings.librewxr_bounds
            )
        except (ValueError, TypeError):
            logger.warning(
                "radar librewxr_bounds %r is not a valid 'south,west,north,east' CSV — "
                "falling back to radius_km computation",
                radar_settings.librewxr_bounds,
            )
            south, west, north, east = _bounds_from_radius(
                station_lat, station_lon, settings.radius_km
            )
    else:
        south, west, north, east = _bounds_from_radius(
            station_lat, station_lon, settings.radius_km
        )
        logger.debug(
            "geographic_features: computed bounds %.4f,%.4f,%.4f,%.4f from "
            "station (%.4f, %.4f) ± %.1f km",
            south, west, north, east, station_lat, station_lon, settings.radius_km,
        )

    # Build a stable cache key from the resolved coordinates.
    bbox_str = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    cache_key = "geo_features:" + hashlib.sha256(bbox_str.encode()).hexdigest()

    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        logger.debug("geographic_features cache hit for bbox %s", bbox_str)
        return cached

    logger.debug("geographic_features cache miss for bbox %s — fetching from Overpass", bbox_str)
    query = build_overpass_query(south, west, north, east)
    result = fetch_overpass(query, settings.overpass_endpoint)

    ttl_seconds = settings.refresh_days * 86400
    cache.set(cache_key, result, ttl_seconds)

    return result


def _bounds_from_radius(
    lat: float, lon: float, radius_km: float
) -> tuple[float, float, float, float]:
    """Compute a bounding box from a centre point and radius.

    Uses flat-earth approximations suitable for the distances involved:
      1° latitude ≈ 111 km (constant)
      1° longitude ≈ 111 km × cos(latitude) (varies with latitude)

    Args:
        lat: Centre latitude in decimal degrees.
        lon: Centre longitude in decimal degrees.
        radius_km: Radius in kilometres.

    Returns:
        (south, west, north, east) tuple in decimal degrees.
    """
    delta_lat = radius_km / 111.0
    delta_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return (
        lat - delta_lat,
        lon - delta_lon,
        lat + delta_lat,
        lon + delta_lon,
    )
