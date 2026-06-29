"""GET /geographic-features — OSM geographic features within configured bounds.

Returns a GeoJSON FeatureCollection of administrative boundaries, major roads,
and water features from OpenStreetMap via the Overpass API.  Results are cached
with a long TTL (default 90 days) because base OSM data changes slowly.

Behavior:
  1. settings.geographic_features.enabled is False → 200 with empty FeatureCollection
  2. Cache hit → return cached FeatureCollection immediately
  3. Cache miss → live Overpass fetch, cache result, return FeatureCollection
  4. Overpass failure → 200 with empty FeatureCollection (graceful degradation)

Bounds cascade (resolved in services/geographic_features.py):
  1. [geographic_features] bounds — explicit "south,west,north,east" CSV
  2. [radar] librewxr_bounds — reuse operator's LibreWxR bounding box
  3. Computed from station lat/lon ± radius_km

Public endpoint — no authentication required (same as /earthquakes/faults).

Attribution: © OpenStreetMap contributors (ODbL)
  https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.geographic_features import get_geographic_features
from weewx_clearskies_api.services.station import get_station_info

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level settings wiring (populated at startup by wire_geographic_features_settings).
# wire_geographic_features_settings() is called from __main__.py after settings load.
# Tests that don't need provider config leave these at the module defaults.
# ---------------------------------------------------------------------------

_geographic_features_settings = None   # GeographicFeaturesSettings | None
_radar_settings = None                  # RadarSettings | None


def wire_geographic_features_settings(settings: object) -> None:
    """Store geographic features settings for use by the endpoint.

    Extracts settings.geographic_features and settings.radar from the loaded
    Settings object.  Called from __main__.py after settings load.
    Tests that don't need config leave module defaults (None) as-is.
    """
    global _geographic_features_settings, _radar_settings  # noqa: PLW0603
    _geographic_features_settings = getattr(settings, "geographic_features", None)
    _radar_settings = getattr(settings, "radar", None)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/geographic-features",
    summary="Geographic features within configured bounds",
    tags=["Geographic Features"],
)
def get_geographic_features_endpoint() -> dict:
    """Return OSM geographic features as a GeoJSON FeatureCollection.

    Features include administrative boundaries, major roads (motorway, trunk,
    primary), natural water bodies, and rivers within the configured bounding
    box.  See the bounds cascade in services/geographic_features.py.

    When the endpoint is disabled via [geographic_features] enabled = false,
    an empty FeatureCollection is returned (HTTP 200) rather than a 503 error
    so the dashboard can still load without the overlay.

    Returns a dict with:
      - data: GeoJSON FeatureCollection (possibly empty)
      - attribution: OSM attribution string (required by ODbL)
      - generatedAt: ISO 8601 UTC timestamp
    """
    now_str = utc_isoformat(datetime.now(tz=UTC))

    # Build the empty-FeatureCollection response shape used in two places.
    empty_response = {
        "data": {"type": "FeatureCollection", "features": []},
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "generatedAt": now_str,
    }

    # When the endpoint is disabled, return an empty collection rather than 503.
    if _geographic_features_settings is not None and not _geographic_features_settings.enabled:
        logger.debug("geographic-features endpoint is disabled — returning empty FeatureCollection")
        return empty_response

    try:
        station = get_station_info()
    except RuntimeError as exc:
        logger.error(
            "Station metadata not available at geographic-features endpoint — "
            "this should not happen after successful startup"
        )
        raise HTTPException(status_code=503, detail="Service starting") from exc

    # Use module-level wired settings; fall back to default instances when not wired
    # (e.g. tests that call the endpoint without startup).
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        GeographicFeaturesSettings,
        RadarSettings,
    )

    geo_settings = _geographic_features_settings if _geographic_features_settings is not None \
        else GeographicFeaturesSettings({})
    radar_settings = _radar_settings if _radar_settings is not None else RadarSettings({})

    data = get_geographic_features(
        settings=geo_settings,
        radar_settings=radar_settings,
        station_lat=station.latitude,
        station_lon=station.longitude,
    )

    return {
        "data": data,
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "generatedAt": now_str,
    }
