"""Almanac endpoints (3a-2).

GET /almanac              — sun + moon snapshot for one date
GET /almanac/sun-times    — year-long sunrise/sunset/daylight series
GET /almanac/moon-phases  — per-day moon-phase grid (month or full year)

All three are pure-compute: no DB hit, no provider dependency.
Params validated via Depends(_get_*_params) pattern per security-baseline §3.5.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from weewx_clearskies_api.models.params import (
    AlmanacQueryParams,
    MoonPhasesQueryParams,
    SunTimesQueryParams,
    PlanetsQueryParams,
    EclipsesQueryParams,
    MeteorShowersQueryParams,
    MoonNamesQueryParams,
)
from weewx_clearskies_api.models.responses import (
    AlmanacResponse,
    AlmanacSnapshot,
    EclipseContactPoint,
    EclipseResponse,
    LunarEclipseEntry,
    LunarEclipseList,
    MeteorShowerEntry,
    MeteorShowerList,
    MeteorShowerResponse,
    MoonNamesCalendar,
    MoonNamesResponse,
    MoonPhaseCalendar,
    MoonPhaseDay,
    MoonPhaseResponse,
    MoonPosition,
    MoonSnapshot,
    PlanetEntry,
    PlanetResponse,
    PlanetVisibility,
    PositionsResponse,
    PositionsSnapshot,
    SolarEclipseEntry,
    SolarEclipseList,
    SolarEclipseResponse,
    SpecialMoonEntry,
    SunPosition,
    SunSnapshot,
    SunTimesDay,
    SunTimesResponse,
    SunTimesSeries,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.services import almanac as almanac_svc
from weewx_clearskies_api.services.station import get_station_info

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Param wrapper functions (Pydantic + Depends pattern per security-baseline §3.5)
# ---------------------------------------------------------------------------


def _get_almanac_params(request: Request) -> AlmanacQueryParams:
    """Validate GET /almanac query parameters.  Rejects unknown keys."""
    try:
        return AlmanacQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _get_sun_times_params(request: Request) -> SunTimesQueryParams:
    """Validate GET /almanac/sun-times query parameters."""
    try:
        return SunTimesQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _get_moon_phases_params(request: Request) -> MoonPhasesQueryParams:
    """Validate GET /almanac/moon-phases query parameters."""
    try:
        return MoonPhasesQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _get_planets_params(request: Request) -> PlanetsQueryParams:
    """Validate GET /almanac/planets query parameters."""
    try:
        return PlanetsQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _get_eclipses_params(request: Request) -> EclipsesQueryParams:
    """Validate GET /almanac/eclipses query parameters."""
    try:
        return EclipsesQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _get_meteor_showers_params(request: Request) -> MeteorShowersQueryParams:
    """Validate GET /almanac/meteor-showers query parameters."""
    try:
        return MeteorShowersQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _get_moon_names_params(request: Request) -> MoonNamesQueryParams:
    """Validate GET /almanac/moon-names query parameters."""
    try:
        return MoonNamesQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _station_location() -> tuple[float, float, float]:
    """Return (lat, lon, altitude) from the cached station metadata.

    Altitude is in whatever unit weewx configured (feet or metres). Skyfield's
    wgs84.latlon elevation_m parameter expects metres.  We pass the value
    through as-is per ADR-019 (no server-side conversion).  The altitude is
    used only for the observer's horizon calculation — a few hundred feet vs
    metres makes negligible difference for rise/set times.
    """
    info = get_station_info()
    return info.latitude, info.longitude, info.altitude


def _current_year_in_station_tz() -> int:
    """Return the current calendar year in station-local time."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    info = get_station_info()
    try:
        zi = ZoneInfo(info.timezone)
        now = datetime.now(tz=zi)
    except ZoneInfoNotFoundError:
        now = datetime.now(tz=UTC)
    return now.year


def _today_in_station_tz() -> date:
    """Return today's date in station-local time."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    info = get_station_info()
    try:
        zi = ZoneInfo(info.timezone)
        now = datetime.now(tz=zi)
    except ZoneInfoNotFoundError:
        now = datetime.now(tz=UTC)
    return now.date()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/almanac", summary="Sun and moon snapshot", tags=["Almanac"])
def get_almanac(
    params: Annotated[AlmanacQueryParams, Depends(_get_almanac_params)],
) -> AlmanacResponse:
    """Sun and moon snapshot for a given date (Skyfield-computed, no DB hit)."""
    target_date = params.date if params.date is not None else _today_in_station_tz()
    lat, lon, alt = _station_location()
    station_tz = get_station_info().timezone

    # Cache-check-first guard (ADR-045).
    try:
        cached = get_cache().get(f"warmer:almanac:snapshot:{target_date.isoformat()}")
        if cached is not None:
            logger.debug("almanac snapshot cache hit: %s", target_date)
            sun = SunSnapshot(
                rise=cached["sun"]["rise"],
                set=cached["sun"]["set"],
                transit=cached["sun"]["transit"],
                civilTwilightDawn=cached["sun"]["civil_twilight_dawn"],
                civilTwilightDusk=cached["sun"]["civil_twilight_dusk"],
                azimuth=cached["sun"]["azimuth"],
                altitude=cached["sun"]["altitude"],
                rightAscension=cached["sun"]["right_ascension"],
                declination=cached["sun"]["declination"],
                daylightMinutes=cached["sun"]["daylight_minutes"],
                daylightDeltaVsYesterdayMinutes=cached["sun"]["daylight_delta_vs_yesterday_minutes"],
                nextEquinox=cached["sun"]["next_equinox"],
                nextSolstice=cached["sun"]["next_solstice"],
            )
            moon = MoonSnapshot(
                rise=cached["moon"]["rise"],
                set=cached["moon"]["set"],
                transit=cached["moon"]["transit"],
                azimuth=cached["moon"]["azimuth"],
                altitude=cached["moon"]["altitude"],
                rightAscension=cached["moon"]["right_ascension"],
                declination=cached["moon"]["declination"],
                phaseName=cached["moon"]["phase_name"],
                illuminationPercent=cached["moon"]["illumination_percent"],
                nextFullMoon=cached["moon"]["next_full_moon"],
                nextNewMoon=cached["moon"]["next_new_moon"],
            )
            return AlmanacResponse(
                data=AlmanacSnapshot(date=cached["date_str"], sun=sun, moon=moon),
                generatedAt=utc_isoformat(datetime.now(tz=UTC)),
            )
    except Exception:
        logger.debug("almanac snapshot cache miss or error: %s", target_date, exc_info=True)

    day = almanac_svc.compute_almanac(target_date, lat, lon, alt, station_tz=station_tz)

    sun = SunSnapshot(
        rise=day.sun.rise,
        set=day.sun.set,
        transit=day.sun.transit,
        civilTwilightDawn=day.sun.civil_twilight_dawn,
        civilTwilightDusk=day.sun.civil_twilight_dusk,
        azimuth=day.sun.azimuth,
        altitude=day.sun.altitude,
        rightAscension=day.sun.right_ascension,
        declination=day.sun.declination,
        daylightMinutes=day.sun.daylight_minutes,
        daylightDeltaVsYesterdayMinutes=day.sun.daylight_delta_vs_yesterday_minutes,
        nextEquinox=day.sun.next_equinox,
        nextSolstice=day.sun.next_solstice,
    )
    moon = MoonSnapshot(
        rise=day.moon.rise,
        set=day.moon.set,
        transit=day.moon.transit,
        azimuth=day.moon.azimuth,
        altitude=day.moon.altitude,
        rightAscension=day.moon.right_ascension,
        declination=day.moon.declination,
        phaseName=day.moon.phase_name,
        illuminationPercent=day.moon.illumination_percent,
        nextFullMoon=day.moon.next_full_moon,
        nextNewMoon=day.moon.next_new_moon,
    )

    return AlmanacResponse(
        data=AlmanacSnapshot(date=day.date_str, sun=sun, moon=moon),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/almanac/sun-times", summary="Year-long sunrise/sunset series", tags=["Almanac"])
def get_sun_times(
    params: Annotated[SunTimesQueryParams, Depends(_get_sun_times_params)],
) -> SunTimesResponse:
    """Year-long sunrise / sunset / daylight series (no DB hit)."""
    year = params.year if params.year is not None else _current_year_in_station_tz()
    lat, lon, alt = _station_location()
    station_tz = get_station_info().timezone

    # Cache-check-first guard (ADR-045).  The warmer pre-computes the current
    # year for the station location; use it when the request matches.
    try:
        cached = get_cache().get(f"warmer:almanac:sun-times:{year}")
        if cached is not None:
            logger.debug("sun-times cache hit: year=%d", year)
            days = [
                SunTimesDay(
                    date=d["date_str"],
                    sunrise=d["sunrise"],
                    sunset=d["sunset"],
                    daylightMinutes=d["daylight_minutes"],
                )
                for d in cached
            ]
            return SunTimesResponse(
                data=SunTimesSeries(year=year, days=days),
                generatedAt=utc_isoformat(datetime.now(tz=UTC)),
            )
    except Exception:
        logger.debug("sun-times cache miss or error: year=%d", year, exc_info=True)

    days_raw = almanac_svc.compute_sun_times_year(year, lat, lon, alt, station_tz=station_tz)
    days = [
        SunTimesDay(
            date=d.date_str,
            sunrise=d.sunrise,
            sunset=d.sunset,
            daylightMinutes=d.daylight_minutes,
        )
        for d in days_raw
    ]

    return SunTimesResponse(
        data=SunTimesSeries(year=year, days=days),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/almanac/moon-phases", summary="Moon-phase calendar", tags=["Almanac"])
def get_moon_phases(
    params: Annotated[MoonPhasesQueryParams, Depends(_get_moon_phases_params)],
) -> MoonPhaseResponse:
    """Per-day moon-phase calendar for a month or full year (no DB hit)."""
    year = params.year if params.year is not None else _current_year_in_station_tz()
    month = params.month  # None = full year
    lat, lon, _alt = _station_location()
    station_tz = get_station_info().timezone

    # Cache-check-first guard (ADR-045).  The warmer pre-computes the full year
    # (month=None) only; per-month requests bypass the cache.
    if month is None:
        try:
            cached = get_cache().get(f"warmer:almanac:moon-phases:{year}")
            if cached is not None:
                logger.debug("moon-phases cache hit: year=%d", year)
                days = [
                    MoonPhaseDay(
                        date=d["date_str"],
                        phaseName=d["phase_name"],
                        illuminationPercent=d["illumination_percent"],
                    )
                    for d in cached
                ]
                return MoonPhaseResponse(
                    data=MoonPhaseCalendar(year=year, month=month, days=days),
                    generatedAt=utc_isoformat(datetime.now(tz=UTC)),
                )
        except Exception:
            logger.debug("moon-phases cache miss or error: year=%d", year, exc_info=True)

    days_raw = almanac_svc.compute_moon_phases(year, lat, lon, month, station_tz=station_tz)
    days = [
        MoonPhaseDay(
            date=d.date_str,
            phaseName=d.phase_name,
            illuminationPercent=d.illumination_percent,
        )
        for d in days_raw
    ]

    return MoonPhaseResponse(
        data=MoonPhaseCalendar(year=year, month=month, days=days),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/almanac/moon-names", summary="Special full moon names", tags=["Almanac"])
def get_moon_names(
    params: Annotated[MoonNamesQueryParams, Depends(_get_moon_names_params)],
) -> MoonNamesResponse:
    """Full moons for a year with traditional and special name annotations.

    Returns one entry per full moon: traditional name (Wolf, Snow, etc.),
    Harvest Moon, Blue Moon, Hunter's Moon, and Supermoon flags.
    """
    year = params.year if params.year is not None else _current_year_in_station_tz()

    # Cache-check-first guard (ADR-045).
    try:
        cached = get_cache().get(f"warmer:almanac:moon-names:{year}")
        if cached is not None:
            logger.debug("moon-names cache hit: year=%d", year)
            moons = [
                SpecialMoonEntry(
                    date=m["date"],
                    traditionalName=m["traditionalName"],
                    isHarvestMoon=m["isHarvestMoon"],
                    isBlueMoon=m["isBlueMoon"],
                    isHuntersMoon=m["isHuntersMoon"],
                    isSupermoon=m["isSupermoon"],
                )
                for m in cached
            ]
            return MoonNamesResponse(
                data=MoonNamesCalendar(year=year, moons=moons),
                generatedAt=utc_isoformat(datetime.now(tz=UTC)),
            )
    except Exception:
        logger.debug("moon-names cache miss or error: year=%d", year, exc_info=True)

    moons_raw = almanac_svc.compute_special_moon_names(year)
    moons = [
        SpecialMoonEntry(
            date=m["date"],
            traditionalName=m["traditionalName"],
            isHarvestMoon=m["isHarvestMoon"],
            isBlueMoon=m["isBlueMoon"],
            isHuntersMoon=m["isHuntersMoon"],
            isSupermoon=m["isSupermoon"],
        )
        for m in moons_raw
    ]

    return MoonNamesResponse(
        data=MoonNamesCalendar(year=year, moons=moons),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/almanac/planets", summary="Planet visibility", tags=["Almanac"])
def get_planets(
    params: Annotated[PlanetsQueryParams, Depends(_get_planets_params)],
) -> PlanetResponse:
    """Evening/morning/all-night planet visibility for a given date.

    Returns Mercury through Saturn classified by visibility period.
    Only planets with apparent magnitude < 6.0 are included.
    Each planet entry includes altitude (degrees) and compass direction.
    """
    target_date = params.date if params.date is not None else _today_in_station_tz()
    lat, lon, alt = _station_location()
    station_tz = get_station_info().timezone

    # Cache-check-first guard (ADR-045).  The warmer pre-computes today's date
    # at the station location; use the cached result when the request matches.
    try:
        cache_key = f"warmer:almanac:planets:{target_date.isoformat()}"
        cached = get_cache().get(cache_key)
        if cached is not None:
            logger.debug("planets cache hit: date=%s", target_date.isoformat())
            visibility_raw = cached

            def _to_entries_cached(raw_list: list[dict]) -> list[PlanetEntry]:
                return [
                    PlanetEntry(
                        name=p["name"],
                        altitude=p["altitude"],
                        direction=p["direction"],
                        rise=p["rise"],
                        set=p["set"],
                        constellation=p["constellation"],
                        magnitude=p.get("magnitude"),
                        transitTime=p.get("transitTime"),
                        rightAscension=p.get("rightAscension"),
                        declination=p.get("declination"),
                        elongation=p.get("elongation"),
                    )
                    for p in raw_list
                ]

            return PlanetResponse(
                data=PlanetVisibility(
                    evening=_to_entries_cached(visibility_raw["evening"]),
                    morning=_to_entries_cached(visibility_raw["morning"]),
                    allNight=_to_entries_cached(visibility_raw["allNight"]),
                ),
                generatedAt=utc_isoformat(datetime.now(tz=UTC)),
            )
    except Exception:
        logger.debug("planets cache miss or error: date=%s", target_date.isoformat(), exc_info=True)

    visibility_raw = almanac_svc.compute_planets(
        target_date, lat, lon, alt, station_tz=station_tz
    )

    def _to_entries(raw_list: list[dict]) -> list[PlanetEntry]:
        return [
            PlanetEntry(
                name=p["name"],
                altitude=p["altitude"],
                direction=p["direction"],
                rise=p["rise"],
                set=p["set"],
                constellation=p["constellation"],
                magnitude=p.get("magnitude"),
                transitTime=p.get("transitTime"),
                rightAscension=p.get("rightAscension"),
                declination=p.get("declination"),
                elongation=p.get("elongation"),
            )
            for p in raw_list
        ]

    planet_response = PlanetResponse(
        data=PlanetVisibility(
            evening=_to_entries(visibility_raw["evening"]),
            morning=_to_entries(visibility_raw["morning"]),
            allNight=_to_entries(visibility_raw["allNight"]),
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )

    from weewx_clearskies_api.sse.endpoint_enrichment import apply_enrichments  # noqa: PLC0415

    response_dict = planet_response.model_dump(by_alias=True, exclude_none=True)
    response_dict = apply_enrichments("almanac/planets", response_dict)
    return response_dict


@router.get("/almanac/eclipses", summary="Lunar eclipses", tags=["Almanac"])
@router.get(
    "/almanac/eclipses/lunar",
    summary="Lunar eclipses",
    tags=["Almanac"],
    include_in_schema=False,
)
def get_eclipses(
    params: Annotated[EclipsesQueryParams, Depends(_get_eclipses_params)],
) -> EclipseResponse:
    """Lunar eclipse dates and types for a rolling 1-year window.

    Default: today through today + 365 days (future eclipses only).
    Optional ?from=YYYY-MM-DD and ?to=YYYY-MM-DD override the window.
    Uses skyfield.eclipselib; returns an empty list if unavailable.
    Types: penumbral, partial, total.

    Enriched with AstronomyAPI.com contact times and local visibility
    when WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_ID and
    WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_SECRET are configured.
    /almanac/eclipses/lunar is an alias (backward-compat preserved).
    """
    from datetime import timedelta

    today = _today_in_station_tz()
    from_date = params.from_ if params.from_ is not None else today
    # 10-year window — dashboard does progressive fill (2yr then backfill).
    to_date = params.to if params.to is not None else (today + timedelta(days=3652))

    # Cache-check-first guard (ADR-045).  The warmer pre-computes the default
    # rolling window; use the cached result when no override params were given.
    use_cache = params.from_ is None and params.to is None
    if use_cache:
        try:
            cache_key = f"warmer:almanac:eclipses:{today.isoformat()}"
            cached = get_cache().get(cache_key)
            if cached is not None:
                logger.debug("eclipses cache hit: from=%s", today.isoformat())
                eclipses = []
                for e in cached:
                    # Deserialise contactTimes from cached dicts back into
                    # EclipseContactPoint objects.  The warmer stores plain dicts
                    # (JSON-safe); reconstruct the Pydantic model instances here.
                    raw_ct = e.get("contactTimes")
                    contact_times = None
                    if isinstance(raw_ct, dict):
                        contact_times = {
                            k: EclipseContactPoint(date=v["date"], altitude=v["altitude"])
                            if isinstance(v, dict) and "date" in v and "altitude" in v
                            else None
                            for k, v in raw_ct.items()
                        }
                    eclipses.append(LunarEclipseEntry(
                        date=e["date"],
                        type=e["type"],
                        contactTimes=contact_times,
                        obscuration=e.get("obscuration"),
                        visibility=e.get("visibility"),
                    ))
                return EclipseResponse(
                    data=LunarEclipseList(
                        from_date=from_date.isoformat(),
                        to_date=to_date.isoformat(),
                        eclipses=eclipses,
                    ),
                    generatedAt=utc_isoformat(datetime.now(tz=UTC)),
                )
        except Exception:
            logger.debug("eclipses cache miss or error: from=%s", today.isoformat(), exc_info=True)

    eclipses_raw = almanac_svc.compute_lunar_eclipses(from_date=from_date, to_date=to_date)

    # Try AstronomyAPI.com enrichment — contact times + local visibility.
    contact_map: dict[str, dict] = {}
    try:
        import os
        app_id = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_ID", "").strip()
        app_secret = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_SECRET", "").strip()
        if app_id and app_secret:
            from weewx_clearskies_api.services.astronomyapi_client import AstronomyApiClient
            lat, lon, alt = _station_location()
            with AstronomyApiClient(
                app_id,
                app_secret,
            ) as client:
                api_eclipses = client.get_lunar_eclipses(lat, lon, alt, from_date, to_date)
            for ae in api_eclipses:
                contact_map[ae["date"]] = ae
                # AstronomyAPI peak date is local TZ; Skyfield is UTC.
                # Index ±1 day to handle timezone-induced date offset.
                from datetime import timedelta as _td
                try:
                    d = date.fromisoformat(ae["date"])
                    contact_map[(d - _td(days=1)).isoformat()] = ae
                    contact_map[(d + _td(days=1)).isoformat()] = ae
                except (ValueError, KeyError):
                    pass
    except Exception:
        logger.warning("AstronomyAPI.com lunar eclipse enrichment failed", exc_info=True)

    eclipses: list[LunarEclipseEntry] = []
    for e in eclipses_raw:
        api_data = contact_map.get(e["date"])
        contact_times_raw = None
        obscuration = None
        visibility = None
        if api_data:
            raw_ct = api_data.get("contactTimes")
            if isinstance(raw_ct, dict):
                contact_times_raw = {
                    k: EclipseContactPoint(date=v["date"], altitude=v["altitude"])
                    if isinstance(v, dict) and "date" in v and "altitude" in v
                    else None
                    for k, v in raw_ct.items()
                }
            obscuration = api_data.get("obscuration")
            # Compute visibility per ADR-053.
            peak = (api_data.get("contactTimes") or {}).get("peak")
            peak_alt = peak["altitude"] if isinstance(peak, dict) else None
            if peak_alt is None or peak_alt <= 0:
                visibility = "Not Visible"
            elif peak_alt <= 5:
                visibility = "Barely Visible"
            elif peak_alt <= 15:
                visibility = "Low in Sky"
            else:
                all_above = all(
                    (ct or {}).get("altitude", -1) > 0
                    for ct in (api_data.get("contactTimes") or {}).values()
                    if ct is not None
                )
                visibility = "Visible All Night" if all_above else "Mostly Visible"
        eclipses.append(LunarEclipseEntry(
            date=e["date"],
            type=e["type"],
            contactTimes=contact_times_raw,
            obscuration=obscuration,
            visibility=visibility,
        ))

    return EclipseResponse(
        data=LunarEclipseList(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            eclipses=eclipses,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/almanac/eclipses/solar", summary="Solar eclipses", tags=["Almanac"])
def get_solar_eclipses(
    params: Annotated[EclipsesQueryParams, Depends(_get_eclipses_params)],
) -> SolarEclipseResponse:
    """Solar eclipse dates, types, contact times, and local visibility.

    Default: today through today + 365 days.
    Optional ?from=YYYY-MM-DD and ?to=YYYY-MM-DD override the window.
    Powered exclusively by AstronomyAPI.com (Skyfield cannot compute solar
    eclipses).  Returns an empty list when credentials are not configured.
    Types: total, annular, partial.
    """
    from datetime import timedelta

    today = _today_in_station_tz()
    from_date = params.from_ if params.from_ is not None else today
    # 10-year window — dashboard does progressive fill (2yr then backfill).
    to_date = params.to if params.to is not None else (today + timedelta(days=3652))

    solar_eclipses: list[SolarEclipseEntry] = []

    try:
        import os
        app_id = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_ID", "").strip()
        app_secret = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_SECRET", "").strip()
        if app_id and app_secret:
            from weewx_clearskies_api.services.astronomyapi_client import AstronomyApiClient
            lat, lon, alt = _station_location()
            with AstronomyApiClient(
                app_id,
                app_secret,
            ) as client:
                api_eclipses = client.get_solar_eclipses(lat, lon, alt, from_date, to_date)

            for ae in api_eclipses:
                # Normalise type: "total_solar_eclipse" → "total", etc.
                raw_type = ae.get("type", "")
                normalised_type = (
                    raw_type
                    .replace("_solar_eclipse", "")
                    .replace("_lunar_eclipse", "")
                )

                raw_ct = ae.get("contactTimes")
                contact_times_raw: dict[str, EclipseContactPoint | None] | None = None
                if isinstance(raw_ct, dict):
                    contact_times_raw = {
                        k: EclipseContactPoint(date=v["date"], altitude=v["altitude"])
                        if isinstance(v, dict) and "date" in v and "altitude" in v
                        else None
                        for k, v in raw_ct.items()
                    }

                obscuration = ae.get("obscuration")
                obs = obscuration or 0

                # Compute solar visibility per ADR-053.
                total_start = (raw_ct or {}).get("totalStart") if isinstance(raw_ct, dict) else None
                peak = (raw_ct or {}).get("peak") if isinstance(raw_ct, dict) else None
                peak_alt = peak["altitude"] if isinstance(peak, dict) else None

                # obs is 0-1 fractional from AstronomyAPI, convert to %
                obs_pct = obs * 100
                if peak_alt is None or peak_alt <= 0 or obs_pct == 0:
                    visibility = "Not Visible"
                elif total_start is not None:
                    visibility = "Fully Visible"
                elif obs_pct >= 75:
                    visibility = "Mostly Visible"
                elif obs_pct >= 10:
                    visibility = "Partially Visible"
                else:
                    visibility = "Barely Visible"

                solar_eclipses.append(SolarEclipseEntry(
                    date=ae["date"],
                    type=normalised_type,
                    contactTimes=contact_times_raw,
                    obscuration=obscuration,
                    visibility=visibility,
                ))
    except Exception:
        logger.warning("AstronomyAPI.com solar eclipse fetch failed", exc_info=True)

    return SolarEclipseResponse(
        data=SolarEclipseList(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            eclipses=solar_eclipses,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/almanac/meteor-showers", summary="Meteor shower moon data", tags=["Almanac"])
def get_meteor_showers(
    params: Annotated[MeteorShowersQueryParams, Depends(_get_meteor_showers_params)],
) -> MeteorShowerResponse:
    """Meteor shower moon data for a rolling 1-year window.

    Default: today through today + 365 days (upcoming showers only).
    Optional ?from=YYYY-MM-DD and ?to=YYYY-MM-DD override the window.
    Returns showers sorted by peak date (soonest first).
    Each shower includes moon illumination percentage and phase name.
    """
    from datetime import timedelta

    today = _today_in_station_tz()
    from_date = params.from_ if params.from_ is not None else today
    to_date = params.to if params.to is not None else (today + timedelta(days=365))

    lat, lon, alt = _station_location()
    station_tz = get_station_info().timezone

    # Cache-check-first guard (ADR-045).  The warmer pre-computes the default
    # rolling window; use the cached result when no override params were given.
    # min_radiant_alt bypasses the cache because the warmer stores the full
    # unfiltered list and the filter is applied in compute_meteor_showers().
    use_cache = params.from_ is None and params.to is None and params.min_radiant_alt is None
    if use_cache:
        try:
            cache_key = f"warmer:almanac:meteor-showers:{today.isoformat()}"
            cached = get_cache().get(cache_key)
            if cached is not None:
                logger.debug("meteor-showers cache hit: from=%s", today.isoformat())
                showers = [
                    MeteorShowerEntry(
                        name=s["name"],
                        peakDate=s["peakDate"],
                        zhr=s["zhr"],
                        radiantAltitudeDeg=s["radiantAltitudeDeg"],
                        moonIlluminationPercent=s["moonIlluminationPercent"],
                        moonPhase=s["moonPhase"],
                        parentBody=s["parentBody"],
                        activeStart=s.get("activeStart"),
                        activeEnd=s.get("activeEnd"),
                        description=s.get("description"),
                        viewingQuality=s.get("viewingQuality"),
                        velocityKms=s.get("velocityKms"),
                        image=s.get("image"),
                    )
                    for s in cached
                ]
                return MeteorShowerResponse(
                    data=MeteorShowerList(
                        from_date=from_date.isoformat(),
                        to_date=to_date.isoformat(),
                        showers=showers,
                    ),
                    generatedAt=utc_isoformat(datetime.now(tz=UTC)),
                )
        except Exception:
            logger.debug("meteor-showers cache miss or error: from=%s", today.isoformat(), exc_info=True)

    showers_raw = almanac_svc.compute_meteor_showers(
        lat, lon, alt, station_tz=station_tz, from_date=from_date, to_date=to_date,
        min_radiant_alt=params.min_radiant_alt,
    )
    showers = [
        MeteorShowerEntry(
            name=s["name"],
            peakDate=s["peakDate"],
            zhr=s["zhr"],
            radiantAltitudeDeg=s["radiantAltitudeDeg"],
            moonIlluminationPercent=s["moonIlluminationPercent"],
            moonPhase=s["moonPhase"],
            parentBody=s["parentBody"],
            activeStart=s.get("activeStart"),
            activeEnd=s.get("activeEnd"),
            description=s.get("description"),
            viewingQuality=s.get("viewingQuality"),
            velocityKms=s.get("velocityKms"),
            image=s.get("image"),
        )
        for s in showers_raw
    ]

    return MeteorShowerResponse(
        data=MeteorShowerList(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            showers=showers,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/almanac/positions", summary="Current sun and moon positions", tags=["Almanac"])
def get_positions() -> PositionsResponse:
    """Real-time sun and moon azimuth/altitude. No caching — computed at request time."""
    info = get_station_info()
    positions = almanac_svc.compute_current_positions(info.latitude, info.longitude, info.altitude)
    return PositionsResponse(data=PositionsSnapshot(
        sun=SunPosition(**positions["sun"]),
        moon=MoonPosition(**positions["moon"]),
    ))
