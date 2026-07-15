"""GET /marine, GET /marine/{location_id} — marine buoy/wave conditions (API-MANUAL §16-18).

Two routes:

  GET /marine
    No locationId.  Returns a list of MarineLocationSummary, one per
    configured [marine] location (id, name, coordinates, activities).
    currentConditions is populated best-effort from NDBC buoy observation
    (wind/water temp/spectral fields), with its waveHeight field specifically
    overridden by the NWPS + wave_transform -> WaveWatch III -> NDBC fallback
    chain (T2.1, API-MANUAL §16 "Card data source contract") so that
    surf-enabled locations get spot-specific wave heights instead of every
    location sharing one buoy's raw offshore reading.  currentTide is always
    None on this card summary (ADR-091) -- see below.  weatherCode/isDay are
    sourced from the marine weather cache when available.
    surfRating/beachSafetyLevel are left None (requires enrichment pipeline,
    Phase 2).  activeAlerts IS populated (Marine Remediation Plan
    T3.5, un-deferred by lead ruling): the NWS alerts provider
    (providers/alerts/nws.py) already caches its response for 5 minutes,
    so a per-location fetch here is cheap enough not to need the cache
    warmer.  Each alert carries a `headline` and an `alertType`
    classified from its NWS `event` string by _classify_alert_type() --
    "marineZone" | "coastalFlood" | "beachHazard" -- so the dashboard can
    filter alerts per activity tab.  A single location's alert-fetch
    failure degrades that location's activeAlerts to None (independent
    best-effort, same pattern as GET /marine/{location_id}'s provider
    calls below) rather than failing the whole list.  Location photos
    are static files served by Caddy at /marine-photos/* -- the API is
    not involved in photo URL resolution.  404 "Marine features not
    configured" when no [marine] section is present or it has zero
    locations.

    Ruling (lead, T5.1/T5.2 round): the API-MANUAL §18 line "Without
    locationId, the endpoint returns data for the first configured
    location" is drafting shorthand -- the dashboard's location-card grid
    (T7.1) needs a list. Lead will correct the manual text at doc-code
    sync; this endpoint implements the list behavior.

  GET /marine/{location_id}
    Looks up the location by id in the configured [marine] locations.
    404 when not found.  Aggregates, independently and best-effort:
      - NDBC buoy observation (providers/buoy/ndbc.py), if the location has
        ndbc_station_ids configured.  Marine Remediation Plan Phase 2 T2.1
        enriches this raw buoy snapshot in place (API-MANUAL §18 "Detail
        endpoint enrichment contract") because most buoys do not report
        wind, air temp, pressure, visibility, or weather conditions:
        windSpeed/windDirection/windGust/airTemp/pressure from station
        hardware (marine_location_resolver.is_station_served()) else
        marine_weather_cache; visibility/weatherCode/isDay from
        marine_weather_cache only; waveHeight overridden by the same
        NWPS+wave_transform -> WaveWatch III -> NDBC fallback chain
        _location_summary() uses (not refactored into a shared function --
        different response shapes); waterTemp overridden by
        ocean_data_resolver.resolve(). Each source is independently
        best-effort and only overrides a field when it actually returns a
        non-null value -- the raw NDBC value survives untouched otherwise.
      - Wave forecast (T2.2): NWPS nearshore data (providers/marine/nwps.py)
        when the location has nwps_wfo configured and the fetch succeeds --
        falls back to WaveWatch III offshore forecast
        (providers/marine/wavewatch.py, global coverage, keyed by lat/lon --
        no station id needed) when NWPS is not configured or its fetch
        fails. The bundle's `source` field reflects which path supplied the
        forecast ("nwps+ndbc+nws_marine" vs "ndbc+wavewatch+nws_marine").
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
from sqlalchemy.orm import Session

from weewx_clearskies_api.config.marine_config import (
    MarineConfig,
    MarineLocation,
    load_marine_config,
)
from weewx_clearskies_api.db.session import get_engine
from weewx_clearskies_api.enrichment import wave_transform
from weewx_clearskies_api.models.responses import (
    MarineAlertSummary,
    MarineBundle,
    MarineForecastPoint,
    MarineLocationSummary,
    MarineObservation,
    MarineTextForecast,
    utc_isoformat,
)
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit
from weewx_clearskies_api.units.labels import get_label as _get_unit_label

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

# NDBC buoy standard-met waterTemp wire unit (T1.2). This is the land
# group_temperature group (not one of the 5 marine-specific groups above) --
# waterTemp is documented as group_temperature in API-MANUAL §16's
# MarineObservation table, so it converts against the operator's configured
# group_temperature target the same way outTemp does on /current.
_BASE_TEMPERATURE_UNIT = "degree_C"

_MARINE_DISPLAY_LABEL = {
    "foot": "ft",
    "meter": "m",
    "second": "s",
    "knot": "kt",
    "nautical_mile": "nm",
    "meter_per_second": "m/s",
    "mile_per_hour": "mph",
    "km_per_hour": "km/h",
    "mile": "mi",
    "km": "km",
}


def _marine_target_units() -> dict[str, str]:
    """Return group -> weewx-internal target unit for the 5 marine groups.

    Reads the operator's configured per-group unit from api.conf [units][[groups]]
    via get_group_unit().  Falls back to preset defaults (knot for ocean speed,
    nautical_mile for visibility, foot/meter for wave height based on US vs METRIC)
    when the group is not explicitly configured.
    """
    height_default = "foot" if get_target_unit() == "US" else "meter"
    temperature_default = "degree_F" if get_target_unit() == "US" else "degree_C"
    return {
        "group_wave_height": get_group_unit("group_wave_height", height_default),
        "group_wave_period": get_group_unit("group_wave_period", "second"),
        "group_water_level": get_group_unit("group_water_level", height_default),
        "group_ocean_speed": get_group_unit("group_ocean_speed", "knot"),
        "group_visibility": get_group_unit("group_visibility", "nautical_mile"),
        "group_temperature": get_group_unit("group_temperature", temperature_default),
    }


def _marine_units_block(targets: dict[str, str]) -> dict[str, str]:
    return {
        "waveHeight": _MARINE_DISPLAY_LABEL[targets["group_wave_height"]],
        "wavePeriod": _MARINE_DISPLAY_LABEL[targets["group_wave_period"]],
        "waterLevel": _MARINE_DISPLAY_LABEL[targets["group_water_level"]],
        "windSpeed": _MARINE_DISPLAY_LABEL[targets["group_ocean_speed"]],
        "visibility": _MARINE_DISPLAY_LABEL[targets["group_visibility"]],
        # T1.2: waterTemp (and any other land group_temperature marine field)
        # display unit -- resolved via units/labels.py rather than
        # _MARINE_DISPLAY_LABEL since group_temperature units (degree_F/
        # degree_C) aren't in that marine-only lookup table.
        "temperature": _get_unit_label(targets["group_temperature"]),
    }


def _convert_observation(
    obs: MarineObservation, targets: dict[str, str]
) -> MarineObservation:
    """Convert the marine-group fields on a MarineObservation to display units.

    windDirection/meanWaveDirection (degrees) and temperature/pressure
    fields are passed through unchanged -- see module docstring.
    """
    updates: dict[str, object] = {
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
    if obs.spectralComponents:
        updates["spectralComponents"] = [
            comp.model_copy(
                update={
                    "height": _convert_unit(
                        comp.height, _BASE_WAVE_HEIGHT_UNIT, targets["group_wave_height"]
                    )
                }
            )
            for comp in obs.spectralComponents
        ]
    return obs.model_copy(update=updates)


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


_HARBOR_NAME_KEYWORDS: tuple[str, ...] = (
    "harbor",
    "harbour",
    "bay",
    "inlet",
    "marina",
    "channel",
    "lagoon",
)

# Marine location photo storage (T4.1-T4.3, Marine Complete Remediation
# Plan). Photos are always converted to WebP and saved at this deterministic
# on-disk path -- see the wizard/admin upload handlers (T4.1). Served by
def _is_harbor_location(location: MarineLocation) -> bool:
    """Best-effort detection of sheltered/harbor locations (T1.4, Marine
    Remediation Plan; API-MANUAL §"Detail endpoint enrichment contract" --
    "Null for harbor locations (no open-ocean fallback)").

    WaveWatch III and NDBC buoy Hs both model open-water conditions -- a
    harbor/bay/inlet/marina/channel/lagoon has calmer, sheltered water that
    neither source represents, so a raw open-ocean wave height there is
    misleading rather than merely approximate.

    Detected via a case-insensitive keyword match on the configured location
    name. A future `sheltered = true` config flag is deferred (lead ruling,
    Phase 1 F0 round) -- name-keyword detection is sufficient for now.
    """
    name_lower = location.name.lower()
    return any(keyword in name_lower for keyword in _HARBOR_NAME_KEYWORDS)


def _classify_alert_type(event: str) -> str:
    """Classify an NWS alert `event` string into a dashboard alertType bucket.

    Keyword match, case-insensitive, on the NWS canonical event name (e.g.
    "Small Craft Advisory", "Coastal Flood Warning"). Unrecognized marine
    event types default to "marineZone" (T3.5, Marine Remediation Plan --
    lead-specified default; every alert reaching this endpoint is already a
    marine/coastal NWS alert, so marineZone is the safest generic bucket).
    """
    event_lower = event.lower()
    if any(
        k in event_lower
        for k in ("small craft", "gale", "storm warning", "hurricane force", "special marine")
    ):
        return "marineZone"
    if "coastal flood" in event_lower:
        return "coastalFlood"
    if any(k in event_lower for k in ("beach hazard", "rip current", "high surf")):
        return "beachHazard"
    return "marineZone"  # default to marineZone for unrecognized marine alerts


def _fetch_active_alerts(location: MarineLocation) -> list[MarineAlertSummary] | None:
    """Best-effort fetch of active NWS alerts for a marine location, classified by type.

    Returns None on any provider failure (independent best-effort, matching
    the try/except pattern used by every other provider call in this
    module) rather than raising -- one location's alert outage must not
    fail the whole /marine list response.
    """
    try:
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        marine_zone_ids = [location.nws_marine_zone_id] if location.nws_marine_zone_id else None
        alerts = nws.fetch(
            lat=location.lat,
            lon=location.lon,
            user_agent_contact=None,
            marine_zone_ids=marine_zone_ids,
        )
        return [
            MarineAlertSummary(headline=alert.headline, alertType=_classify_alert_type(alert.event))
            for alert in alerts
        ]
    except Exception:
        logger.warning(
            "NWS alerts fetch failed for marine location %r", location.id, exc_info=True
        )
        return None


def _location_summary(location: MarineLocation) -> MarineLocationSummary:
    """Build a MarineLocationSummary with best-effort live data.

    Each sub-fetch (NDBC observation, weather cache, station archive) is
    independently wrapped in try/except so a single provider outage
    degrades that field to None without failing the whole summary.

    currentTide is always None on this card summary (ADR-091, T1.3): all
    locations sharing a CO-OPS station show identical tide predictions --
    visual noise on the landing page. The CO-OPS prediction fetch stays in
    the cache warmer (services/cache_warmer.py _warm_marine()) because it
    feeds the activity detail tabs via GET /tides/{locationId}.
    """
    now_str = utc_isoformat(datetime.now(tz=UTC))
    current_conditions: MarineObservation | None = None
    weather_code: int | None = None
    is_day: bool | None = None

    # --- currentConditions from NDBC buoy observation ---
    if location.ndbc_station_ids:
        try:
            from weewx_clearskies_api.providers.buoy import ndbc  # noqa: PLC0415

            result = ndbc.fetch(station_id=location.ndbc_station_ids[0])
            raw_obs = result.get("observation")
            if raw_obs is not None:
                current_conditions = raw_obs
        except Exception:
            logger.warning(
                "NDBC fetch failed for summary of location %r (station %s)",
                location.id,
                location.ndbc_station_ids[0],
                exc_info=True,
            )

    # --- waveHeight fallback chain (T2.1, API-MANUAL §16 "Card data source
    # contract", ADR-091/ADR-084): NWPS + wave_transform supplements (surf-
    # enabled locations only) -> WaveWatch III first forecast point (offshore,
    # no supplements) -> NDBC buoy Hs (already fetched above, raw canonical
    # meters). Each level is independently try/excepted; a wave-height source
    # outage falls through to the next level rather than failing the whole
    # summary. This replaces the previous behavior where every location's
    # card waveHeight came solely from the (shared) NDBC buoy, making all
    # locations sharing a buoy show identical values.
    wave_height_meters: float | None = None

    if "surf" in location.activities and location.nwps_wfo:
        try:
            from weewx_clearskies_api.providers.marine import nwps  # noqa: PLC0415

            spot_config = (
                _marine_config.surf_spots.get(location.id) if _marine_config else None
            )
            if spot_config is not None:
                nwps_result = nwps.fetch(
                    lat=location.lat, lon=location.lon, wfo_override=location.nwps_wfo
                )
                nearshore = nwps_result.get("nearshore") if nwps_result else None
                if nearshore and nearshore.get("waveHeight") is not None:
                    supplemented = wave_transform.apply_supplements(
                        {
                            "wave_height": nearshore.get("waveHeight"),
                            "wave_period": nearshore.get("wavePeriod"),
                            "wave_direction": nearshore.get("waveDirection"),
                        },
                        spot_config,
                        location.lat,
                        location.lon,
                    )
                    if supplemented is not None and supplemented.get("wave_height") is not None:
                        wave_height_meters = supplemented["wave_height"]
        except Exception:
            logger.warning(
                "NWPS wave height fetch/supplement failed for marine location %r",
                location.id,
                exc_info=True,
            )

    if wave_height_meters is None:
        try:
            from weewx_clearskies_api.providers.marine import wavewatch  # noqa: PLC0415

            ww_result = wavewatch.fetch(lat=location.lat, lon=location.lon)
            points = ww_result.get("forecast") or []
            if points and points[0].waveHeight is not None:
                wave_height_meters = points[0].waveHeight
        except Exception:
            logger.warning(
                "WaveWatch III wave height fetch failed for marine location %r",
                location.id,
                exc_info=True,
            )

    if wave_height_meters is None and current_conditions is not None:
        wave_height_meters = current_conditions.waveHeight

    # --- harbor/sheltered-water override (T1.4, Marine Remediation Plan) ---
    # Non-surf locations in this chain only ever reach here via WaveWatch III
    # or the raw NDBC buoy reading (the NWPS + wave_transform branch above is
    # gated on "surf" in location.activities) -- both are open-ocean sources.
    # Suppress the wave height for harbor-like locations rather than show a
    # misleading open-water number for sheltered water. Also null out
    # current_conditions.waveHeight directly -- otherwise the raw
    # (unconverted) NDBC buoy value already sitting on current_conditions
    # would survive unchanged, since the conversion block below (which is
    # the only other place that writes waveHeight onto current_conditions)
    # is skipped whenever wave_height_meters is None.
    if (
        wave_height_meters is not None
        and "surf" not in location.activities
        and _is_harbor_location(location)
    ):
        wave_height_meters = None
        if current_conditions is not None:
            current_conditions = current_conditions.model_copy(update={"waveHeight": None})

    if wave_height_meters is not None:
        target_wave_height_unit = get_group_unit(
            "group_wave_height", "foot" if get_target_unit() == "US" else "meter"
        )
        converted_wave_height = _convert_unit(
            wave_height_meters, _BASE_WAVE_HEIGHT_UNIT, target_wave_height_unit
        )
        if current_conditions is not None:
            current_conditions = current_conditions.model_copy(
                update={"waveHeight": converted_wave_height}
            )
        else:
            current_conditions = MarineObservation(
                stationId=location.id,
                time=now_str,
                waveHeight=converted_wave_height,
            )

    # --- waterTemp from ocean data resolver (T3.7, replaces raw NDBC) ---
    # The resolver runs the tiered fallback chain (OFS → regional ERDDAP →
    # MUR SST → RTOFS). If it returns a surface_temp, that replaces the
    # NDBC buoy waterTemp on the card. If it fails, the NDBC buoy value
    # (already in current_conditions from the fetch above) is kept as-is
    # and converted below.
    try:
        from weewx_clearskies_api.services.ocean_data_resolver import resolve as resolve_ocean  # noqa: PLC0415

        ocean = resolve_ocean(
            lat=location.lat,
            lon=location.lon,
            location_config={
                "ofs_model": getattr(location, "ofs_model", None),
                "ofs_fallback": getattr(location, "ofs_fallback", None),
                "ofs_region": getattr(location, "ofs_region", None),
            },
            needs="surface",
        )
        if ocean.surface_temp is not None and current_conditions is not None:
            current_conditions = current_conditions.model_copy(
                update={"waterTemp": ocean.surface_temp}
            )
    except Exception:
        logger.warning(
            "Ocean resolver failed for card waterTemp at %r", location.id, exc_info=True
        )

    # --- waterTemp unit conversion (T1.2 / T3.7) ---
    # waterTemp is now either from the ocean resolver (Celsius) or NDBC
    # buoy (also Celsius). Convert to the operator's display unit.
    if current_conditions is not None and current_conditions.waterTemp is not None:
        target_temp_unit = get_group_unit(
            "group_temperature",
            "degree_F" if get_target_unit() == "US" else "degree_C",
        )
        current_conditions = current_conditions.model_copy(
            update={
                "waterTemp": _convert_unit(
                    current_conditions.waterTemp, _BASE_TEMPERATURE_UNIT, target_temp_unit
                )
            }
        )

    # --- windSpeed / windDirection / airTemp precedence (T1.1) ---
    # Station hardware wins when the location is within dedup_radius_km of
    # the weewx station (marine_location_resolver.is_station_served());
    # otherwise falls back to the forecast-provider-fed marine weather cache
    # (populated by cache_warmer._warm_marine_weather()). Card data source
    # contract, API-MANUAL §16. Values pass through in whatever unit the
    # source already provides -- no marine-group (knot) conversion applied
    # here, per lead ruling (interim; T1.2 is the only conversion in scope
    # for this round).
    #
    # The station-served branch opens its own short-lived Session via
    # get_engine() rather than threading a FastAPI Depends()-injected Session
    # through list_marine_locations() -> _location_summary() (lead ruling):
    # this is a conditional path inside a helper, not a reason to change the
    # route's public signature, and services/cache_warmer.py already
    # establishes the "open a fresh Session(engine) outside the request-DI
    # path" pattern for exactly this kind of one-off archive read.
    wind_temp_updates: dict[str, float] = {}
    try:
        from weewx_clearskies_api.services.marine_location_resolver import (  # noqa: PLC0415
            is_station_served,
        )

        if is_station_served(location.id):
            from weewx_clearskies_api.db.registry import get_registry  # noqa: PLC0415
            from weewx_clearskies_api.services.archive import (  # noqa: PLC0415
                get_current as _get_current_observation,
            )

            registry = get_registry()
            with Session(get_engine()) as station_db:
                station_obs = _get_current_observation(station_db, registry)
            if station_obs is not None:
                if station_obs.windSpeed is not None:
                    wind_temp_updates["windSpeed"] = station_obs.windSpeed
                if station_obs.windDir is not None:
                    wind_temp_updates["windDirection"] = station_obs.windDir
                if station_obs.outTemp is not None:
                    wind_temp_updates["airTemp"] = station_obs.outTemp
        else:
            from weewx_clearskies_api.services.marine_weather_cache import (  # noqa: PLC0415
                get_marine_weather_cache as _get_weather_cache_for_wind,
            )

            weather_cache_for_wind = _get_weather_cache_for_wind()
            if weather_cache_for_wind is not None:
                provider_conditions = weather_cache_for_wind.get_current_conditions(
                    location.lat, location.lon
                )
                if provider_conditions is not None:
                    if provider_conditions.get("windSpeed") is not None:
                        wind_temp_updates["windSpeed"] = provider_conditions["windSpeed"]
                    if provider_conditions.get("windDirection") is not None:
                        wind_temp_updates["windDirection"] = provider_conditions["windDirection"]
                    if provider_conditions.get("airTemp") is not None:
                        wind_temp_updates["airTemp"] = provider_conditions["airTemp"]
    except Exception:
        logger.warning(
            "Station/provider wind+airTemp lookup failed for marine location %r",
            location.id,
            exc_info=True,
        )

    if wind_temp_updates:
        if current_conditions is not None:
            current_conditions = current_conditions.model_copy(update=wind_temp_updates)
        else:
            current_conditions = MarineObservation(
                stationId=location.id,
                time=now_str,
                **wind_temp_updates,
            )

    # --- weatherCode / isDay from marine weather cache ---
    try:
        from weewx_clearskies_api.services.marine_weather_cache import (  # noqa: PLC0415
            get_marine_weather_cache,
        )

        cache = get_marine_weather_cache()
        if cache is not None:
            conditions = cache.get_current_conditions(location.lat, location.lon)
            if conditions is not None:
                weather_code = conditions.get("weatherCode")
                is_day = conditions.get("isDay")
    except Exception:
        logger.warning(
            "Marine weather cache lookup failed for location %r",
            location.id,
            exc_info=True,
        )

    return MarineLocationSummary(
        locationId=location.id,
        name=location.name,
        coordinates={"lat": location.lat, "lon": location.lon},
        activities=list(location.activities),
        currentConditions=current_conditions,
        currentTide=None,
        activeAlerts=_fetch_active_alerts(location),
        surfRating=None,
        beachSafetyLevel=None,
        weatherCode=weather_code,
        isDay=is_day,
        photoUrl=None,
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

    # --- T2.1 enrichment (API-MANUAL §18 "Detail endpoint enrichment
    # contract"): the raw NDBC buoy observation alone is insufficient -- most
    # buoys do not report wind, air temp, pressure, visibility, or weather
    # conditions. Copies the enrichment dispatch pattern from
    # _location_summary() (implementation rule: do NOT refactor these two
    # endpoints into a shared function -- different response shapes and
    # different additional data).
    #
    # Note on unit conversion: `observation` above is already display-unit
    # converted (via _convert_observation() called once, immediately after
    # the NDBC fetch). We do NOT call _convert_observation() a second time
    # after layering enrichment on top, because that function assumes its
    # inputs are in the NDBC/NWPS/WaveWatch canonical base units (m/s,
    # nautical miles, meters) -- station-hardware and marine_weather_cache
    # values are not in those units (station hardware is in the weewx
    # archive's native unit; the cache is already in the operator's *land*
    # display unit system from the forecast provider). Re-running them
    # through _convert_observation() would silently mis-convert them. Each
    # enrichment source below is therefore converted with the correct,
    # source-specific unit (waveHeight, waterTemp) or passed through
    # unconverted, matching the same interim treatment _location_summary()
    # already established for windSpeed/windDirection/airTemp.

    # --- waveHeight fallback chain: NWPS + wave_transform (surf locations)
    # -> WaveWatch III (offshore) -> NDBC buoy Hs (already on `observation`,
    # used automatically as the last resort by doing nothing below). ---
    wave_height_meters: float | None = None

    if "surf" in location.activities and location.nwps_wfo:
        try:
            from weewx_clearskies_api.providers.marine import nwps  # noqa: PLC0415

            spot_config = (
                _marine_config.surf_spots.get(location.id) if _marine_config else None
            )
            if spot_config is not None:
                nwps_result = nwps.fetch(
                    lat=location.lat, lon=location.lon, wfo_override=location.nwps_wfo
                )
                nearshore = nwps_result.get("nearshore") if nwps_result else None
                if nearshore and nearshore.get("waveHeight") is not None:
                    supplemented = wave_transform.apply_supplements(
                        {
                            "wave_height": nearshore.get("waveHeight"),
                            "wave_period": nearshore.get("wavePeriod"),
                            "wave_direction": nearshore.get("waveDirection"),
                        },
                        spot_config,
                        location.lat,
                        location.lon,
                    )
                    if supplemented is not None and supplemented.get("wave_height") is not None:
                        wave_height_meters = supplemented["wave_height"]
        except Exception:
            logger.warning(
                "T2.1 enrichment: NWPS wave height fetch/supplement failed for "
                "marine location %r",
                location_id,
                exc_info=True,
            )

    if wave_height_meters is None:
        try:
            from weewx_clearskies_api.providers.marine import wavewatch  # noqa: PLC0415

            ww_result = wavewatch.fetch(lat=location.lat, lon=location.lon)
            points = ww_result.get("forecast") or []
            if points and points[0].waveHeight is not None:
                wave_height_meters = points[0].waveHeight
        except Exception:
            logger.warning(
                "T2.1 enrichment: WaveWatch III wave height fetch failed for "
                "marine location %r",
                location_id,
                exc_info=True,
            )

    # --- harbor/sheltered-water override (mirrors _location_summary() lines
    # 468-486). Suppress open-ocean wave height for harbor locations. ---
    if (
        wave_height_meters is not None
        and "surf" not in location.activities
        and _is_harbor_location(location)
    ):
        wave_height_meters = None
        if observation is not None:
            observation = observation.model_copy(update={"waveHeight": None})

    if wave_height_meters is not None:
        converted_wave_height = _convert_unit(
            wave_height_meters, _BASE_WAVE_HEIGHT_UNIT, targets["group_wave_height"]
        )
        if observation is not None:
            observation = observation.model_copy(update={"waveHeight": converted_wave_height})
        else:
            observation = MarineObservation(
                stationId=location_id, time=now_str, waveHeight=converted_wave_height
            )
    # else: leave observation.waveHeight as whatever the NDBC buoy fetch above
    # already produced (already display-unit converted) -- the documented
    # last-resort fallback.

    # --- waterTemp: ocean data resolver (OFS -> regional ERDDAP -> MUR SST ->
    # RTOFS), falls back to the NDBC buoy's raw waterTemp already on
    # `observation` (that raw value has NOT been temperature-converted yet --
    # _convert_observation() does not touch waterTemp -- so the conversion
    # below applies uniformly whether the value came from the resolver or the
    # buoy). ---
    try:
        from weewx_clearskies_api.services.ocean_data_resolver import resolve as resolve_ocean  # noqa: PLC0415

        ocean = resolve_ocean(
            lat=location.lat,
            lon=location.lon,
            location_config={
                "ofs_model": getattr(location, "ofs_model", None),
                "ofs_fallback": getattr(location, "ofs_fallback", None),
                "ofs_region": getattr(location, "ofs_region", None),
            },
            needs="surface",
        )
        if ocean.surface_temp is not None:
            if observation is not None:
                observation = observation.model_copy(update={"waterTemp": ocean.surface_temp})
            else:
                observation = MarineObservation(
                    stationId=location_id, time=now_str, waterTemp=ocean.surface_temp
                )
    except Exception:
        logger.warning(
            "T2.1 enrichment: ocean resolver failed for marine location %r waterTemp",
            location_id,
            exc_info=True,
        )

    if observation is not None and observation.waterTemp is not None:
        observation = observation.model_copy(
            update={
                "waterTemp": _convert_unit(
                    observation.waterTemp, _BASE_TEMPERATURE_UNIT, targets["group_temperature"]
                )
            }
        )

    # --- windSpeed / windDirection / windGust / airTemp / pressure: station
    # hardware (when is_station_served()) else marine_weather_cache. Passed
    # through in whatever unit the source provides -- see the module-level
    # note above on why a second _convert_observation() pass is unsafe here.
    # windGust/pressure have no marine_weather_cache fallback: the forecast
    # provider's ProviderConditions DTO (models/responses.py) does not carry
    # either field today, so those two are station-hardware-only until that
    # DTO is extended (out of scope for this task -- flagged separately). ---
    station_updates: dict[str, float] = {}
    try:
        from weewx_clearskies_api.services.marine_location_resolver import (  # noqa: PLC0415
            is_station_served,
        )

        if is_station_served(location_id):
            from weewx_clearskies_api.db.registry import get_registry  # noqa: PLC0415
            from weewx_clearskies_api.services.archive import (  # noqa: PLC0415
                get_current as _get_current_observation,
            )

            registry = get_registry()
            with Session(get_engine()) as station_db:
                station_obs = _get_current_observation(station_db, registry)
            if station_obs is not None:
                if station_obs.windSpeed is not None:
                    station_updates["windSpeed"] = station_obs.windSpeed
                if station_obs.windDir is not None:
                    station_updates["windDirection"] = station_obs.windDir
                if station_obs.windGust is not None:
                    station_updates["windGust"] = station_obs.windGust
                if station_obs.outTemp is not None:
                    station_updates["airTemp"] = station_obs.outTemp
                if station_obs.barometer is not None:
                    station_updates["pressure"] = station_obs.barometer
        else:
            from weewx_clearskies_api.services.marine_weather_cache import (  # noqa: PLC0415
                get_marine_weather_cache as _get_weather_cache_for_wind,
            )

            weather_cache_for_wind = _get_weather_cache_for_wind()
            if weather_cache_for_wind is not None:
                provider_conditions = weather_cache_for_wind.get_current_conditions(
                    location.lat, location.lon
                )
                if provider_conditions is not None:
                    if provider_conditions.get("windSpeed") is not None:
                        station_updates["windSpeed"] = provider_conditions["windSpeed"]
                    if provider_conditions.get("windDirection") is not None:
                        station_updates["windDirection"] = provider_conditions["windDirection"]
                    if provider_conditions.get("airTemp") is not None:
                        station_updates["airTemp"] = provider_conditions["airTemp"]
    except Exception:
        logger.warning(
            "T2.1 enrichment: station/provider wind+airTemp lookup failed for "
            "marine location %r",
            location_id,
            exc_info=True,
        )

    if station_updates:
        if observation is not None:
            observation = observation.model_copy(update=station_updates)
        else:
            observation = MarineObservation(stationId=location_id, time=now_str, **station_updates)

    # --- visibility / weatherCode / isDay: marine_weather_cache only, no
    # station fallback (API-MANUAL §18 table -- buoys/station hardware do not
    # report a WMO weatherCode or day/night flag; visibility here only
    # overrides when the cache actually has a value -- the NDBC buoy's own
    # visibility reading, if present, is otherwise left as-is). ---
    cache_updates: dict[str, object] = {}
    try:
        from weewx_clearskies_api.services.marine_weather_cache import (  # noqa: PLC0415
            get_marine_weather_cache,
        )

        cache = get_marine_weather_cache()
        if cache is not None:
            conditions = cache.get_current_conditions(location.lat, location.lon)
            if conditions is not None:
                if conditions.get("weatherCode") is not None:
                    cache_updates["weatherCode"] = conditions["weatherCode"]
                if conditions.get("isDay") is not None:
                    cache_updates["isDay"] = conditions["isDay"]
                if conditions.get("visibility") is not None:
                    cache_updates["visibility"] = conditions["visibility"]
    except Exception:
        logger.warning(
            "T2.1 enrichment: marine weather cache lookup failed for marine "
            "location %r",
            location_id,
            exc_info=True,
        )

    if cache_updates:
        if observation is not None:
            observation = observation.model_copy(update=cache_updates)
        else:
            observation = MarineObservation(stationId=location_id, time=now_str, **cache_updates)

    # --- Wave forecast: NWPS nearshore (T2.2, preferred when configured) ---
    # falls back to WaveWatch III offshore data only when NWPS is not
    # configured for this location or its fetch fails. Unlike the surf
    # endpoint, no wave_transform supplements are applied here -- this is the
    # general-purpose marine bundle, not a surf-spot-specific score, and not
    # every marine location carries a SurfSpotConfig to supplement against.
    forecast: list[MarineForecastPoint] = []
    nwps_succeeded = False
    if location.nwps_wfo:
        try:
            from weewx_clearskies_api.providers.marine import nwps  # noqa: PLC0415

            nwps_result = nwps.fetch(
                lat=location.lat, lon=location.lon, wfo_override=location.nwps_wfo
            )
            nearshore = nwps_result.get("nearshore") if nwps_result else None
            if nearshore and nearshore.get("waveHeight") is not None:
                point = MarineForecastPoint(
                    time=nwps_result.get("cycle_time") or now_str,
                    waveHeight=nearshore.get("waveHeight"),
                    wavePeriod=nearshore.get("wavePeriod"),
                    waveDirection=nearshore.get("waveDirection"),
                )
                forecast = [_convert_forecast_point(point, targets)]
                nwps_succeeded = True
        except Exception:
            logger.warning(
                "NWPS fetch failed for marine location %r", location_id, exc_info=True
            )

    if not nwps_succeeded:
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

    # --- source attribution (T2.2): reflect which wave-data path actually
    # supplied the forecast list, so dashboard/debugging can tell NWPS-backed
    # nearshore data from WaveWatch III offshore fallback data. ---
    wave_source = "nwps+ndbc+nws_marine" if nwps_succeeded else "ndbc+wavewatch+nws_marine"

    bundle = MarineBundle(
        locationId=location.id,
        locationName=location.name,
        coordinates={"lat": location.lat, "lon": location.lon},
        observation=observation,
        forecast=forecast,
        textForecast=text_forecast,
        source=wave_source,
        generatedAt=now_str,
    )

    return {
        "data": bundle.model_dump(by_alias=True, exclude_none=False),
        "units": _marine_units_block(targets),
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("marine").model_dump(by_alias=True),
        "generatedAt": now_str,
    }
