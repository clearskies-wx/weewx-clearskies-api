"""GET /almanac/seeing-forecast — 72-hour astronomical seeing forecast (7Timer).

Behavior:
  1. Seeing provider not configured → 404 problem+json
  2. Cache hit (warmer:seeing-forecast) → deserialize and return immediately
  3. Cache miss → live fetch from 7Timer, construct and return response

Provider:
  7Timer ASTRO product (keyless, no credentials required).
  Returns 72 h of 3-hour-interval seeing, transparency, cloud cover, and
  atmospheric stability data.  Empty list on any provider error — seeing
  forecast degrades gracefully per seven_timer.py error handling strategy.

Cache key: warmer:seeing-forecast (static — no date suffix; 7Timer init_time
  is embedded in the response data, not the cache key).

No query parameters — this is a simple GET with no filtering options.
Pydantic + Depends pattern (security-baseline §3.5) is preserved via the
router structure, but no param model is needed for a parameterless endpoint.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from weewx_clearskies_api.models.responses import (
    SeeingForecastData,
    SeeingForecastPointResponse,
    SeeingForecastResponse,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.services.station import get_station_info

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level settings wiring (populated at startup by wire_seeing_settings).
# wire_seeing_settings() is called from __main__.py after settings load.
# Tests that don't need provider config leave these at the module defaults.
# ---------------------------------------------------------------------------

_seeing_provider: str | None = None
_seeing_base_url: str = "http://www.7timer.info/bin/api.pl"
_seeing_timeout_seconds: int = 10


def wire_seeing_settings(settings: object) -> None:
    """Store seeing settings for use by the endpoint.

    Extracts provider, base_url, and timeout_seconds from settings.seeing.
    Called from __main__.py after settings load.
    Tests that don't care about provider config leave module defaults as-is.
    """
    global _seeing_provider, _seeing_base_url, _seeing_timeout_seconds  # noqa: PLW0603
    seeing_section = getattr(settings, "seeing", None)
    if seeing_section is not None:
        _seeing_provider = getattr(seeing_section, "provider", None)
        raw_base_url = getattr(seeing_section, "base_url", "http://www.7timer.info/bin/api.pl")
        _seeing_base_url = raw_base_url if raw_base_url else "http://www.7timer.info/bin/api.pl"
        _seeing_timeout_seconds = int(getattr(seeing_section, "timeout_seconds", 10))


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/almanac/seeing-forecast",
    summary="Astronomical seeing forecast (72-hour, 3-hour intervals from 7Timer)",
    tags=["Almanac"],
    response_model=SeeingForecastResponse,
)
def get_seeing_forecast() -> SeeingForecastResponse:
    """Return the 72-hour astronomical seeing forecast from 7Timer.

    Checks the warmer cache first (key: warmer:seeing-forecast).  Falls back
    to a live 7Timer fetch on cache miss.  Returns HTTP 404 when no seeing
    provider is configured.
    """
    # Decision branch 1: no provider configured
    if _seeing_provider is None:
        raise HTTPException(
            status_code=404,
            detail="Seeing forecast provider not configured",
        )

    # Station coordinates for live fallback fetch
    station_info = get_station_info()
    lat = station_info.latitude
    lon = station_info.longitude

    # Decision branch 2: cache hit
    try:
        cached = get_cache().get("warmer:seeing-forecast")
        if cached is not None:
            logger.debug("seeing-forecast cache hit")
            # cached is a dict with "init_time" (ISO str) and "points" (list of dicts)
            # from the cache warmer's SeeingForecastPoint.model_dump(mode="json") calls.
            init_time_str = cached.get("init_time", utc_isoformat(datetime.now(tz=UTC)))
            raw_points = cached.get("points", [])
            points = [
                SeeingForecastPointResponse(
                    validTime=p["valid_time"],
                    seeingIndex=p["seeing_index"],
                    transparencyIndex=p["transparency_index"],
                    cloudCoverOctet=p["cloud_cover_octet"],
                    liftedIndex=p["lifted_index"],
                    windSpeedClass=p["wind_speed_class"],
                    windDirection=p["wind_direction"],
                    temp2mC=p["temp_2m_c"],
                    humidityClass=p["humidity_class"],
                    precType=p["prec_type"],
                )
                for p in raw_points
            ]
            return SeeingForecastResponse(
                data=SeeingForecastData(
                    initTime=init_time_str,
                    points=points,
                ),
                generatedAt=utc_isoformat(datetime.now(tz=UTC)),
            )
    except Exception:
        logger.debug("seeing-forecast cache miss or deserialization error", exc_info=True)

    # Decision branch 3: cache miss — live fetch
    from weewx_clearskies_api.providers.seeing.seven_timer import SevenTimerProvider

    with SevenTimerProvider(
        base_url=_seeing_base_url,
        timeout_seconds=_seeing_timeout_seconds,
    ) as provider:
        raw_points = provider.fetch_forecast(lat, lon)

    points = [
        SeeingForecastPointResponse(
            validTime=p.valid_time,
            seeingIndex=p.seeing_index,
            transparencyIndex=p.transparency_index,
            cloudCoverOctet=p.cloud_cover_octet,
            liftedIndex=p.lifted_index,
            windSpeedClass=p.wind_speed_class,
            windDirection=p.wind_direction,
            temp2mC=p.temp_2m_c,
            humidityClass=p.humidity_class,
            precType=p.prec_type,
        )
        for p in raw_points
    ]

    # Approximate init_time from the earliest valid_time minus 3 h (first timepoint is +3 h).
    # This is a best-effort reconstruction; the provider strips init_time from SeeingForecastPoint.
    # The first point's valid_time - 3 h gives the 7Timer model initialization time.
    if points:
        first_valid = points[0].validTime
        init_dt = first_valid - timedelta(hours=3)
        init_time_str = utc_isoformat(init_dt)
    else:
        init_time_str = utc_isoformat(datetime.now(tz=UTC))

    return SeeingForecastResponse(
        data=SeeingForecastData(
            initTime=init_time_str,
            points=points,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
