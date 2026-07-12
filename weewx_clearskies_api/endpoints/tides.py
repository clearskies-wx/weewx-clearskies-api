"""GET /tides, GET /tides/{location_id} — CO-OPS tide predictions & water levels.

API-MANUAL §16-18.  Reuses the same [marine] location configuration as
endpoints/marine.py (config/marine_config.py's MarineConfig) -- tides are
not a separate config section.  Per ADR-090, all four marine activities
(marine, surf, fishing, beach_safety) use tide data, so "tide-capable"
here means "has at least one coops_station_ids entry configured", not a
specific activity value.

Two routes:

  GET /tides
    No locationId.  Returns a list of MarineLocationSummary (same shape as
    endpoints/marine.py's list) filtered to locations with a CO-OPS station
    configured.  404 "Tide features not configured" when no [marine]
    section is present, or no location has a coops_station_ids entry.

  GET /tides/{location_id}
    404 when the location is not found, or found but has no
    coops_station_ids configured (no way to fetch tide data for it).
    Otherwise calls providers/tides/coops.py's fetch() once for
    predictions + water_level (water_temperature is not requested --
    TideBundle, models/responses.py, has no field for it).

wire_tides_config(settings) duplicates wire_marine_config()'s resolution
logic (endpoints/marine.py) rather than importing from it, keeping the two
endpoint modules independently wireable -- the same pattern as
wire_earthquakes_settings() / wire_marine_alert_zone_ids() each owning
their own module-level state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import configobj
from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.config.marine_config import (
    MarineConfig,
    MarineLocation,
    load_marine_config,
)
from weewx_clearskies_api.models.responses import (
    MarineLocationSummary,
    TideBundle,
    TidePrediction,
    WaterLevel,
    utc_isoformat,
)
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup)
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None


def wire_tides_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by the tides endpoints.

    Accepts the same three input shapes as endpoints/marine.py's
    wire_marine_config() -- see that docstring for the full contract.
    Deliberately duplicated (not imported) so this module has no
    compile-time dependency on endpoints/marine.py.
    """
    global _marine_config  # noqa: PLW0603

    if isinstance(settings, MarineConfig):
        _marine_config = settings
        return

    attr = getattr(settings, "marine_config", None)
    if isinstance(attr, MarineConfig):
        _marine_config = attr
        return

    if isinstance(settings, configobj.ConfigObj):
        try:
            _marine_config = load_marine_config(settings)
        except ValueError:
            logger.error(
                "Invalid [marine] configuration; tide features disabled", exc_info=True
            )
            _marine_config = None
        return

    _marine_config = None


# ---------------------------------------------------------------------------
# Water-level unit conversion (API-MANUAL §16 "Marine unit groups")
# ---------------------------------------------------------------------------

# group_water_level base unit (API-MANUAL §16). CO-OPS predictions/water
# levels are requested with units=metric (providers/tides/coops.py), so
# the canonical value is already in meters.
_BASE_WATER_LEVEL_UNIT = "meter"

_WATER_LEVEL_DISPLAY_LABEL = {"foot": "ft", "meter": "m"}


def _water_level_target_unit() -> str:
    """Return the operator's configured water level unit."""
    default = "foot" if get_target_unit() == "US" else "meter"
    return get_group_unit("group_water_level", default)


def _convert_tide_prediction(prediction: TidePrediction, target_unit: str) -> TidePrediction:
    return prediction.model_copy(
        update={"height": _convert_unit(prediction.height, _BASE_WATER_LEVEL_UNIT, target_unit)}
    )


def _convert_water_level(level: WaterLevel, target_unit: str) -> WaterLevel:
    return level.model_copy(
        update={"height": _convert_unit(level.height, _BASE_WATER_LEVEL_UNIT, target_unit)}
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_tide_capable(location: MarineLocation) -> bool:
    """Per ADR-090: all four marine activities use tides; the gating factor
    is whether a CO-OPS station is configured for this location at all."""
    return bool(location.coops_station_ids)


def _location_summary(location: MarineLocation) -> MarineLocationSummary:
    return MarineLocationSummary(
        locationId=location.id,
        name=location.name,
        coordinates={"lat": location.lat, "lon": location.lon},
        activities=list(location.activities),
        currentConditions=None,
        currentTide=None,
        activeAlerts=None,
        surfRating=None,
        beachSafetyLevel=None,
    )


def _find_tide_capable_location(location_id: str) -> MarineLocation | None:
    if _marine_config is None:
        return None
    for location in _marine_config.locations:
        if location.id == location_id and _is_tide_capable(location):
            return location
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tides", summary="Tide-capable marine locations", tags=["Tides"])
def list_tide_locations() -> dict:
    """List configured locations that have a CO-OPS station configured.

    404 "Tide features not configured" when no [marine] section is present,
    or no location has a coops_station_ids entry.
    """
    if _marine_config is None:
        raise HTTPException(status_code=404, detail="Tide features not configured")

    tide_locations = [loc for loc in _marine_config.locations if _is_tide_capable(loc)]
    if not tide_locations:
        raise HTTPException(status_code=404, detail="Tide features not configured")

    now_str = utc_isoformat(datetime.now(tz=UTC))
    target_unit = _water_level_target_unit()
    summaries = [_location_summary(loc) for loc in tide_locations]

    return {
        "data": [s.model_dump(by_alias=True, exclude_none=False) for s in summaries],
        "units": {"waterLevel": _WATER_LEVEL_DISPLAY_LABEL[target_unit]},
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("tides").model_dump(by_alias=True),
        "generatedAt": now_str,
    }


@router.get(
    "/tides/{location_id}",
    summary="Tide predictions and water levels for one location",
    tags=["Tides"],
)
def get_tide_location(location_id: str) -> dict:
    """CO-OPS tide predictions (72h) + observed water levels (24h) for one location.

    404 when the location is not found, or found but has no
    coops_station_ids configured.
    """
    location = _find_tide_capable_location(location_id)
    if location is None:
        raise HTTPException(
            status_code=404, detail=f"Tide location {location_id!r} not found"
        )

    now_str = utc_isoformat(datetime.now(tz=UTC))
    target_unit = _water_level_target_unit()

    predictions: list[TidePrediction] = []
    water_levels: list[WaterLevel] = []
    try:
        from weewx_clearskies_api.providers.tides import coops  # noqa: PLC0415

        result = coops.fetch(
            station_id=location.coops_station_ids[0],
            products=("predictions", "water_level"),
        )
        predictions = [
            _convert_tide_prediction(p, target_unit) for p in result.get("predictions", [])
        ]
        water_levels = [
            _convert_water_level(w, target_unit) for w in result.get("water_levels", [])
        ]
    except Exception:
        logger.warning(
            "CO-OPS fetch failed for tide location %r (station %s)",
            location_id,
            location.coops_station_ids[0],
            exc_info=True,
        )

    bundle = TideBundle(
        locationId=location.id,
        locationName=location.name,
        coordinates={"lat": location.lat, "lon": location.lon},
        predictions=predictions,
        waterLevels=water_levels,
        source="coops",
        generatedAt=now_str,
    )

    return {
        "data": bundle.model_dump(by_alias=True, exclude_none=False),
        "units": {"waterLevel": _WATER_LEVEL_DISPLAY_LABEL[target_unit]},
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("tides").model_dump(by_alias=True),
        "generatedAt": now_str,
    }
