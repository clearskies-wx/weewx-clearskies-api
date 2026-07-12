"""GET /fishing, GET /fishing/{location_id} — fishing conditions forecast (API-MANUAL §18, T5.4).

Behavior:
  GET /fishing (no locationId): list configured fishing locations. 404 when
    no location has "fishing" in its activities list.
  GET /fishing/{location_id}: 3-day fishing forecast bundle. 404 when the
    location doesn't exist or doesn't have "fishing" enabled.

Data flow per API-MANUAL §17/§18:
  1. enrichment/solunar.py's compute_solunar() for each of the next 3
     station-local days.
  2. 5-6 periods per day (dawn/morning/midday/afternoon/evening/night) built
     from that day's sunrise/sunset (services/almanac.py compute_almanac,
     Skyfield-backed) — see _build_periods().
  3. enrichment/fishing_scorer.py's score_fishing() per period, fed with:
       - pressure_trend_hpa_3hr: NDBC buoy pressureTendency (PTDY field is
         already a 3-hour delta — no separate trend computation needed).
         Station-barometer fallback (mentioned in the task brief as an
         alternate source) is deliberately deferred: it would require
         pulling enrichment/barometer_trend.py's DB-derived inHg/hr trend
         and re-deriving a 3-hour hPa delta from it, a materially different
         computation from the NDBC passthrough. When NDBC is unavailable,
         pressure_trend_hpa_3hr is None (neutral score) per graceful
         degradation.
       - tide_state: derived from CO-OPS tide predictions via
         _tide_state_at() (incoming/outgoing/slack_high/slack_low/peak_flow).
       - water_temp_f: NDBC waterTemp, falling back to CO-OPS
         water_temperature.
       - solunar_intensity / is_during_major_period / is_during_minor_period:
         from the day's SolunarTimes.
       - species / target_category: from FishingSpotConfig.
       - bathymetric_profile: reused from the location's SurfSpotConfig (if
         the location also has "surf" configured) since FishingSpotConfig
         itself carries no bathymetric_profile field (config/marine_config.py)
         — bathymetry is captured once per location via CUDEM, not
         per-activity. None (-> no habitat features) when the location has
         no surf spot config.

Each provider section is wrapped in its own try/except (graceful
degradation). Response is a plain dict following the standard envelope
(API-MANUAL §2), matching endpoints/almanac.py's get_solunar() pattern
since no FishingBundleResponse Pydantic model exists yet.

wire_fishing_config(settings) stores the parsed MarineConfig module-level —
see endpoints/surf.py's wire_surf_config() docstring for the same caveat
about *settings* being a MarineConfig instance, not the top-level Settings
object.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.enrichment.fishing_scorer import get_habitat_features, score_fishing
from weewx_clearskies_api.enrichment.solunar import compute_solunar
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services import almanac as almanac_svc
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock, get_station_info
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup — see module docstring)
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None

_FORECAST_DAYS = 3


def wire_fishing_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by the fishing endpoints.

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


def _fishing_locations() -> list[MarineLocation]:
    if _marine_config is None:
        return []
    return [loc for loc in _marine_config.locations if "fishing" in loc.activities]


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


def _units_block() -> dict[str, str]:
    _, height_symbol = _wave_height_unit()
    _, speed_symbol = _ocean_speed_unit()
    _, period_symbol = _wave_period_unit()
    return {
        "swellHeight": height_symbol,
        "swellPeriod": period_symbol,
        "windSpeed": speed_symbol,
    }


# ---------------------------------------------------------------------------
# Period generation from sunrise/sunset (module docstring point 2)
# ---------------------------------------------------------------------------


def _parse_iso_z(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_periods(sunrise: datetime, sunset: datetime) -> list[tuple[str, datetime, datetime]]:
    """Build 6 period windows (dawn/morning/midday/afternoon/evening/night).

    Daylight (sunrise..sunset) is split into three equal thirds for
    morning/midday/afternoon; dawn and evening are 2h windows straddling
    sunrise/sunset; night is a representative 6h window starting 1h after
    sunset. This is a deliberate approximation — a fully accurate "night"
    window would need the *next* day's sunrise (to avoid an arbitrary
    cutoff), which is out of scope for a per-day period builder. The
    representative window is only used to pick each period's midpoint hour
    for score_fishing()'s own (independently accurate) sunrise/sunset-based
    time-of-day scoring — see fishing_scorer.py's _time_bucket().
    """
    daylight = sunset - sunrise
    third = daylight / 3
    return [
        ("dawn", sunrise - timedelta(hours=1), sunrise + timedelta(hours=1)),
        ("morning", sunrise + timedelta(hours=1), sunrise + third),
        ("midday", sunrise + third, sunrise + 2 * third),
        ("afternoon", sunrise + 2 * third, sunset - timedelta(hours=1)),
        ("evening", sunset - timedelta(hours=1), sunset + timedelta(hours=1)),
        ("night", sunset + timedelta(hours=1), sunset + timedelta(hours=7)),
    ]


# ---------------------------------------------------------------------------
# Tide-state derivation from CO-OPS predictions
# ---------------------------------------------------------------------------

_SLACK_WINDOW = timedelta(minutes=30)
_PEAK_FLOW_WINDOW = timedelta(minutes=30)


def _tide_state_at(predictions: list[dict], now: datetime) -> str:
    """Classify the tidal state at *now* from a list of TidePrediction dicts.

    Returns one of "incoming", "outgoing", "slack_high", "slack_low",
    "peak_flow" — matching fishing_scorer.py's _TIDE_SCORES keys. Falls back
    to "incoming" (a recognized, neutral-ish key) when predictions are
    unavailable or malformed — score_fishing()'s _score_tide() only applies
    the true "unrecognized key" fallback for keys outside that set.
    """
    parsed: list[tuple[datetime, float, str | None]] = []
    for p in predictions:
        t = _parse_iso_z(p.get("time"))
        if t is None:
            continue
        parsed.append((t, p.get("height", 0.0), p.get("type")))
    parsed.sort(key=lambda x: x[0])
    if not parsed:
        return "incoming"

    extremes = [(t, h, typ) for t, h, typ in parsed if typ in ("high", "low")]
    if len(extremes) < 2:
        return "incoming"

    before: tuple[datetime, float, str | None] | None = None
    after: tuple[datetime, float, str | None] | None = None
    for t, h, typ in extremes:
        if t <= now:
            before = (t, h, typ)
        elif after is None:
            after = (t, h, typ)
            break

    if before is not None and abs(now - before[0]) <= _SLACK_WINDOW:
        return "slack_high" if before[2] == "high" else "slack_low"
    if after is not None and abs(after[0] - now) <= _SLACK_WINDOW:
        return "slack_high" if after[2] == "high" else "slack_low"

    if before is None or after is None:
        return "incoming"

    span = (after[0] - before[0]).total_seconds()
    if span > 0:
        midpoint_time = before[0] + timedelta(seconds=span / 2)
        if abs(now - midpoint_time) <= _PEAK_FLOW_WINDOW:
            return "peak_flow"

    return "incoming" if before[2] == "low" else "outgoing"


# ---------------------------------------------------------------------------
# GET /fishing — list fishing locations
# ---------------------------------------------------------------------------


@router.get("/fishing", summary="Configured fishing locations", tags=["Marine"])
def list_fishing_locations() -> dict:
    locations = _fishing_locations()
    if not locations:
        raise HTTPException(status_code=404, detail="No fishing locations configured")

    cards = [
        {
            "locationId": loc.id,
            "name": loc.name,
            "lat": loc.lat,
            "lon": loc.lon,
        }
        for loc in locations
    ]

    now_str = utc_isoformat(datetime.now(tz=UTC))
    return {
        "data": cards,
        "units": _units_block(),
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("fishing").model_dump(by_alias=True),
        "generatedAt": now_str,
    }


# ---------------------------------------------------------------------------
# GET /fishing/{location_id} — 3-day fishing forecast bundle
# ---------------------------------------------------------------------------


@router.get("/fishing/{location_id}", summary="Fishing forecast for a location", tags=["Marine"])
def get_fishing(location_id: str) -> dict:
    location = _find_location(location_id)
    if location is None or "fishing" not in location.activities:
        raise HTTPException(status_code=404, detail=f"Fishing location {location_id!r} not found")

    fishing_config = _marine_config.fishing_spots.get(location_id) if _marine_config else None
    if fishing_config is None:
        raise HTTPException(
            status_code=404,
            detail=f"No fishing spot configuration for location {location_id!r}",
        )

    wave_height_internal, _ = _wave_height_unit()

    station_info = get_station_info()
    station_tz = station_info.timezone

    # --- NDBC: pressure trend, water temp, wind (best-effort, cached fetch) ---
    pressure_trend_hpa_3hr: float | None = None
    water_temp_c: float | None = None
    wind_speed: float | None = None
    wind_direction: float | None = None
    wind_gust: float | None = None
    try:
        if location.ndbc_station_ids:
            from weewx_clearskies_api.providers.buoy import ndbc  # noqa: PLC0415

            ndbc_result = ndbc.fetch(
                station_id=location.ndbc_station_ids[0], include_spectral=False
            )
            obs = ndbc_result.get("observation")
            if obs is not None:
                pressure_trend_hpa_3hr = obs.pressureTendency
                water_temp_c = obs.waterTemp
                wind_speed = obs.windSpeed
                wind_direction = obs.windDirection
                wind_gust = obs.windGust
    except Exception:
        logger.warning("fishing endpoint: NDBC fetch failed for %s", location_id, exc_info=True)

    # --- CO-OPS: tide predictions + water temp fallback ---
    tide_predictions: list[dict] = []
    try:
        if location.coops_station_ids:
            from weewx_clearskies_api.providers.tides import coops  # noqa: PLC0415

            coops_result = coops.fetch(
                station_id=location.coops_station_ids[0],
                products=("predictions", "water_temperature"),
            )
            tide_predictions = [p.model_dump() for p in coops_result.get("predictions", [])]
            if water_temp_c is None:
                water_temp_c = coops_result.get("water_temperature")
    except Exception:
        logger.warning("fishing endpoint: CO-OPS fetch failed for %s", location_id, exc_info=True)

    water_temp_f = (
        _convert_unit(water_temp_c, "degree_C", "degree_F") if water_temp_c is not None else None
    )

    # --- Bathymetric profile: reused from the location's surf spot config
    # (see module docstring) ---
    bathymetric_profile: list[dict] | None = None
    surf_config = _marine_config.surf_spots.get(location_id) if _marine_config else None
    if surf_config is not None and surf_config.bathymetric_profile:
        bathymetric_profile = [
            {"distance_m": pt.distance_m, "depth_m": pt.depth_m}
            for pt in surf_config.bathymetric_profile
        ]
    habitat_features = get_habitat_features(bathymetric_profile)

    species = fishing_config.species
    target_category = fishing_config.target_category

    today = datetime.now(tz=UTC).date()

    days: list[dict] = []
    for day_offset in range(_FORECAST_DAYS):
        target_date: date = today + timedelta(days=day_offset)

        # --- Solunar for this day ---
        try:
            solunar = compute_solunar(target_date, location.lat, location.lon, station_tz)
        except Exception:
            logger.warning(
                "fishing endpoint: solunar computation failed for %s day=%s",
                location_id,
                target_date,
                exc_info=True,
            )
            continue

        major_windows = [
            (_parse_iso_z(p["start"]), _parse_iso_z(p["end"])) for p in solunar.majorPeriods
        ]
        minor_windows = [
            (_parse_iso_z(p["start"]), _parse_iso_z(p["end"])) for p in solunar.minorPeriods
        ]

        # --- Sunrise/sunset for period building (Skyfield, services/almanac.py) ---
        try:
            almanac_day = almanac_svc.compute_almanac(
                target_date,
                location.lat,
                location.lon,
                station_info.altitude,
                station_tz=station_tz,
            )
            sunrise = _parse_iso_z(almanac_day.sun.rise)
            sunset = _parse_iso_z(almanac_day.sun.set)
        except Exception:
            logger.warning(
                "fishing endpoint: almanac computation failed for %s day=%s",
                location_id,
                target_date,
                exc_info=True,
            )
            sunrise = sunset = None

        periods: list[dict] = []
        if sunrise is not None and sunset is not None and sunset > sunrise:
            for _label, start, end in _build_periods(sunrise, sunset):
                midpoint = start + (end - start) / 2
                hour_utc = midpoint.hour
                tide_state = _tide_state_at(tide_predictions, midpoint)
                is_major = any(
                    ws is not None and we is not None and ws <= midpoint <= we
                    for ws, we in major_windows
                )
                is_minor = any(
                    ws is not None and we is not None and ws <= midpoint <= we
                    for ws, we in minor_windows
                )

                forecast = score_fishing(
                    pressure_hpa=None,
                    pressure_trend_hpa_3hr=pressure_trend_hpa_3hr,
                    tide_state=tide_state,
                    water_temp_f=water_temp_f,
                    hour_utc=hour_utc,
                    sunrise_utc=utc_isoformat(sunrise),
                    sunset_utc=utc_isoformat(sunset),
                    solunar_intensity=solunar.intensity,
                    is_during_major_period=is_major,
                    is_during_minor_period=is_minor,
                    species=species,
                    target_category=target_category,
                    month=target_date.month,
                    bathymetric_profile=bathymetric_profile,
                    wind_speed=wind_speed,
                    wind_direction=wind_direction,
                    wind_gust=wind_gust,
                    period_start_utc=utc_isoformat(start),
                    period_end_utc=utc_isoformat(end),
                )
                entry = forecast.model_dump()
                if entry.get("swellHeight") is not None:
                    entry["swellHeight"] = _convert_unit(
                        entry["swellHeight"], "meter", wave_height_internal
                    )
                periods.append(entry)

        days.append(
            {
                "date": target_date.isoformat(),
                "periods": periods,
                "solunar": solunar.model_dump(),
            }
        )

    now_str = utc_isoformat(datetime.now(tz=UTC))
    bundle = {
        "locationId": location.id,
        "locationName": location.name,
        "coordinates": {"lat": location.lat, "lon": location.lon},
        "days": days,
        "species": species,
        "targetCategory": target_category,
        "habitatFeatures": habitat_features,
        "tidePredictions": tide_predictions,
        "source": "ndbc+coops+solunar",
        "generatedAt": now_str,
    }

    return {
        "data": bundle,
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("fishing").model_dump(by_alias=True),
        "units": _units_block(),
        "generatedAt": now_str,
    }
