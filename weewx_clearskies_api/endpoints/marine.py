"""GET /marine, GET /marine/{location_id} — marine buoy/wave conditions (API-MANUAL §16-18).

Two routes:

  GET /marine
    No locationId.  Returns a list of MarineLocationSummary, one per
    configured [marine] location (id, name, coordinates, activities).
    currentConditions/currentTide/activeAlerts/surfRating/beachSafetyLevel
    are left None -- full population is deferred until the cache warmer
    infrastructure exists to make a multi-location summary cheap to serve
    (per brief).  404 "Marine features not configured" when no [marine]
    section is present or it has zero locations.

    Ruling (lead, T5.1/T5.2 round): the API-MANUAL §18 line "Without
    locationId, the endpoint returns data for the first configured
    location" is drafting shorthand -- the dashboard's location-card grid
    (T7.1) needs a list. Lead will correct the manual text at doc-code
    sync; this endpoint implements the list behavior.

  GET /marine/{location_id}
    Looks up the location by id in the configured [marine] locations.
    404 when not found.  Aggregates, independently and best-effort:
      - NDBC buoy observation (providers/buoy/ndbc.py), if the location has
        ndbc_station_ids configured.
      - WaveWatch III forecast (providers/marine/wavewatch.py), always
        attempted (global coverage, keyed by lat/lon -- no station id
        needed).
      - NWS marine zone text forecast (providers/marine/nws_marine.py), if
        the location has nws_marine_zone_id configured.
    Each provider call is wrapped in try/except -- a single provider outage
    never fails the whole response (brief: "The endpoint never fails
    because one provider is down").  CO-OPS tide data is intentionally NOT
    fetched here: MarineBundle (models/responses.py) has no field for it --
    tides live at GET /tides[/{location_id}] (tides.py, TideBundle).

Unit conversion: only the 5 marine-specific groups (API-MANUAL §16 "Marine
unit groups") are converted here -- group_wave_height, group_wave_period
(single-unit, no-op), group_ocean_speed, group_water_level, and
group_visibility.  Existing land groups incidentally present on
MarineObservation (group_temperature: airTemp/waterTemp/dewpoint;
group_pressure: pressure/pressureTendency) are left in the providers'
canonical base units (Celsius, hPa) -- out of scope per brief, and
services/units.py's field->group table (_GROUP_MEMBERS) has no entries for
these marine-specific field names to convert them through anyway.

wire_marine_config(settings) is called once at startup by __main__.py
(lead-owned; not modified here).  See its docstring for the accepted input
shapes.
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
    MarineBundle,
    MarineForecastPoint,
    MarineLocationSummary,
    MarineObservation,
    MarineTextForecast,
    utc_isoformat,
)
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock
from weewx_clearskies_api.services.units import get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup)
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None


def wire_marine_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by the marine endpoints.

    Defensive resolution -- accepts any of:
      1. A MarineConfig instance directly (already parsed).
      2. Any object exposing a `.marine_config` attribute holding a
         MarineConfig (e.g. a future Settings.marine_config field).
      3. A raw configobj.ConfigObj -- parsed here via
         config/marine_config.py's load_marine_config().

    Anything else (including a bare `object()` with neither shape) leaves
    module state at None -- both endpoints then report 404 "Marine features
    not configured", matching load_marine_config()'s own "[marine] section
    absent -> None, zero impact" contract.

    Called from __main__.py after settings/config load (mirrors the
    getattr()-based defensive pattern in wire_earthquakes_settings(),
    endpoints/earthquakes.py).  Not called by app.py or __main__.py in this
    change -- router registration and the startup call site are handled
    separately.
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
                "Invalid [marine] configuration; marine features disabled", exc_info=True
            )
            _marine_config = None
        return

    _marine_config = None


# ---------------------------------------------------------------------------
# Marine unit conversion (API-MANUAL §16 "Marine unit groups")
# ---------------------------------------------------------------------------

# Canonical base units the marine provider modules emit (API-MANUAL §16
# "Marine unit groups" table's Base unit column). group_wave_period has a
# single valid unit (second) -- convert() is never called for it.
_BASE_WAVE_HEIGHT_UNIT = "meter"
_BASE_WATER_LEVEL_UNIT = "meter"
_BASE_OCEAN_SPEED_UNIT = "meter_per_second"
_BASE_VISIBILITY_UNIT = "nautical_mile"

_MARINE_DISPLAY_LABEL = {
    "foot": "ft",
    "meter": "m",
    "second": "s",
    "knot": "kt",
    "nautical_mile": "nm",
}


def _marine_target_units() -> dict[str, str]:
    """Return group -> weewx-internal target unit for the 5 marine groups.

    group_ocean_speed, group_wave_period, and group_visibility are fixed at
    knot / second / nautical_mile in *every* preset (API-MANUAL §16: "Even
    countries that use m/s on land use knots at sea" -- maritime convention
    overrides land convention in all three unit systems).  group_wave_height
    and group_water_level are the only two that vary: foot for US, meter for
    METRIC/METRICWX.

    Does not honor per-group [StdReport] overrides -- services/units.py's
    get_units_block() has no field-name entries for marine fields to look
    up an override through (out of scope for this endpoint pair; see module
    docstring).
    """
    height_unit = "foot" if get_target_unit() == "US" else "meter"
    return {
        "group_wave_height": height_unit,
        "group_wave_period": "second",
        "group_water_level": height_unit,
        "group_ocean_speed": "knot",
        "group_visibility": "nautical_mile",
    }


def _marine_units_block(targets: dict[str, str]) -> dict[str, str]:
    return {
        "waveHeight": _MARINE_DISPLAY_LABEL[targets["group_wave_height"]],
        "wavePeriod": _MARINE_DISPLAY_LABEL[targets["group_wave_period"]],
        "waterLevel": _MARINE_DISPLAY_LABEL[targets["group_water_level"]],
        "windSpeed": _MARINE_DISPLAY_LABEL[targets["group_ocean_speed"]],
        "visibility": _MARINE_DISPLAY_LABEL[targets["group_visibility"]],
    }


def _convert_observation(
    obs: MarineObservation, targets: dict[str, str]
) -> MarineObservation:
    """Convert the marine-group fields on a MarineObservation to display units.

    windDirection/meanWaveDirection (degrees) and temperature/pressure
    fields are passed through unchanged -- see module docstring.
    """
    return obs.model_copy(
        update={
            "windSpeed": _convert_unit(
                obs.windSpeed, _BASE_OCEAN_SPEED_UNIT, targets["group_ocean_speed"]
            ),
            "windGust": _convert_unit(
                obs.windGust, _BASE_OCEAN_SPEED_UNIT, targets["group_ocean_speed"]
            ),
            "waveHeight": _convert_unit(
                obs.waveHeight, _BASE_WAVE_HEIGHT_UNIT, targets["group_wave_height"]
            ),
            "tideLevel": _convert_unit(
                obs.tideLevel, _BASE_WATER_LEVEL_UNIT, targets["group_water_level"]
            ),
            "visibility": _convert_unit(
                obs.visibility, _BASE_VISIBILITY_UNIT, targets["group_visibility"]
            ),
        }
    )


def _convert_forecast_point(
    point: MarineForecastPoint, targets: dict[str, str]
) -> MarineForecastPoint:
    return point.model_copy(
        update={
            "waveHeight": _convert_unit(
                point.waveHeight, _BASE_WAVE_HEIGHT_UNIT, targets["group_wave_height"]
            ),
            "swellHeight": _convert_unit(
                point.swellHeight, _BASE_WAVE_HEIGHT_UNIT, targets["group_wave_height"]
            ),
            "windWaveHeight": _convert_unit(
                point.windWaveHeight, _BASE_WAVE_HEIGHT_UNIT, targets["group_wave_height"]
            ),
            "windSpeed": _convert_unit(
                point.windSpeed, _BASE_OCEAN_SPEED_UNIT, targets["group_ocean_speed"]
            ),
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _location_summary(location: MarineLocation) -> MarineLocationSummary:
    """Build a MarineLocationSummary with only config-derived fields populated."""
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


def _find_location(location_id: str) -> MarineLocation | None:
    if _marine_config is None:
        return None
    for location in _marine_config.locations:
        if location.id == location_id:
            return location
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/marine", summary="Configured marine locations", tags=["Marine"])
def list_marine_locations() -> dict:
    """List all configured marine locations (summary card per location).

    404 "Marine features not configured" when no [marine] section is
    present or it has zero locations.
    """
    if _marine_config is None or not _marine_config.locations:
        raise HTTPException(status_code=404, detail="Marine features not configured")

    now_str = utc_isoformat(datetime.now(tz=UTC))
    targets = _marine_target_units()
    summaries = [_location_summary(location) for location in _marine_config.locations]

    return {
        "data": [s.model_dump(by_alias=True, exclude_none=False) for s in summaries],
        "units": _marine_units_block(targets),
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("marine").model_dump(by_alias=True),
        "generatedAt": now_str,
    }


@router.get(
    "/marine/{location_id}",
    summary="Marine conditions for one location",
    tags=["Marine"],
)
def get_marine_location(location_id: str) -> dict:
    """Aggregate NDBC + WaveWatch III + NWS marine text for one location.

    404 when the location is not in the configured [marine] locations
    (including when marine is not configured at all).  Each provider call
    is independently best-effort -- see module docstring.
    """
    location = _find_location(location_id)
    if location is None:
        raise HTTPException(
            status_code=404, detail=f"Marine location {location_id!r} not found"
        )

    now_str = utc_isoformat(datetime.now(tz=UTC))
    targets = _marine_target_units()

    observation: MarineObservation | None = None
    if location.ndbc_station_ids:
        try:
            from weewx_clearskies_api.providers.buoy import ndbc  # noqa: PLC0415

            result = ndbc.fetch(station_id=location.ndbc_station_ids[0])
            raw_obs = result.get("observation")
            if raw_obs is not None:
                spectral = result.get("spectral") or None
                if spectral:
                    raw_obs = raw_obs.model_copy(update={"spectralComponents": spectral})
                observation = _convert_observation(raw_obs, targets)
        except Exception:
            logger.warning(
                "NDBC fetch failed for marine location %r (station %s)",
                location_id,
                location.ndbc_station_ids[0],
                exc_info=True,
            )

    forecast: list[MarineForecastPoint] = []
    try:
        from weewx_clearskies_api.providers.marine import wavewatch  # noqa: PLC0415

        ww_result = wavewatch.fetch(lat=location.lat, lon=location.lon)
        forecast = [
            _convert_forecast_point(point, targets)
            for point in ww_result.get("forecast", [])
        ]
    except Exception:
        logger.warning(
            "WaveWatch III fetch failed for marine location %r", location_id, exc_info=True
        )

    text_forecast: list[MarineTextForecast] = []
    if location.nws_marine_zone_id:
        try:
            from weewx_clearskies_api.providers.marine import nws_marine  # noqa: PLC0415

            text_forecast = nws_marine.fetch(zone_id=location.nws_marine_zone_id)
        except Exception:
            logger.warning(
                "NWS marine zone text fetch failed for marine location %r (zone %s)",
                location_id,
                location.nws_marine_zone_id,
                exc_info=True,
            )

    bundle = MarineBundle(
        locationId=location.id,
        locationName=location.name,
        coordinates={"lat": location.lat, "lon": location.lon},
        observation=observation,
        forecast=forecast,
        textForecast=text_forecast,
        source="ndbc+wavewatch+nws_marine",
        generatedAt=now_str,
    )

    return {
        "data": bundle.model_dump(by_alias=True, exclude_none=False),
        "units": _marine_units_block(targets),
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("marine").model_dump(by_alias=True),
        "generatedAt": now_str,
    }
