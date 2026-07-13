"""GET /beach-safety, GET /beach-safety/{location_id} — beach safety assessment
(API-MANUAL §18, T5.5).

Behavior:
  GET /beach-safety (no locationId): list configured beach-safety locations.
    404 when no location has "beach_safety" in its activities list.
  GET /beach-safety/{location_id}: full safety assessment bundle. 404 when
    the location doesn't exist or doesn't have "beach_safety" enabled.

Classification rules (task brief, not yet documented in API-MANUAL — the
manual only names the enum values in §16's BeachSafetyAssessment field
table, not the thresholds; thresholds below match the task brief verbatim):

  Sea state ("safetyLevel"):
    height <2ft AND period >8s              -> "safe"
    height 2-3ft OR period 6-8s             -> "caution"
    height >3ft OR period <6s               -> "dangerous"
    (dangerous checked first since "height >3" and "period <6" are the most
    severe single-condition triggers; caution is the residual middle band)

  Water temperature comfort ("comfortLevel"):
    >75°F  -> "comfortable"
    65-75°F -> "cool"
    55-65°F -> "cold"
    <55°F  -> "dangerous"

Data sources:
  - Nearshore wave height/period: NWPS (providers/marine/nwps.py) preferred,
    NDBC buoy (providers/buoy/ndbc.py) as fallback.
  - Rip current risk + UV index: NWS SRF (providers/marine/nws_srf.py).
  - Water temperature: NDBC, falling back to CO-OPS water_temperature.
  - Tides / water levels: CO-OPS (providers/tides/coops.py).
  - NWPS v1.5 show-when-available fields (rip current probability, total
    water level, wave runup): surfaced under `data.nwpsV15` when the
    covering WFO supplies them (~12 WFOs per PROVIDER-MANUAL §14.6); absent
    (key omitted's value is None) otherwise, without error.
  - Active alerts filtered per ADR-090 to beach-safety-relevant event types:
    Beach Hazards Statement, High Surf Advisory, High Surf Warning, Rip
    Current Statement, Coastal Flood Advisory, Coastal Flood Warning.
  - External links: BeachSafetyConfig.external_links (informational
    passthrough, e.g. state water-quality dashboards).

Each provider section is wrapped in its own try/except (graceful
degradation). Response is a plain dict following the standard envelope
(API-MANUAL §2), matching endpoints/almanac.py's get_solunar() pattern
since no BeachSafetyBundleResponse Pydantic model exists yet.

wire_beach_safety_config(settings) stores the parsed MarineConfig
module-level — see endpoints/surf.py's wire_surf_config() docstring for the
same caveat about *settings* being a MarineConfig instance, not the
top-level Settings object.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup — see module docstring)
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None

# ADR-090 beach-safety-relevant alert event types (substring, case-insensitive).
_BEACH_SAFETY_ALERT_EVENTS = (
    "beach hazards statement",
    "high surf advisory",
    "high surf warning",
    "rip current statement",
    "coastal flood advisory",
    "coastal flood warning",
)


def wire_beach_safety_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by the beach-safety endpoints.

    Same accepted shapes as endpoints/surf.py's wire_surf_config() (mirrors
    the wire_marine_config() contract established for T5.1's /marine
    endpoint): a bare MarineConfig, a settings-like object with a
    `.marine_config` attribute, or anything else -> stored as None. Never
    raises.
    """
    global _marine_config  # noqa: PLW0603
    if isinstance(settings, MarineConfig):
        _marine_config = settings
    elif isinstance(getattr(settings, "marine_config", None), MarineConfig):
        _marine_config = settings.marine_config  # type: ignore[attr-defined]
    else:
        _marine_config = None


# ---------------------------------------------------------------------------
# Location lookup helpers
# ---------------------------------------------------------------------------


def _beach_safety_locations() -> list[MarineLocation]:
    if _marine_config is None:
        return []
    return [loc for loc in _marine_config.locations if "beach_safety" in loc.activities]


def _find_location(location_id: str) -> MarineLocation | None:
    if _marine_config is None:
        return None
    for loc in _marine_config.locations:
        if loc.id == location_id:
            return loc
    return None


# ---------------------------------------------------------------------------
# Marine unit resolution (API-MANUAL §16 "Marine unit groups")
# ---------------------------------------------------------------------------

_DISPLAY_LABEL = {
    "foot": "ft",
    "meter": "m",
    "second": "s",
    "knot": "kt",
    "meter_per_second": "m/s",
    "mile_per_hour": "mph",
    "km_per_hour": "km/h",
    "nautical_mile": "nm",
    "mile": "mi",
    "km": "km",
}


def _wave_height_unit() -> tuple[str, str]:
    height_default = "foot" if get_target_unit() == "US" else "meter"
    unit = get_group_unit("group_wave_height", height_default)
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _ocean_speed_unit() -> tuple[str, str]:
    unit = get_group_unit("group_ocean_speed", "knot")
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _wave_period_unit() -> tuple[str, str]:
    unit = get_group_unit("group_wave_period", "second")
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _visibility_unit() -> tuple[str, str]:
    unit = get_group_unit("group_visibility", "nautical_mile")
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _units_block() -> dict[str, str]:
    _, height_symbol = _wave_height_unit()
    _, speed_symbol = _ocean_speed_unit()
    _, period_symbol = _wave_period_unit()
    _, vis_symbol = _visibility_unit()
    return {
        "waveHeight": height_symbol,
        "wavePeriod": period_symbol,
        "windSpeed": speed_symbol,
        "visibility": vis_symbol,
    }


# ---------------------------------------------------------------------------
# Classification rules (task brief — see module docstring)
# ---------------------------------------------------------------------------

_SAFE_MAX_HEIGHT_FT = 2.0
_SAFE_MIN_PERIOD_S = 8.0
_CAUTION_MAX_HEIGHT_FT = 3.0
_CAUTION_MIN_PERIOD_S = 6.0
_CAUTION_MAX_PERIOD_S = 8.0

_COMFORT_COMFORTABLE_MIN_F = 75.0
_COMFORT_COOL_MIN_F = 65.0
_COMFORT_COLD_MIN_F = 55.0


def classify_sea_state(height_ft: float | None, period_s: float | None) -> str | None:
    """Classify nearshore wave conditions into "safe" / "caution" / "dangerous".

    Returns None when either input is unavailable (unknown, not unsafe).
    Dangerous is checked first — it's the most severe single-condition
    trigger and must win over an ambiguous "also matches caution" case
    (e.g. height=4ft, period=7s matches both "height>3" (dangerous) and
    "period 6-8s" (caution); dangerous wins).
    """
    if height_ft is None or period_s is None:
        return None
    if height_ft > _CAUTION_MAX_HEIGHT_FT or period_s < _CAUTION_MIN_PERIOD_S:
        return "dangerous"
    if (
        _SAFE_MAX_HEIGHT_FT <= height_ft <= _CAUTION_MAX_HEIGHT_FT
        or _CAUTION_MIN_PERIOD_S <= period_s <= _CAUTION_MAX_PERIOD_S
    ):
        return "caution"
    if height_ft < _SAFE_MAX_HEIGHT_FT and period_s > _SAFE_MIN_PERIOD_S:
        return "safe"
    return "caution"  # residual gap (shouldn't occur given the ranges above)


def classify_water_comfort(water_temp_f: float | None) -> str | None:
    """Classify water temperature comfort. Returns None when unavailable."""
    if water_temp_f is None:
        return None
    if water_temp_f > _COMFORT_COMFORTABLE_MIN_F:
        return "comfortable"
    if water_temp_f >= _COMFORT_COOL_MIN_F:
        return "cool"
    if water_temp_f >= _COMFORT_COLD_MIN_F:
        return "cold"
    return "dangerous"


# ---------------------------------------------------------------------------
# Alert filter (ADR-090)
# ---------------------------------------------------------------------------


def _filter_beach_safety_alerts(alerts: list) -> list:
    filtered = []
    for alert in alerts:
        event = (getattr(alert, "event", "") or "").strip().lower()
        if any(keyword in event for keyword in _BEACH_SAFETY_ALERT_EVENTS):
            filtered.append(alert)
    return filtered


# ---------------------------------------------------------------------------
# Best-effort data gather (shared by list + detail views)
# ---------------------------------------------------------------------------


def _gather_wave_and_temp(location: MarineLocation) -> dict:
    """Best-effort fetch of wave height/period, water temp, rip risk, UV.

    Every provider call is independently wrapped — a single provider
    failure degrades that field to None rather than failing the whole
    gather. Used by both the list view (lightweight cards) and the detail
    view (full bundle).
    """
    result: dict = {
        "wave_height_m": None,
        "wave_period_s": None,
        "water_temp_c": None,
        "rip_current_risk": None,
        "uv_index": None,
        "nwps_v15": None,
        "wind_speed": None,
        "wind_direction": None,
        "visibility": None,
    }

    try:
        from weewx_clearskies_api.providers.marine import nwps  # noqa: PLC0415

        nwps_result = nwps.fetch(lat=location.lat, lon=location.lon, wfo_override=location.nwps_wfo)
        nearshore = nwps_result.get("nearshore") if nwps_result else None
        if nearshore:
            result["wave_height_m"] = nearshore.get("waveHeight")
            result["wave_period_s"] = nearshore.get("wavePeriod")
            v15_fields = {
                "ripCurrentProbability": nearshore.get("ripCurrentProbability"),
                "totalWaterLevel": nearshore.get("totalWaterLevel"),
                "waveRunup": nearshore.get("waveRunup"),
            }
            if any(v is not None for v in v15_fields.values()):
                result["nwps_v15"] = v15_fields
    except Exception:
        logger.warning(
            "beach-safety endpoint: NWPS fetch failed for %s", location.id, exc_info=True
        )

    try:
        if location.ndbc_station_ids:
            from weewx_clearskies_api.providers.buoy import ndbc  # noqa: PLC0415

            ndbc_result = ndbc.fetch(
                station_id=location.ndbc_station_ids[0], include_spectral=False
            )
            obs = ndbc_result.get("observation")
            if obs is not None:
                if result["wave_height_m"] is None:
                    result["wave_height_m"] = obs.waveHeight
                if result["wave_period_s"] is None:
                    result["wave_period_s"] = obs.dominantPeriod
                result["water_temp_c"] = obs.waterTemp
                result["wind_speed"] = obs.windSpeed
                result["wind_direction"] = obs.windDirection
                result["visibility"] = obs.visibility
    except Exception:
        logger.warning(
            "beach-safety endpoint: NDBC fetch failed for %s", location.id, exc_info=True
        )

    try:
        from weewx_clearskies_api.providers.marine import nws_srf  # noqa: PLC0415

        srf_result = nws_srf.fetch(
            lat=location.lat,
            lon=location.lon,
            county_zone=getattr(location, "nws_srf_zone_id", None),
            wfo_override=getattr(location, "nws_srf_wfo", None),
        )
        srf_forecasts = srf_result.get("forecasts") or []
        if srf_forecasts:
            today_forecast = srf_forecasts[0]
            result["rip_current_risk"] = today_forecast.ripCurrentRisk
            result["uv_index"] = today_forecast.uvIndex
            if result["water_temp_c"] is None:
                result["water_temp_c"] = today_forecast.waterTemp
    except Exception:
        logger.warning(
            "beach-safety endpoint: NWS SRF fetch failed for %s", location.id, exc_info=True
        )

    if result["water_temp_c"] is None and location.coops_station_ids:
        try:
            from weewx_clearskies_api.providers.tides import coops  # noqa: PLC0415

            coops_result = coops.fetch(
                station_id=location.coops_station_ids[0], products=("water_temperature",)
            )
            result["water_temp_c"] = coops_result.get("water_temperature")
        except Exception:
            logger.warning(
                "beach-safety endpoint: CO-OPS water temp fetch failed for %s",
                location.id,
                exc_info=True,
            )

    return result


# ---------------------------------------------------------------------------
# GET /beach-safety — list beach-safety locations
# ---------------------------------------------------------------------------


@router.get("/beach-safety", summary="Configured beach-safety locations", tags=["Marine"])
def list_beach_safety_locations() -> dict:
    locations = _beach_safety_locations()
    if not locations:
        raise HTTPException(status_code=404, detail="No beach-safety locations configured")

    cards = []
    for loc in locations:
        gathered = _gather_wave_and_temp(loc)
        height_ft = _convert_unit(gathered["wave_height_m"], "meter", "foot")
        safety_level = classify_sea_state(height_ft, gathered["wave_period_s"])
        cards.append(
            {
                "locationId": loc.id,
                "name": loc.name,
                "lat": loc.lat,
                "lon": loc.lon,
                "safetyLevel": safety_level,
                "ripCurrentRisk": gathered["rip_current_risk"],
                "waterTemp": gathered["water_temp_c"],
            }
        )

    now_str = utc_isoformat(datetime.now(tz=UTC))
    return {
        "data": cards,
        "units": _units_block(),
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("beach_safety").model_dump(by_alias=True),
        "generatedAt": now_str,
    }


# ---------------------------------------------------------------------------
# GET /beach-safety/{location_id} — full safety assessment bundle
# ---------------------------------------------------------------------------


@router.get(
    "/beach-safety/{location_id}", summary="Beach safety assessment for a location", tags=["Marine"]
)
def get_beach_safety(location_id: str) -> dict:
    location = _find_location(location_id)
    if location is None or "beach_safety" not in location.activities:
        raise HTTPException(
            status_code=404, detail=f"Beach-safety location {location_id!r} not found"
        )

    beach_safety_config = _marine_config.beach_safety.get(location_id) if _marine_config else None

    wave_height_internal, _ = _wave_height_unit()

    gathered = _gather_wave_and_temp(location)
    height_ft = _convert_unit(gathered["wave_height_m"], "meter", "foot")
    safety_level = classify_sea_state(height_ft, gathered["wave_period_s"])
    water_temp_f = (
        _convert_unit(gathered["water_temp_c"], "degree_C", "degree_F")
        if gathered["water_temp_c"] is not None
        else None
    )
    comfort_level = classify_water_comfort(water_temp_f)

    # --- CO-OPS tides / water levels ---
    tide_predictions: list[dict] = []
    water_levels: list[dict] = []
    try:
        if location.coops_station_ids:
            from weewx_clearskies_api.providers.tides import coops  # noqa: PLC0415

            coops_result = coops.fetch(
                station_id=location.coops_station_ids[0],
                products=("predictions", "water_level"),
            )
            tide_predictions = [p.model_dump() for p in coops_result.get("predictions", [])]
            water_levels = [w.model_dump() for w in coops_result.get("water_levels", [])]
    except Exception:
        logger.warning(
            "beach-safety endpoint: CO-OPS tides fetch failed for %s", location_id, exc_info=True
        )

    # --- Active alerts, filtered per ADR-090 ---
    active_alert_headlines: list[str] = []
    try:
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        marine_zone_ids = [location.nws_marine_zone_id] if location.nws_marine_zone_id else None
        alerts = nws.fetch(
            lat=location.lat,
            lon=location.lon,
            user_agent_contact=None,
            marine_zone_ids=marine_zone_ids,
        )
        active_alert_headlines = [a.headline for a in _filter_beach_safety_alerts(alerts)]
    except Exception:
        logger.warning(
            "beach-safety endpoint: alerts fetch failed for %s", location_id, exc_info=True
        )

    # --- External links (informational passthrough) ---
    external_links = (
        [{"label": link.label, "url": link.url} for link in beach_safety_config.external_links]
        if beach_safety_config is not None
        else []
    )

    assessment = {
        "safetyLevel": safety_level,
        "waveHeight": _convert_unit(gathered["wave_height_m"], "meter", wave_height_internal),
        "wavePeriod": gathered["wave_period_s"],
        "ripCurrentRisk": gathered["rip_current_risk"],
        "waterTemp": gathered["water_temp_c"],
        "comfortLevel": comfort_level,
        "uvIndex": gathered["uv_index"],
        "visibility": _convert_unit(gathered["visibility"], "nautical_mile", _visibility_unit()[0])
        if gathered["visibility"] is not None
        else None,
        "windSpeed": _convert_unit(gathered["wind_speed"], "meter_per_second", _ocean_speed_unit()[0]),
        "windDirection": gathered["wind_direction"],
        "activeAlerts": active_alert_headlines,
    }

    now_str = utc_isoformat(datetime.now(tz=UTC))
    bundle = {
        "locationId": location.id,
        "locationName": location.name,
        "coordinates": {"lat": location.lat, "lon": location.lon},
        "assessment": assessment,
        "nwpsV15": gathered["nwps_v15"],
        "tidePredictions": tide_predictions,
        "waterLevels": water_levels,
        "externalLinks": external_links,
        "source": "nwps+ndbc+nws_srf+coops+nws_alerts",
        "generatedAt": now_str,
    }

    return {
        "data": bundle,
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("beach_safety").model_dump(by_alias=True),
        "units": _units_block(),
        "generatedAt": now_str,
    }
