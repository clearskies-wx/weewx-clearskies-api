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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from weewx_clearskies_api.models.params import (
    AlmanacQueryParams,
    MoonPhasesQueryParams,
    SunTimesQueryParams,
)
from weewx_clearskies_api.models.responses import (
    AlmanacResponse,
    AlmanacSnapshot,
    MoonPhaseCalendar,
    MoonPhaseDay,
    MoonPhaseResponse,
    MoonSnapshot,
    SunSnapshot,
    SunTimesDay,
    SunTimesSeries,
    SunTimesResponse,
    utc_isoformat,
)
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
