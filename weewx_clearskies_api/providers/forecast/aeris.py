"""Xweather (Vaisala) — module id: aeris — forecast provider module (ADR-007, ADR-038).

Five responsibilities per ADR-038 §2:
  1. Outbound API calls — two per cache miss:
       GET /forecasts/{lat},{lon}?filter=1hr  → hourly periods
       GET /forecasts/{lat},{lon}?filter=daynight → paired day/night periods
  2. Response parsing — wire-shape Pydantic models for each response
  3. Translation to canonical ForecastBundle (HourlyForecastPoint + DailyForecastPoint)
  4. Capability declaration — CAPABILITY symbol consumed at startup
  5. Error handling — provider errors translated to canonical taxonomy

Aeris is the first keyed provider on this project (ADR-006):
  client_id + client_secret passed as query params on every request.
  Sourced from env vars WEEWX_CLEARSKIES_AERIS_CLIENT_ID +
  WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET at startup (ADR-027 §3).
  Naming deviation: provider-scoped (not domain-scoped) per brief Q1 user
  decision 2026-05-08. Operator pastes once; works for forecast AND future
  alerts/observation modules. Deviation from ADR-027 §3 literal schema
  documented here and in settings.py docstring; no ADR amendment.

Cache layer (ADR-017):
  Caches the post-normalization ForecastBundle, not raw JSON.
  Key: SHA-256 of (provider_id, endpoint="forecast_bundle", {lat4, lon4, target_unit}).
  TTL: 1800s (30 min per ADR-017 defaults table for forecast).
  Single cache entry covers BOTH upstream calls (hourly + daynight).
  Cache stores model_dump(mode="json"); reconstructed via model_validate().

Slice-after-cache pattern (ADR-017 §Cache key):
  Full bundle stored in cache; endpoint applies hours/days slice after lookup.
  One cache entry per (station, target_unit) — limit=240/14 captures all
  periods Aeris will return for typical entry-paid plans.

Time conversion (ADR-020):
  Aeris period timestamps include UTC offset ("2026-04-30T10:00:00-07:00").
  to_utc_iso8601_from_offset() from _common/datetime_utils.py normalises to
  UTC Z form. validDate extracted from dateTimeISO BEFORE conversion — the
  offset IS the station-local one Aeris applies via profile.tz lookup.

Per-unit handling (ADR-019):
  Aeris returns BOTH metric + imperial fields in same payload; no units= param.
  Module picks the right field names based on target_unit.
  US → *F / *MPH / *IN, METRIC → *C / *KPH / *MM, METRICWX → *C / *MPS / *MM.
  windSpeedMaxMPS / windGustMaxMPS: if absent from daynight payload but KPH
  variants present, post-convert at canonical-translation time (brief lead-call 13).

Aeris weather code pass-through (canonical-data-model §4.1.2, §4.1.3):
  weatherCode = weatherPrimaryCoded string passed through opaque.
  weatherText = weather string (e.g. "Partly Cloudy") passed through.
  precipType derived via _aeris_descriptor_to_precip_type() from the
  third colon-segment of weatherPrimaryCoded per brief lead-call 16.

ForecastDiscussion (brief Q2 runtime detection, user decision 2026-05-08):
  Module attempts runtime detection of paid-tier summary field at:
    response[0].summary  (response-level)
    response[0].periods[0].summary  (period-level)
  When present and non-empty: ForecastDiscussion(headline=weatherPrimary,
    body=<summary>, source="aeris", issuedAt=<UTC-converted dateTimeISO>).
  When absent/empty/whitespace-only: discussion=None.
  CAPABILITY.supplied_canonical_fields declares headline + body as max-surface;
  runtime population is conditional (user-accepted capability-vs-runtime drift).

Forecast model selection (ADR-063):
  Operator selects "standard" or "xcast" model via aeris_forecast_model in
  api.conf [forecast]. Default: "xcast". When xcast is selected, the hourly
  call uses /xcast/forecasts (ML-enhanced temp/wind). The daynight call always
  uses /forecasts because xcast doesn't support filter=daynight. Confidence
  limits (tempConfidenceLimit, windConfidenceLimit) pass through in extras
  when non-null.

Rate limiter (ADR-038 §3):
  RateLimiter("aeris-forecast", max_calls=5, window_seconds=1) as "be polite"
  guard. Per-call acquire before each of the two outbound calls per cache miss.

ruff: noqa: N815  (field names match Aeris camelCase: dateTimeISO, maxTempF, etc.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import (
    DailyForecastPoint,
    ForecastBundle,
    ForecastDiscussion,
    HourlyForecastPoint,
    ProviderConditions,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.datetime_utils import to_utc_iso8601_from_offset
from weewx_clearskies_api.providers._common.errors import (
    KeyInvalid,
    ProviderProtocolError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "aeris"
DOMAIN = "forecast"
AERIS_BASE_URL = "https://data.api.xweather.com"
AERIS_FORECASTS_PATH = "/forecasts"
AERIS_XCAST_FORECASTS_PATH = "/xcast/forecasts"
AERIS_OBSERVATIONS_PATH = "/observations"
AERIS_CONVECTIVE_PATH = "/convective/outlook"
DEFAULT_FORECAST_TTL_SECONDS = 1800   # 30 min per ADR-017
DEFAULT_CONDITIONS_TTL_SECONDS = 300  # 5 min per brief
HOURLY_LIMIT = 240                     # 10 days × 24h, well above 384h ForecastQueryParams cap
DAYNIGHT_LIMIT = 16                    # 8 days × 2 so day 7 always has its night pair

_API_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        # HourlyForecastPoint
        "validTime",
        "outTemp",
        "outHumidity",
        "windSpeed",
        "windDir",
        "windGust",
        "precipProbability",
        "precipAmount",
        "precipType",
        "cloudCover",
        "weatherCode",
        "weatherText",
        # DailyForecastPoint
        "validDate",
        "tempMax",
        "tempMin",
        "precipAmount",
        "precipProbabilityMax",
        "windSpeedMax",
        "windGustMax",
        "uvIndexMax",
        "weatherCode",
        "weatherText",
        "dewpointMax",
        "dewpointMin",
        "humidityMax",
        "humidityMin",
        "visibilityMax",
        "snowAmount",
        "thunderRisk",
        "tornadoRisk",
        "hailRisk",
        "windRisk",
        "sunrise",
        "sunset",
        # ForecastDiscussion — max-surface; populated only on paid-tier responses
        # where summary field is detected at runtime (Q2 user decision 2026-05-08).
        # Free-tier returns bundle.discussion=null.  Auditor note: this is a
        # capability-vs-runtime-fidelity trade-off accepted by the user at brief Q2.
        "headline",
        "body",
        "narrative",
    ),
    geographic_coverage="global",   # Trust Aeris's authoritative answer per lead-call 17
    auth_required=("client_id", "client_secret"),
    default_poll_interval_seconds=DEFAULT_FORECAST_TTL_SECONDS,
    operator_notes=(
        "Xweather (Vaisala) free-tier and entry-paid plans. "
        "Requires client_id + client_secret bound to a registered domain "
        "or bundle id (see docs/reference/api-docs/aeris.md §Authentication). "
        "Forecast discussion populated when paid-tier summary field is present; "
        "free-tier returns bundle.discussion=null. Coverage per Aeris's "
        "authoritative answer; warn_location responses return empty bundle. "
        "Operator can select forecast model: 'standard' (/forecasts) or "
        "'xcast' (/xcast/forecasts, ML-enhanced temp/wind, default). "
        "Config key: aeris_forecast_model in [forecast] section of api.conf."
    ),
    refresh_interval=1800,
    attribution=ProviderAttribution(
        attribution_required=True,
        display_name="Xweather (Vaisala)",
        attribution_text="powered by Vaisala Xweather",
        text_prefix="powered by",
        text_provider_name="Vaisala Xweather",
        url="https://www.xweather.com/",
        logo_required=True,
    ),
)

# ---------------------------------------------------------------------------
# Aeris weather descriptor → canonical precipType (brief lead-call 16)
# Third colon-segment of weatherPrimaryCoded (e.g. "::OVC" → descriptor="OVC").
# Unknown descriptors → None (log DEBUG once on first encounter).
# ---------------------------------------------------------------------------

_AERIS_DESCRIPTOR_TO_PRECIP_TYPE: dict[str, str] = {
    # rain family
    "R": "rain",
    "RW": "rain",
    "L": "rain",       # drizzle → rain
    # snow family
    "S": "snow",
    "SW": "snow",
    # freezing
    "ZR": "freezing-rain",
    "ZL": "freezing-rain",   # freezing drizzle
    # ice/sleet
    "IP": "sleet",
    # hail
    "A": "hail",
    # thunder accompanies rain in canonical framing (consistent with NWS tsra → rain, 3b-3)
    "T": "rain",
    # mixed precip — canonical has no mixed-precip enum; log DEBUG on encounter
    "RS": "rain",    # rain/snow mix
    "WM": "rain",    # wintry mix
    "SI": "rain",    # snow/sleet
}

# Track which unknown descriptors have been logged to avoid log spam.
_logged_unknown_descriptors: set[str] = set()
# Track which mixed-precip descriptors have been logged for future canonical amendment.
_logged_mixed_precip: set[str] = set()
_MIXED_PRECIP_DESCRIPTORS = frozenset({"RS", "WM", "SI"})

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Source: docs/reference/api-docs/aeris.md + brief §per-module spec
# extras="ignore" so Aeris additions don't break us; missing required fields
# raise ValidationError → translated to ProviderProtocolError.
# ---------------------------------------------------------------------------


class _AerisLoc(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lat: float
    long: float


class _AerisProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tz: str | None = None
    elevFT: float | None = None
    elevM: float | None = None


class _AerisHourlyPeriod(BaseModel):
    """One hourly period from /forecasts?filter=1hr."""

    model_config = ConfigDict(extra="ignore")

    timestamp: int
    dateTimeISO: str
    # Temperature — both unit systems present in payload; module picks per target_unit
    tempC: float | None = None
    tempF: float | None = None
    # Humidity
    humidity: float | None = None
    # Wind speed — both unit systems
    windSpeedKPH: float | None = None
    windSpeedMPH: float | None = None
    windSpeedMPS: float | None = None
    windDirDEG: float | None = None
    # Wind gust
    windGustKPH: float | None = None
    windGustMPH: float | None = None
    windGustMPS: float | None = None
    # Precipitation
    precipMM: float | None = None
    precipIN: float | None = None
    pop: float | None = None    # probability of precipitation (0-100)
    # Dewpoint
    dewpointC: float | None = None
    dewpointF: float | None = None
    # Pressure
    pressureMB: float | None = None
    pressureIN: float | None = None
    # Sky/cloud
    sky: float | None = None   # 0-100 cloud cover percent
    # Weather codes
    weatherPrimaryCoded: str | None = None
    weather: str | None = None
    weatherPrimary: str | None = None
    uvi: float | None = None
    isDay: bool | None = None
    # Xcast ML-enhanced confidence limits — present only on /xcast/forecasts responses.
    # null where no Xcast sensors are deployed. Not present on standard /forecasts.
    tempConfidenceLimit: dict[str, float] | None = None
    windConfidenceLimit: dict[str, float] | None = None


class _AerisDayNightPeriod(BaseModel):
    """One day or night period from /forecasts?filter=daynight."""

    model_config = ConfigDict(extra="ignore")

    timestamp: int
    dateTimeISO: str
    # Temperature extremes — both unit systems
    maxTempC: float | None = None
    maxTempF: float | None = None
    minTempC: float | None = None
    minTempF: float | None = None
    # Wind speed max — both unit systems
    windSpeedKPH: float | None = None
    windSpeedMPH: float | None = None
    windSpeedMPS: float | None = None
    windSpeedMaxKPH: float | None = None
    windSpeedMaxMPH: float | None = None
    windSpeedMaxMPS: float | None = None   # may be absent; fall back to KPH÷3.6 per lead-call 13
    # Wind gust max — both unit systems
    windGustKPH: float | None = None
    windGustMPH: float | None = None
    windGustMPS: float | None = None
    windGustMaxKPH: float | None = None
    windGustMaxMPH: float | None = None
    windGustMaxMPS: float | None = None   # may be absent; fall back to KPH÷3.6 per lead-call 13
    # Wind direction
    windDirDEG: float | None = None
    # Precipitation
    precipMM: float | None = None
    precipIN: float | None = None
    pop: float | None = None
    # Sunrise/sunset
    sunriseISO: str | None = None
    sunsetISO: str | None = None
    # UV index
    uvi: float | None = None
    # Dewpoint extremes — both unit systems
    maxDewpointF: float | None = Field(default=None)
    maxDewpointC: float | None = Field(default=None)
    minDewpointF: float | None = Field(default=None)
    minDewpointC: float | None = Field(default=None)
    # Humidity
    humidity: float | None = Field(default=None)
    maxHumidity: float | None = Field(default=None)
    minHumidity: float | None = Field(default=None)
    # Visibility — both unit systems
    visibilityMI: float | None = Field(default=None)
    visibilityKM: float | None = Field(default=None)
    # Snow accumulation — both unit systems
    snowIN: float | None = Field(default=None)
    snowCM: float | None = Field(default=None)
    # Weather codes
    weatherPrimaryCoded: str | None = None
    weather: str | None = None
    weatherPrimary: str | None = None
    isDay: bool | None = None


class _AerisHourlyResponse(BaseModel):
    """Top-level /forecasts?filter=1hr response — wire shape."""

    model_config = ConfigDict(extra="ignore")

    loc: _AerisLoc | None = None
    profile: _AerisProfile | None = None
    periods: list[_AerisHourlyPeriod] = Field(default_factory=list)


class _AerisDayNightResponse(BaseModel):
    """Top-level /forecasts?filter=daynight response — wire shape."""

    model_config = ConfigDict(extra="ignore")

    loc: _AerisLoc | None = None
    profile: _AerisProfile | None = None
    periods: list[_AerisDayNightPeriod] = Field(default_factory=list)


class _AerisEnvelope(BaseModel):
    """Aeris response envelope — success/error wrapper."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    error: dict[str, Any] | None = None
    # response is a list of location objects; we always use [0]
    response: list[dict[str, Any]] = Field(default_factory=list)


class _AerisCurrentOb(BaseModel):
    """Wire shape of the ob block from /observations/{lat},{lon}."""

    model_config = ConfigDict(extra="ignore")

    weather: str | None = None
    weatherPrimaryCoded: str | None = None
    tempF: float | None = None
    tempC: float | None = None
    humidity: float | None = None
    windSpeedMPH: float | None = None
    windSpeedKPH: float | None = None
    windSpeedMPS: float | None = None
    windDirDEG: float | None = None
    sky: float | None = None
    isDay: bool | None = None
    precipIN: float | None = None
    precipMM: float | None = None
    snowDepthIN: float | None = None
    snowDepthCM: float | None = None


class _AerisCurrentResponse(BaseModel):
    """Wire shape of response[0] from /observations/{lat},{lon}."""

    model_config = ConfigDict(extra="ignore")

    ob: _AerisCurrentOb


class _AerisConvectiveRisk(BaseModel):
    """Wire shape of the risk block from /convective/outlook/{lat},{lon}."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None       # "general", "tornado", "hail", "wind"
    name: str | None = None
    code: float | None = None     # numeric risk level


class _AerisConvectiveDetails(BaseModel):
    """Wire shape of the details block from /convective/outlook/{lat},{lon}."""

    model_config = ConfigDict(extra="ignore")

    day: int | None = None        # 1-8
    risk: _AerisConvectiveRisk | None = None


class _AerisConvectiveItem(BaseModel):
    """Wire shape of one item from the /convective/outlook response list."""

    model_config = ConfigDict(extra="ignore")

    details: _AerisConvectiveDetails | None = None


# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3, brief lead-call 18)
# 5 req/s "be polite" guard — per-call acquire before each of two outbound
# calls per cache miss. Covers lowest documented Aeris paid-tier (10/s entry).
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="aeris-forecast",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# HTTP client (module-level singleton — one per module, not per request)
# ---------------------------------------------------------------------------

_http_client: ProviderHTTPClient | None = None


def _client_for() -> ProviderHTTPClient:
    """Return the module-level HTTP client, constructing on first call."""
    global _http_client  # noqa: PLW0603
    if _http_client is None:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent=f"weewx-clearskies-api/{_API_VERSION}",
        )
    return _http_client


# ---------------------------------------------------------------------------
# Cache key construction (ADR-017 §Cache key)
# ---------------------------------------------------------------------------


def _build_cache_key(
    lat: float, lon: float, target_unit: str, forecast_model: str = "standard"
) -> str:
    """Build a deterministic cache key for (provider_id, endpoint, {lat, lon, unit, model}).

    endpoint="forecast_bundle" covers the two upstream calls (hourly + daynight)
    per brief §cache integration. Lat/lon rounded to 4 decimal places per ADR-017.
    target_unit included so US and METRIC/METRICWX get separate cache entries
    (module picks different field names per unit system at ingest time).
    forecast_model included so "standard" and "xcast" get separate cache entries
    (they call different upstream URLs; mixing them would return wrong data).
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "forecast_bundle",
            "params": {
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "target_unit": target_unit,
                "forecast_model": forecast_model,
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Helpers — precipType derivation (brief lead-call 16)
# ---------------------------------------------------------------------------


def _aeris_descriptor_to_precip_type(coded: str | None) -> str | None:
    """Derive canonical precipType from Aeris weatherPrimaryCoded string.

    Aeris weatherPrimaryCoded is colon-delimited:
      <coverage>:<intensity>:<descriptor>
    e.g. ":HV:RW" (heavy rain shower), "::OVC" (overcast, no precip).
    The third segment (index 2) is the weather descriptor that drives
    precipType classification.

    Args:
        coded: Aeris weatherPrimaryCoded string or None.

    Returns:
        Canonical precipType string ("rain", "snow", "freezing-rain",
        "sleet", "hail") or None if no precipitation descriptor found.
    """
    if not coded:
        return None

    parts = coded.split(":")
    descriptor = parts[2] if len(parts) >= 3 else ""
    if not descriptor:
        return None

    result = _AERIS_DESCRIPTOR_TO_PRECIP_TYPE.get(descriptor)

    if result is not None:
        # Log mixed-precip descriptors once so future canonical amendment
        # is informed by real-data prevalence (brief lead-call 16).
        if descriptor in _MIXED_PRECIP_DESCRIPTORS and descriptor not in _logged_mixed_precip:
            _logged_mixed_precip.add(descriptor)
            logger.debug(
                "Aeris mixed-precip descriptor %r mapped to 'rain' "
                "(canonical has no mixed-precip enum; track for future amendment)",
                descriptor,
            )
        return result

    # Unknown descriptor — log once, return None
    if descriptor not in _logged_unknown_descriptors:
        _logged_unknown_descriptors.add(descriptor)
        logger.debug(
            "Aeris unknown weather descriptor %r → precipType=None "
            "(update _AERIS_DESCRIPTOR_TO_PRECIP_TYPE if this is a known type)",
            descriptor,
        )
    return None


# ---------------------------------------------------------------------------
# Helpers — unit-field selection (ADR-019, brief lead-call 13)
# ---------------------------------------------------------------------------


def _wind_speed_max_mps(period: _AerisDayNightPeriod) -> float | None:
    """Return windSpeedMax in m/s for METRICWX, with KPH fallback.

    Aeris doesn't document windSpeedMaxMPS explicitly; if absent, post-convert
    from windSpeedMaxKPH ÷ 3.6 per brief lead-call 13.
    """
    if period.windSpeedMaxMPS is not None:
        return period.windSpeedMaxMPS
    if period.windSpeedMaxKPH is not None:
        return period.windSpeedMaxKPH / 3.6
    return None


def _wind_gust_max_mps(period: _AerisDayNightPeriod) -> float | None:
    """Return windGustMax in m/s for METRICWX, with KPH fallback.

    Same post-convert pattern as _wind_speed_max_mps.
    """
    if period.windGustMaxMPS is not None:
        return period.windGustMaxMPS
    if period.windGustMaxKPH is not None:
        return period.windGustMaxKPH / 3.6
    return None


# ---------------------------------------------------------------------------
# Hourly period → canonical HourlyForecastPoint (canonical-data-model §4.1.2)
# ---------------------------------------------------------------------------


def _hourly_period_to_point(period: _AerisHourlyPeriod, target_unit: str) -> HourlyForecastPoint:
    """Translate one Aeris hourly period to canonical HourlyForecastPoint.

    Unit-field selection per ADR-019 + brief lead-call 13:
      US       → *F, *MPH, *IN
      METRIC   → *C, *KPH, *MM
      METRICWX → *C, *MPS, *MM
    Aeris returns both systems; module picks the matching field name.
    """
    # validTime: UTC ISO-8601 Z from offset-aware dateTimeISO
    valid_time = to_utc_iso8601_from_offset(
        period.dateTimeISO, provider_id=PROVIDER_ID, domain=DOMAIN
    )

    # Temperature
    if target_unit == "US":
        temp = period.tempF
        wind_speed = period.windSpeedMPH
        wind_gust = period.windGustMPH
        precip_amount = period.precipIN
    elif target_unit == "METRICWX":
        temp = period.tempC
        wind_speed = period.windSpeedMPS
        wind_gust = period.windGustMPS
        precip_amount = period.precipMM
    else:  # METRIC
        temp = period.tempC
        wind_speed = period.windSpeedKPH
        wind_gust = period.windGustKPH
        precip_amount = period.precipMM

    # Pass xcast confidence limits through extras when non-null.
    # These are present only on /xcast/forecasts responses; null where no sensors deployed.
    extras: dict[str, Any] = {}
    if period.tempConfidenceLimit is not None:
        extras["tempConfidenceLimit"] = period.tempConfidenceLimit
    if period.windConfidenceLimit is not None:
        extras["windConfidenceLimit"] = period.windConfidenceLimit

    return HourlyForecastPoint(
        validTime=valid_time,
        outTemp=temp,
        outHumidity=period.humidity,
        windSpeed=wind_speed,
        windDir=period.windDirDEG,
        windGust=wind_gust,
        precipProbability=period.pop,
        precipAmount=precip_amount,
        precipType=_aeris_descriptor_to_precip_type(period.weatherPrimaryCoded),
        cloudCover=period.sky,
        weatherCode=period.weatherPrimaryCoded,
        weatherText=period.weather,
        source=PROVIDER_ID,
        extras=extras,
    )


# ---------------------------------------------------------------------------
# Day/night period → canonical DailyForecastPoint (canonical-data-model §4.1.3)
# Aeris filter=daynight returns alternating day and night periods.
# Module pairs consecutive periods: day period + immediately-following night period.
# Only day-period values are used for the canonical DailyForecastPoint;
# validDate is derived from the day period's dateTimeISO (local date, before UTC conv).
# ---------------------------------------------------------------------------


def _daynight_periods_to_daily(
    periods: list[_AerisDayNightPeriod],
    target_unit: str,
) -> list[DailyForecastPoint]:
    """Translate Aeris day/night period pairs to canonical DailyForecastPoint list.

    Aeris filter=daynight returns alternating day/night pairs in chronological
    order: [day0, night0, day1, night1, ...]. We iterate by stepping 2 and
    using the day period for canonical daily values.

    Aeris wire behaviour (confirmed from captured fixture):
      - Day periods carry maxTempC/maxTempF; minTemp fields are null.
      - Night periods carry minTempC/minTempF; maxTemp fields are null.
    tempMin is therefore read from the immediately-following night period
    (index i+1).  When the trailing night period is absent (last day in a
    truncated response), tempMin is None.

    validDate: date portion of dateTimeISO BEFORE UTC conversion — the offset
    IS the station-local one Aeris applies via profile.tz lookup (brief call 22).
    sunrise/sunset: converted to UTC ISO-8601 Z via to_utc_iso8601_from_offset.
    """
    points: list[DailyForecastPoint] = []
    # Step through day periods (even indices — isDay=True)
    i = 0
    while i < len(periods):
        day_period = periods[i]
        # Skip night-only periods if they appear at the start (defensive)
        if day_period.isDay is False:
            i += 1
            continue

        # Night period immediately follows the day period in the paired list.
        # minTemp lives on the night period; it is always null on the day period.
        night_period: _AerisDayNightPeriod | None = (
            periods[i + 1] if i + 1 < len(periods) else None
        )

        # validDate: station-local date extracted from dateTimeISO before any conversion
        valid_date = day_period.dateTimeISO[:10]   # "YYYY-MM-DD"

        # Unit-field selection per ADR-019 + brief lead-call 13.
        # maxTemp comes from the day period; minTemp from the night period.
        if target_unit == "US":
            temp_max = day_period.maxTempF
            temp_min = night_period.minTempF if night_period is not None else None
            wind_speed_max = day_period.windSpeedMaxMPH
            wind_gust_max = day_period.windGustMaxMPH
            precip_amount = day_period.precipIN
            dewpoint_max = day_period.maxDewpointF
            dewpoint_min = day_period.minDewpointF
            visibility_max = day_period.visibilityMI
            snow_amount = day_period.snowIN
        elif target_unit == "METRICWX":
            temp_max = day_period.maxTempC
            temp_min = night_period.minTempC if night_period is not None else None
            wind_speed_max = _wind_speed_max_mps(day_period)
            wind_gust_max = _wind_gust_max_mps(day_period)
            precip_amount = day_period.precipMM
            dewpoint_max = day_period.maxDewpointC
            dewpoint_min = day_period.minDewpointC
            visibility_max = day_period.visibilityKM
            snow_amount = day_period.snowCM
        else:  # METRIC
            temp_max = day_period.maxTempC
            temp_min = night_period.minTempC if night_period is not None else None
            wind_speed_max = day_period.windSpeedMaxKPH
            wind_gust_max = day_period.windGustMaxKPH
            precip_amount = day_period.precipMM
            dewpoint_max = day_period.maxDewpointC
            dewpoint_min = day_period.minDewpointC
            visibility_max = day_period.visibilityKM
            snow_amount = day_period.snowCM

        # Sunrise/sunset: convert from local ISO-with-offset to UTC Z
        sunrise_utc: str | None = None
        if day_period.sunriseISO:
            sunrise_utc = to_utc_iso8601_from_offset(
                day_period.sunriseISO, provider_id=PROVIDER_ID, domain=DOMAIN
            )

        sunset_utc: str | None = None
        if day_period.sunsetISO:
            sunset_utc = to_utc_iso8601_from_offset(
                day_period.sunsetISO, provider_id=PROVIDER_ID, domain=DOMAIN
            )

        extras: dict[str, Any] = {}
        if day_period.windDirDEG is not None:
            extras["windDir"] = day_period.windDirDEG

        points.append(
            DailyForecastPoint(
                validDate=valid_date,
                tempMax=temp_max,
                tempMin=temp_min,
                precipAmount=precip_amount,
                precipProbabilityMax=day_period.pop,
                windSpeedMax=wind_speed_max,
                windGustMax=wind_gust_max,
                sunrise=sunrise_utc,
                sunset=sunset_utc,
                uvIndexMax=day_period.uvi,
                weatherCode=day_period.weatherPrimaryCoded,
                weatherText=day_period.weather,
                narrative=day_period.weatherPrimary,
                dewpointMax=dewpoint_max,
                dewpointMin=dewpoint_min,
                humidityMax=day_period.maxHumidity,
                humidityMin=day_period.minHumidity,
                visibilityMax=visibility_max,
                snowAmount=snow_amount,
                source=PROVIDER_ID,
                extras=extras,
            )
        )
        # Skip both day and night periods (or just this day if no night follows)
        i += 2

    return points


# ---------------------------------------------------------------------------
# ForecastDiscussion runtime detection (brief Q2, user decision 2026-05-08)
# ---------------------------------------------------------------------------


def _extract_aeris_discussion(
    daynight_raw: dict[str, Any],
    first_period_raw: dict[str, Any] | None,
    *,
    provider_id: str,
    domain: str,
) -> ForecastDiscussion | None:
    """Attempt runtime detection of paid-tier summary field for ForecastDiscussion.

    Checks two candidate locations per brief lead-call 14:
      - daynight_raw["summary"]  (response-level summary)
      - first_period_raw["summary"]  (per-period summary)

    When a non-empty string is found, constructs ForecastDiscussion with:
      headline = weatherPrimary of first period
      body = detected summary string
      source = "aeris"
      issuedAt = UTC-converted dateTimeISO of first period
      validFrom = None (Aeris doesn't expose a forecast-valid-from timestamp)
      validUntil = None

    When absent/empty/whitespace-only → returns None (free-tier default).

    Args:
        daynight_raw: Raw dict for response[0] from daynight Pydantic model.
        first_period_raw: Raw dict for response[0].periods[0] or None.
        provider_id: For ProviderProtocolError context.
        domain: For ProviderProtocolError context.
    """
    summary_text: str | None = None

    # Check response-level summary first
    candidate = daynight_raw.get("summary")
    if isinstance(candidate, str) and candidate.strip():
        summary_text = candidate.strip()
        logger.debug("Aeris: detected response-level summary field (paid-tier)")

    # Fall back to period-level summary
    if summary_text is None and first_period_raw is not None:
        candidate = first_period_raw.get("summary")
        if isinstance(candidate, str) and candidate.strip():
            summary_text = candidate.strip()
            logger.debug("Aeris: detected period-level summary field (paid-tier)")

    if summary_text is None:
        return None

    # Build ForecastDiscussion from first period data
    headline: str | None = None
    issued_at: str | None = None

    if first_period_raw is not None:
        headline = first_period_raw.get("weatherPrimary") or None
        raw_dt = first_period_raw.get("dateTimeISO")
        if isinstance(raw_dt, str):
            try:
                issued_at = to_utc_iso8601_from_offset(
                    raw_dt, provider_id=provider_id, domain=domain
                )
            except ProviderProtocolError:
                # to_utc_iso8601_from_offset raises ProviderProtocolError on
                # malformed input. Discussion issuedAt is best-effort; absent
                # is acceptable per canonical §3.5 (issuedAt nullable).
                logger.debug("Aeris: could not parse dateTimeISO for discussion issuedAt")
                issued_at = None

    return ForecastDiscussion(
        headline=headline,
        body=summary_text,
        source=PROVIDER_ID,
        issuedAt=issued_at,
        validFrom=None,
        validUntil=None,
        senderName=None,
    )


# ---------------------------------------------------------------------------
# Wire → canonical normalization (canonical-data-model §4.1.2 / §4.1.3)
# ---------------------------------------------------------------------------


def _to_canonical(
    hourly_wire: _AerisHourlyResponse,
    daynight_wire: _AerisDayNightResponse,
    *,
    target_unit: str,
    daynight_raw: dict[str, Any],
) -> ForecastBundle:
    """Translate Aeris wire responses to canonical ForecastBundle.

    hourly: translated from hourly_wire.periods.
    daily: paired from daynight_wire.periods (day periods only).
    discussion: runtime-detected from daynight_raw (paid-tier only, None for free-tier).
    source: PROVIDER_ID ("aeris").
    generatedAt: current UTC timestamp.
    """
    hourly_points = [
        _hourly_period_to_point(p, target_unit) for p in hourly_wire.periods
    ]

    daily_points = _daynight_periods_to_daily(daynight_wire.periods, target_unit)

    # Runtime detection of paid-tier summary (Q2)
    first_period_raw: dict[str, Any] | None = None
    if daynight_raw.get("periods") and isinstance(daynight_raw["periods"], list):
        raw_periods = daynight_raw["periods"]
        first_period_raw = raw_periods[0] if raw_periods else None

    discussion = _extract_aeris_discussion(
        daynight_raw=daynight_raw,
        first_period_raw=first_period_raw,
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
    )

    return ForecastBundle(
        hourly=hourly_points,
        daily=daily_points,
        discussion=discussion,
        source=PROVIDER_ID,
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


# ---------------------------------------------------------------------------
# Internal fetch helpers — one per outbound call
# ---------------------------------------------------------------------------


def _fetch_hourly(
    client: ProviderHTTPClient,
    lat: float,
    lon: float,
    client_id: str,
    client_secret: str,
    forecasts_path: str = AERIS_FORECASTS_PATH,
) -> _AerisHourlyResponse:
    """GET {forecasts_path}/{lat},{lon}?filter=1hr and validate wire shape.

    forecasts_path: AERIS_FORECASTS_PATH ("/forecasts") for standard model,
      AERIS_XCAST_FORECASTS_PATH ("/xcast/forecasts") for ML-enhanced xcast model.

    Raises:
        KeyInvalid: HTTP 401 (invalid credentials).
        QuotaExhausted: HTTP 429 (rate limit exceeded).
        ProviderProtocolError: HTTP 200 with success=false, or validation failure.
        TransientNetworkError: Network failure / 5xx after retries (from ProviderHTTPClient).
    """
    location = f"{round(lat, 4)},{round(lon, 4)}"
    url = f"{AERIS_BASE_URL}{forecasts_path}/{location}"
    params = {
        "filter": "1hr",
        "limit": str(HOURLY_LIMIT),
        "client_id": client_id,
        "client_secret": client_secret,
    }

    _rate_limiter.acquire()
    # ProviderHTTPClient.get raises canonical taxonomy exceptions (KeyInvalid,
    # QuotaExhausted, TransientNetworkError, ProviderProtocolError) with all
    # structured attributes set (status_code, retry_after_seconds). Let them
    # propagate; do NOT re-wrap (3b-4 audit F1/F2: re-construction dropped
    # retry_after_seconds from QuotaExhausted, and `except Exception` violates
    # rules/coding.md §3).
    response = client.get(url, params=params)

    return _parse_aeris_envelope(response, model_class=_AerisHourlyResponse, call_label="hourly")


def _fetch_daynight(
    client: ProviderHTTPClient,
    lat: float,
    lon: float,
    client_id: str,
    client_secret: str,
) -> tuple[_AerisDayNightResponse, dict[str, Any]]:
    """GET /forecasts/{lat},{lon}?filter=daynight and validate wire shape.

    Returns:
        Tuple of (validated _AerisDayNightResponse, raw response[0] dict).
        The raw dict is used for paid-tier summary detection (brief Q2).

    Raises: same taxonomy as _fetch_hourly.
    """
    location = f"{round(lat, 4)},{round(lon, 4)}"
    url = f"{AERIS_BASE_URL}{AERIS_FORECASTS_PATH}/{location}"
    params = {
        "filter": "daynight",
        "limit": str(DAYNIGHT_LIMIT),
        "client_id": client_id,
        "client_secret": client_secret,
    }

    _rate_limiter.acquire()
    # ProviderHTTPClient.get raises canonical taxonomy exceptions; let them
    # propagate (3b-4 audit F1/F2 — see _fetch_hourly).
    response = client.get(url, params=params)

    raw_response_list = _parse_aeris_envelope_raw(response, call_label="daynight")
    raw_first = raw_response_list[0] if raw_response_list else {}

    try:
        validated = _AerisDayNightResponse.model_validate(raw_first)
    except ValidationError as exc:
        logger.error(
            "Aeris daynight response[0] validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"Aeris daynight response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    return validated, raw_first


def _fetch_convective_outlook(
    client: ProviderHTTPClient,
    lat: float,
    lon: float,
    client_id: str,
    client_secret: str,
) -> dict[int, dict[str, float]]:
    """Fetch Aeris convective outlook and return {day: {risk_type: risk_code}}.

    Calls GET /convective/outlook/{lat},{lon} and maps the response items to a
    dict keyed by forecast day number (1-8), where each value is a dict of
    risk type ("general", "tornado", "hail", "wind") to numeric risk code.

    Returns empty dict on any failure (non-US stations, network error, etc.)
    since convective outlook is optional supplementary data.  The broad
    except Exception guard is intentional here — this call must never cause
    the main forecast fetch to fail.

    Args:
        client: Module-level ProviderHTTPClient singleton.
        lat: Station latitude, rounded to 4 decimal places in the URL.
        lon: Station longitude, rounded to 4 decimal places in the URL.
        client_id: Aeris client_id credential.
        client_secret: Aeris client_secret credential.

    Returns:
        {day_number: {"general": code, "tornado": code, ...}} or {} on failure.
    """
    location = f"{round(lat, 4)},{round(lon, 4)}"
    url = f"{AERIS_BASE_URL}{AERIS_CONVECTIVE_PATH}/{location}"
    params = {
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        _rate_limiter.acquire()
        response = client.get(url, params=params)
    except Exception:
        logger.debug(
            "Aeris convective outlook fetch failed (non-fatal; supplementary data)",
            exc_info=True,
        )
        return {}

    try:
        raw = response.json()
    except Exception:
        logger.debug("Aeris convective outlook: could not parse JSON response (non-fatal)")
        return {}

    if not raw.get("success") or not raw.get("response"):
        return {}

    result: dict[int, dict[str, float]] = {}
    for item_data in raw["response"]:
        try:
            item = _AerisConvectiveItem.model_validate(item_data)
        except Exception:
            continue
        if (
            item.details
            and item.details.day
            and item.details.risk
            and item.details.risk.type
            and item.details.risk.code is not None
        ):
            day = item.details.day
            if day not in result:
                result[day] = {}
            result[day][item.details.risk.type] = item.details.risk.code

    return result


# ---------------------------------------------------------------------------
# Envelope parsing helpers
# ---------------------------------------------------------------------------


def _parse_aeris_envelope_raw(response: Any, *, call_label: str) -> list[dict[str, Any]]:
    """Parse the Aeris success/error envelope and return the raw response list.

    On success=false: raises ProviderProtocolError.
    On success=true with warn_location: logs WARNING and returns empty list
      (caller returns empty bundle per brief lead-call 17).
    On success=true with response=[]: returns empty list.

    Args:
        response: httpx.Response from ProviderHTTPClient.get().
        call_label: "hourly" or "daynight" for error context.

    Raises:
        ProviderProtocolError: success=false or envelope parse failure.
    """
    try:
        raw = response.json()
        # Aeris returns response as a list for batch queries but as a single
        # object for single-location queries (/observations/{lat},{lon}).
        # Normalize to list so the envelope model always sees a list.
        if isinstance(raw.get("response"), dict):
            raw["response"] = [raw["response"]]
        envelope = _AerisEnvelope.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        logger.error(
            "Aeris %s envelope parse failed: %s. Body (first 2000 chars): %.2000s",
            call_label, exc, response.text,
        )
        raise ProviderProtocolError(
            f"Aeris {call_label} envelope parse failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    if not envelope.success:
        error_code = ""
        if envelope.error:
            error_code = envelope.error.get("code", "")
            error_desc = envelope.error.get("description", "")
        else:
            error_desc = "unknown error"
        raise ProviderProtocolError(
            f"Aeris {call_label} returned success=false: code={error_code!r} {error_desc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    # success=true with a warning (e.g. warn_location) — log and continue
    if envelope.error:
        warn_code = envelope.error.get("code", "")
        warn_desc = envelope.error.get("description", "")
        logger.warning(
            "Aeris %s returned success=true with warning: code=%r %s",
            call_label, warn_code, warn_desc,
        )

    return envelope.response


def _parse_aeris_envelope(
    response: Any,
    *,
    model_class: type,
    call_label: str,
) -> Any:
    """Parse envelope and validate response[0] against model_class.

    Used for the hourly call where we don't need the raw dict.
    Returns a validated Pydantic model instance.
    """
    raw_list = _parse_aeris_envelope_raw(response, call_label=call_label)
    raw_first = raw_list[0] if raw_list else {}

    try:
        return model_class.model_validate(raw_first)
    except ValidationError as exc:
        logger.error(
            "Aeris %s response[0] validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            call_label, exc, response.text,
        )
        raise ProviderProtocolError(
            f"Aeris {call_label} response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    target_unit: str,
    client_id: str | None,
    client_secret: str | None,
    forecast_model: str = "xcast",
) -> ForecastBundle:
    """Call Aeris /forecasts (1hr + daynight) and return canonical ForecastBundle.

    Two outbound calls per cache miss: filter=1hr for hourly, filter=daynight
    for paired day/night periods. Both results are normalised and cached as a
    single ForecastBundle.

    Cache-first: check cache before making outbound HTTP calls.
    Cache stores post-normalization ForecastBundle as model_dump(mode="json")
    dict; reconstructed via ForecastBundle.model_validate() on cache hit.
    Cache key includes target_unit so US and metric systems get separate entries,
    and forecast_model so "standard" and "xcast" get separate cache entries.

    Slice-after-cache pattern (ADR-017):
    The FULL bundle is stored in cache regardless of the requested hours/days.
    The endpoint handler slices to the requested count AFTER cache lookup.

    Forecast model selection (ADR-063):
    When forecast_model="xcast", hourly call uses /xcast/forecasts (ML-enhanced
    temp/wind). Daynight call always uses /forecasts because xcast ignores the
    filter=daynight parameter and returns hourly data instead.

    Args:
        lat: Station latitude from services/station.py StationInfo.
        lon: Station longitude from services/station.py StationInfo.
        target_unit: Weewx unit system ("US" | "METRIC" | "METRICWX") from
            services/units.py get_target_unit().
        client_id: Aeris client_id from env var WEEWX_CLEARSKIES_AERIS_CLIENT_ID.
            None if operator hasn't configured it.
        client_secret: Aeris client_secret from env var
            WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET. None if not configured.
        forecast_model: "xcast" (default) for ML-enhanced temp/wind via
            /xcast/forecasts, or "standard" for /forecasts. Set from
            aeris_forecast_model in api.conf [forecast] section (ADR-063).

    Returns:
        ForecastBundle — single canonical Pydantic model.
        discussion is None for free-tier; populated for paid-tier when
        summary field is detected (brief Q2).

    Raises:
        KeyInvalid: Credentials missing (both args None), or Aeris returned 401.
        QuotaExhausted: Aeris returned 429.
        ProviderProtocolError: target_unit unknown, response validation failed,
            or Aeris returned success=false envelope.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
    """
    # Validate credentials before making any outbound call (brief lead-call 12).
    # Loud failure beats silent disable — operator intent is unambiguous.
    if not client_id or not client_secret:
        raise KeyInvalid(
            "Aeris credentials missing — set WEEWX_CLEARSKIES_AERIS_CLIENT_ID "
            "and WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET env vars",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    # Resolve forecast model to hourly path.
    # Daynight always uses standard /forecasts — xcast ignores filter=daynight.
    hourly_path = (
        AERIS_XCAST_FORECASTS_PATH if forecast_model == "xcast" else AERIS_FORECASTS_PATH
    )

    cache_key = _build_cache_key(lat, lon, target_unit, forecast_model=forecast_model)
    cached = get_cache().get(cache_key)
    if cached is not None:
        logger.debug(
            "Cache hit for Aeris forecast",
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        return ForecastBundle.model_validate(cached)

    logger.info(
        "Aeris forecast using %s model (hourly path: %s)",
        forecast_model, hourly_path,
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )
    logger.debug(
        "Cache miss for Aeris forecast; calling API (two calls: 1hr + daynight)",
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )

    if target_unit not in {"US", "METRIC", "METRICWX"}:
        # Defensive: services/units.py validates at startup; should not fire.
        raise ProviderProtocolError(
            f"Unknown target_unit {target_unit!r}; expected US, METRIC, or METRICWX",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    client = _client_for()

    # Call 1: hourly periods — uses xcast path when forecast_model="xcast"
    hourly_wire = _fetch_hourly(
        client, lat, lon, client_id, client_secret, forecasts_path=hourly_path
    )

    # Call 2: daynight periods + raw dict for discussion detection
    daynight_wire, daynight_raw = _fetch_daynight(client, lat, lon, client_id, client_secret)

    bundle = _to_canonical(
        hourly_wire,
        daynight_wire,
        target_unit=target_unit,
        daynight_raw=daynight_raw,
    )

    # Overlay convective outlook risk data onto daily points (US-only; returns
    # empty dict for non-US stations or on any failure — non-fatal supplementary data).
    convective = _fetch_convective_outlook(client, lat, lon, client_id, client_secret)
    if convective:
        for i, day_point in enumerate(bundle.daily):
            day_num = i + 1  # Day 1 = first forecast day
            risks = convective.get(day_num)
            if risks:
                updates: dict[str, float] = {}
                if "general" in risks:
                    updates["thunderRisk"] = risks["general"]
                if "tornado" in risks:
                    updates["tornadoRisk"] = risks["tornado"]
                if "hail" in risks:
                    updates["hailRisk"] = risks["hail"]
                if "wind" in risks:
                    updates["windRisk"] = risks["wind"]
                if updates:
                    bundle.daily[i] = day_point.model_copy(update=updates)

    get_cache().set(
        cache_key,
        bundle.model_dump(mode="json"),
        ttl_seconds=DEFAULT_FORECAST_TTL_SECONDS,
    )

    logger.info(
        "Aeris forecast fetched: %d hourly, %d daily point(s)",
        len(bundle.hourly),
        len(bundle.daily),
        extra={
            "provider_id": PROVIDER_ID,
            "domain": DOMAIN,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "target_unit": target_unit,
        },
    )
    return bundle


def _build_current_conditions_cache_key(lat: float, lon: float, target_unit: str) -> str:
    """Build a deterministic cache key for the Aeris current-conditions call.

    Separate from the forecast bundle key so TTL and invalidation are independent.
    endpoint="current_conditions" per brief spec.
    Lat/lon rounded to 4 decimal places per ADR-017.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "current_conditions",
            "params": {
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "target_unit": target_unit,
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def fetch_current_conditions(
    *,
    lat: float,
    lon: float,
    target_unit: str,
    client_id: str | None,
    client_secret: str | None,
) -> ProviderConditions | None:
    """Call Aeris /observations/{lat},{lon} and return ProviderConditions.

    Uses the same HTTP client, rate limiter, and error-handling patterns as
    fetch().  Cache key uses endpoint="current_conditions" so its TTL (300 s)
    is independent of the forecast bundle TTL (1800 s).

    Unit-field selection mirrors fetch() hourly-period logic:
      US       → tempF, windSpeedMPH
      METRIC   → tempC, windSpeedKPH
      METRICWX → tempC, windSpeedMPS

    weatherText = ob.weather
    cloudCover  = ob.sky (0-100 percent)
    precipType  derived from ob.weatherPrimaryCoded via existing
                _aeris_descriptor_to_precip_type().

    Args:
        lat: Station latitude.
        lon: Station longitude.
        target_unit: Weewx unit system ("US" | "METRIC" | "METRICWX").
        client_id: Aeris client_id from env var WEEWX_CLEARSKIES_AERIS_CLIENT_ID.
        client_secret: Aeris client_secret from env var WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET.

    Returns:
        ProviderConditions on success; None when the Aeris response is empty
        (warn_location path — location outside Aeris coverage).

    Raises:
        KeyInvalid: Credentials missing, or Aeris returned 401.
        QuotaExhausted: Aeris returned 429.
        ProviderProtocolError: Response validation failure, unknown target_unit,
            or Aeris returned success=false.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
    """
    if not client_id or not client_secret:
        raise KeyInvalid(
            "Aeris credentials missing — set WEEWX_CLEARSKIES_AERIS_CLIENT_ID "
            "and WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET env vars",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    if target_unit not in {"US", "METRIC", "METRICWX"}:
        raise ProviderProtocolError(
            f"Unknown target_unit {target_unit!r}; expected US, METRIC, or METRICWX",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    cache_key = _build_current_conditions_cache_key(lat, lon, target_unit)
    cached = get_cache().get(cache_key)
    if cached is not None:
        logger.debug(
            "Cache hit for Aeris current conditions",
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        return ProviderConditions.model_validate(cached)

    logger.debug(
        "Cache miss for Aeris current conditions; calling /observations",
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )

    location = f"{round(lat, 4)},{round(lon, 4)}"
    url = f"{AERIS_BASE_URL}{AERIS_OBSERVATIONS_PATH}/{location}"
    params = {
        "client_id": client_id,
        "client_secret": client_secret,
    }

    _rate_limiter.acquire()
    # ProviderHTTPClient.get raises canonical taxonomy exceptions; let them
    # propagate — do NOT re-wrap (same rule as _fetch_hourly).
    response = _client_for().get(url, params=params)

    raw_list = _parse_aeris_envelope_raw(response, call_label="observations")
    if not raw_list:
        # warn_location path — location outside Aeris coverage; return None.
        logger.warning(
            "Aeris observations returned empty response list for lat=%s,lon=%s — "
            "location may be outside coverage",
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        return None

    raw_first = raw_list[0]
    try:
        current_wire = _AerisCurrentResponse.model_validate(raw_first)
    except ValidationError as exc:
        logger.error(
            "Aeris observations response[0] validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"Aeris observations response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    ob = current_wire.ob

    # Unit-field selection mirrors hourly-period logic (ADR-019).
    if target_unit == "US":
        temperature = ob.tempF
        wind_speed = ob.windSpeedMPH
    elif target_unit == "METRICWX":
        temperature = ob.tempC
        wind_speed = ob.windSpeedMPS
    else:  # METRIC
        temperature = ob.tempC
        wind_speed = ob.windSpeedKPH

    conditions = ProviderConditions(
        weatherText=ob.weather,
        weatherCode=ob.weatherPrimaryCoded,
        precipType=_aeris_descriptor_to_precip_type(ob.weatherPrimaryCoded),
        cloudCover=ob.sky,
        isDay=ob.isDay,
        temperature=temperature,
        humidity=ob.humidity,
        windSpeed=wind_speed,
        windDir=ob.windDirDEG,
        source=PROVIDER_ID,
    )

    get_cache().set(
        cache_key,
        conditions.model_dump(mode="json"),
        ttl_seconds=DEFAULT_CONDITIONS_TTL_SECONDS,
    )

    logger.info(
        "Aeris current conditions fetched",
        extra={
            "provider_id": PROVIDER_ID,
            "domain": DOMAIN,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "target_unit": target_unit,
            "cloudCover": conditions.cloudCover,
            "weatherText": conditions.weatherText,
        },
    )
    return conditions


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
