"""OpenAQ v3 AQI provider module (ADR-066, ADR-038).

Five responsibilities per ADR-038 §2:
  1. Outbound API calls — up to four endpoints per fetch cycle:
       a. GET https://api.openaq.org/v3/locations?coordinates={lat},{lon}&radius=25000
          — Resolves nearest PM2.5 sensor (step 1, first call only).
          If PM10 not co-located, a second identical request scans all returned
          locations for a PM10 sensor (step 2, first call only).
       b. GET https://api.openaq.org/v3/locations/{locationId}/latest
          — Fetches latest sensor measurements from the PM2.5 station.
          If PM10 is on a DIFFERENT station, a second /latest call fetches from
          the PM10 station and its pm10 value overrides the primary result.
  2. Response parsing — wire-shape Pydantic models for the OpenAQ meta+results
     envelope (LC5).  extra="ignore" on all models (OpenAQ carries many fields
     canonical AQIReading does not consume: coordinates, isMobile, isAnalysis,
     entity, country, etc.).
  3. Translation to canonical AQIReading (_wire_to_canonical):
       - aqi = None (OpenAQ does not compute composite AQI index)
       - aqiScale = None
       - aqiCategory = None
       - aqiMainPollutant = None
       - aqiLocation = PM2.5 station name resolved in step 1a
       - pollutantPM25 = value for parameter "pm25" in µg/m³
       - pollutantPM10 = value for parameter "pm10" in µg/m³ (if available,
         may come from a separate nearby station)
       - All gas fields (O3, NO2, SO2, CO) = None
       - observedAt = measurement datetime UTC Z form (from datetime.utc field)
       - source = "openaq"
  4. Capability declaration — CAPABILITY symbol consumed at startup.
     is_observed_source=True — government reference monitors; haze-eligible.
  5. Error handling — ProviderHTTPClient.get() raises canonical taxonomy with all
     attributes set.  No re-construction of canonical exceptions from HTTP-level
     errors (L2 carry-forward, 3b-4 audit F1).

OpenAQ is a header-keyed provider (ADR-006):
  X-API-Key header (not query param — OpenAQ's own authentication pattern).
  Key from env var WEEWX_CLEARSKIES_OPENAQ_API_KEY.
  Credentials NOT in the cache key (LC7 — privacy/leakage concern).

Module-level sensor resolution (once per process):
  _resolved_pm25_location_id: int | None — OpenAQ locationId for PM2.5 station
  _resolved_pm25_location_name: str | None — PM2.5 station name for aqiLocation
  _resolved_pm25_sensor_id: int | None — sensorsId for PM2.5 sensor
  _resolved_pm10_location_id: int | None — OpenAQ locationId for PM10 station
    (may equal _resolved_pm25_location_id if co-located, or differ)
  _resolved_pm10_sensor_id: int | None — sensorsId for PM10 sensor (may be None)
  Resolved on first fetch() call via _resolve_sensors().
  Reset by _reset_sensor_state_for_tests() in test context.

Cache layer (ADR-017 / LC3 / LC6 / LC7):
  TTL: 3600s (1 hour) per spec — data lag ~1-2 hours makes shorter TTL wasteful.
  Key: SHA-256 of (provider_id="openaq", endpoint="aqi_current", {lat4, lon4}).
  Credentials NOT in key (LC7 — privacy/leakage concern).
  Value: model_dump() dict (JSON-serializable for Redis backend).
  Sentinel: {"_no_reading": True} when provider returns empty/null reading.
  Reconstruction on hit: AQIReading.model_validate(cached_dict).

Rate limiter (LC8):
  max_calls=2, window_seconds=1 (60 req/min free tier; paired calls in resolution
  and fetch phases need 2 slots per second without hitting QuotaExhausted).

Wire shape — GET /v3/locations (nearest station search):
  {
    "meta": {"name": "openaq-api", "page": 1, "limit": 100, "found": N},
    "results": [{
      "id": 12345,
      "name": "Station Name",
      "sensors": [{"id": 678, "name": "pm25", "parameter": {...}}, ...]
      ...
    }, ...]
  }

Wire shape — GET /v3/locations/{id}/latest:
  {
    "meta": {...},
    "results": [{
      "sensorsId": 678,
      "value": 12.5,
      "parameter": {"id": 2, "name": "pm25", "units": "µg/m³", "displayName": "PM2.5"},
      "datetime": {"utc": "2026-06-22T10:00:00Z", "local": "2026-06-22T06:00:00-04:00"},
      ...
    }, ...]
  }

ruff: noqa: N815  (wire field names include camelCase: sensorsId, displayName)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, ConfigDict, ValidationError

from weewx_clearskies_api.models.responses import AQIReading
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import ProviderCapability
from weewx_clearskies_api.providers._common.errors import (
    GeographicallyUnsupported,
    ProviderProtocolError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "openaq"
DOMAIN = "aqi"
DEFAULT_TTL_SECONDS = 3600  # 1 hour — data lag makes shorter TTL wasteful
_API_VERSION = "0.1.0"
OPENAQ_BASE_URL = "https://api.openaq.org"
OPENAQ_LOCATIONS_PATH = "/v3/locations"
OPENAQ_LATEST_PATH_TMPL = "/v3/locations/{location_id}/latest"

# Search radius for nearest station (OpenAQ max is 25,000 m).
_SEARCH_RADIUS_METERS = 25000

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        "pollutantPM25", "pollutantPM10",
        "aqiLocation", "observedAt", "source",
    ),
    geographic_coverage="global",
    auth_required=("api_key",),
    default_poll_interval_seconds=DEFAULT_TTL_SECONDS,
    is_observed_source=True,  # Government reference monitors — haze-eligible (ADR-066)
    operator_notes=(
        "OpenAQ v3 API. Aggregates government reference-grade PM monitors from "
        "141 countries (~2016-present). Free API key required (register at "
        "https://explore.openaq.org/register). Data lag ~1-2 hours from "
        "measurement — not recommended as primary provider when Aeris or IQAir "
        "is available. Provides PM2.5 and PM10 only (no composite AQI, no gases). "
        "Haze-eligible (is_observed_source=True)."
    ),
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (LC5 — extra="ignore"; required fields enumerated)
# Source: docs/reference/api-docs/openaq-v3.md
# ---------------------------------------------------------------------------


class _OpenAQParameter(BaseModel):
    """Parameter sub-object in /latest results (LC5)."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    name: str  # "pm25", "pm10", "o3", etc.
    units: str | None = None
    displayName: str | None = None


class _OpenAQDatetime(BaseModel):
    """Datetime sub-object in /latest results (LC5).

    OpenAQ returns both UTC and local forms.  ADR-020 mandates UTC ISO-8601 Z.
    utc field is already Z-form ("2026-06-22T10:00:00Z"); no conversion needed
    if it conforms.  We use to_utc_iso8601_from_offset() on the local field as
    a fallback only if utc is absent.
    """

    model_config = ConfigDict(extra="ignore")

    utc: str | None = None    # "2026-06-22T10:00:00Z" — preferred
    local: str | None = None  # "2026-06-22T06:00:00-04:00" — fallback


class _OpenAQLatestResult(BaseModel):
    """One result entry from /v3/locations/{id}/latest (LC5)."""

    model_config = ConfigDict(extra="ignore")

    sensorsId: int
    value: float | None = None
    parameter: _OpenAQParameter
    datetime: _OpenAQDatetime | None = None


class _OpenAQLatestResponse(BaseModel):
    """Top-level /latest response envelope (LC5)."""

    model_config = ConfigDict(extra="ignore")

    results: list[_OpenAQLatestResult] = []


class _OpenAQSensor(BaseModel):
    """Sensor entry within a location's sensors list (LC5)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str  # "pm25", "pm10", etc.  Used for parameter type matching.


class _OpenAQLocation(BaseModel):
    """One location result from /v3/locations?coordinates=... (LC5)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str | None = None
    sensors: list[_OpenAQSensor] = []


class _OpenAQLocationsResponse(BaseModel):
    """Top-level /v3/locations response envelope (LC5)."""

    model_config = ConfigDict(extra="ignore")

    results: list[_OpenAQLocation] = []


# ---------------------------------------------------------------------------
# Module-level sensor resolution state
# ---------------------------------------------------------------------------

_resolved_pm25_location_id: int | None = None
_resolved_pm25_location_name: str | None = None
_resolved_pm25_sensor_id: int | None = None
_resolved_pm10_location_id: int | None = None
_resolved_pm10_sensor_id: int | None = None

# ---------------------------------------------------------------------------
# Rate limiter (LC8 — 60 req/min free tier; 2 slots/s for paired calls)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="openaq-aqi",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=2,
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
# Cache key construction (ADR-017 §Cache key / LC7)
# ---------------------------------------------------------------------------


def _build_cache_key(lat: float, lon: float) -> str:
    """Build a deterministic SHA-256 cache key for (provider_id, endpoint, {lat4, lon4}).

    Credentials NOT in the key per LC7 — privacy/leakage concern; cache scope is
    per-location-per-provider, not per-tenant.

    Lat/lon rounded to 4 decimal places per ADR-017 §Cache key.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "aqi_current",
            "params": {
                "lat4": round(lat, 4),
                "lon4": round(lon, 4),
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sensor resolution (first-call only — module-level state)
# ---------------------------------------------------------------------------


def _fetch_locations_wire(
    *,
    lat: float,
    lon: float,
    api_key: str,
    client: ProviderHTTPClient,
) -> _OpenAQLocationsResponse:
    """Call GET /v3/locations and return parsed wire response.

    Acquires a rate-limiter slot before calling.  L2 carry-forward: canonical
    taxonomy exceptions propagate bare — not caught here.

    Raises:
        ProviderProtocolError: Response JSON validation failed.
        KeyInvalid / QuotaExhausted / TransientNetworkError: from ProviderHTTPClient.
    """
    url = OPENAQ_BASE_URL + OPENAQ_LOCATIONS_PATH
    params = {
        "coordinates": f"{round(lat, 6)},{round(lon, 6)}",
        "radius": str(_SEARCH_RADIUS_METERS),
        "limit": "10",  # Nearest 10 locations; we scan for PM2.5/PM10
    }
    headers = {"X-API-Key": api_key}

    _rate_limiter.acquire()

    # L2 carry-forward: ProviderHTTPClient.get() raises canonical taxonomy.
    # Do NOT catch and re-raise — that drops retry_after_seconds (3b-4 audit F1).
    response = client.get(url, params=params, headers=headers)

    try:
        return _OpenAQLocationsResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "OpenAQ locations response validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        raise ProviderProtocolError(
            f"OpenAQ locations response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc


def _scan_for_pm25(
    locations: list[_OpenAQLocation],
) -> tuple[_OpenAQLocation, int, int | None] | None:
    """Scan a list of locations for the first one with a PM2.5 sensor.

    Returns:
        (location, pm25_sensor_id, pm10_sensor_id_or_None) for the first
        location that has a PM2.5 sensor, or None if none found.
    """
    for location in locations:
        pm25_id: int | None = None
        pm10_id: int | None = None
        for sensor in location.sensors:
            sensor_name = sensor.name.lower().replace(".", "").replace("-", "")
            if sensor_name in ("pm25", "pm2_5", "pm2.5"):
                pm25_id = sensor.id
            elif sensor_name in ("pm10",):
                pm10_id = sensor.id
        if pm25_id is not None:
            return location, pm25_id, pm10_id
    return None


def _scan_for_pm10(
    locations: list[_OpenAQLocation],
) -> tuple[_OpenAQLocation, int] | None:
    """Scan a list of locations for the first one with a PM10 sensor.

    Returns:
        (location, pm10_sensor_id) for the first location that has a PM10
        sensor, or None if none found.
    """
    for location in locations:
        for sensor in location.sensors:
            sensor_name = sensor.name.lower().replace(".", "").replace("-", "")
            if sensor_name in ("pm10",):
                return location, sensor.id
    return None


def _resolve_sensors(
    *,
    lat: float,
    lon: float,
    api_key: str,
    client: ProviderHTTPClient,
) -> bool:
    """Find nearest PM2.5 sensor, and separately find PM10 if not co-located.

    Two-pass resolution:
      Pass 1: GET /v3/locations — scan results for the first location with
        a PM2.5 sensor.  If that same location also has PM10, PM10 is captured
        here and no second query is needed.
      Pass 2 (only if PM10 not found in pass 1): GET /v3/locations again —
        scan ALL returned locations (not just the PM2.5 one) for ANY station
        that has a PM10 sensor.  Uses the same query parameters (same result
        set in practice; second call avoids re-using a stale wire object).

    PM2.5 is mandatory — returns False if not found (caller raises
    GeographicallyUnsupported).  PM10 is optional — if not found, the
    _resolved_pm10_* state vars remain None and no error is raised.

    Sets module-level globals:
      _resolved_pm25_location_id, _resolved_pm25_location_name,
      _resolved_pm25_sensor_id, _resolved_pm10_location_id,
      _resolved_pm10_sensor_id.

    Args:
        lat: Station latitude.
        lon: Station longitude.
        api_key: OpenAQ API key (X-API-Key header).
        client: ProviderHTTPClient to use for requests.

    Returns:
        True if a PM2.5 sensor was found; False otherwise.

    Raises:
        ProviderProtocolError: Response JSON validation failed.
        KeyInvalid: 401/403 from provider (raised by ProviderHTTPClient).
        QuotaExhausted: 429 from provider (raised by ProviderHTTPClient).
        TransientNetworkError: Network failure after retries.
    """
    global _resolved_pm25_location_id, _resolved_pm25_location_name  # noqa: PLW0603
    global _resolved_pm25_sensor_id  # noqa: PLW0603
    global _resolved_pm10_location_id, _resolved_pm10_sensor_id  # noqa: PLW0603

    # Pass 1: find PM2.5 station (and PM10 if co-located).
    wire1 = _fetch_locations_wire(lat=lat, lon=lon, api_key=api_key, client=client)

    pm25_result = _scan_for_pm25(wire1.results)
    if pm25_result is None:
        logger.warning(
            "OpenAQ: no PM2.5 sensor found within %dm of lat=%s lon=%s",
            _SEARCH_RADIUS_METERS,
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        return False

    pm25_location, pm25_sensor_id, pm10_sensor_id_coloc = pm25_result
    _resolved_pm25_location_id = pm25_location.id
    _resolved_pm25_location_name = pm25_location.name
    _resolved_pm25_sensor_id = pm25_sensor_id

    if pm10_sensor_id_coloc is not None:
        # PM10 co-located on the same station — no second query needed.
        _resolved_pm10_location_id = pm25_location.id
        _resolved_pm10_sensor_id = pm10_sensor_id_coloc
        logger.info(
            "OpenAQ: resolved PM2.5 at station %r (id=%s, sensor=%s), "
            "PM10 co-located (sensor=%s) for lat=%s lon=%s",
            pm25_location.name,
            pm25_location.id,
            pm25_sensor_id,
            pm10_sensor_id_coloc,
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        return True

    # Pass 2: PM10 not co-located — search same radius for any PM10 station.
    wire2 = _fetch_locations_wire(lat=lat, lon=lon, api_key=api_key, client=client)

    pm10_result = _scan_for_pm10(wire2.results)
    if pm10_result is not None:
        pm10_location, pm10_sensor_id = pm10_result
        _resolved_pm10_location_id = pm10_location.id
        _resolved_pm10_sensor_id = pm10_sensor_id
        logger.info(
            "OpenAQ: resolved PM2.5 at station %r (id=%s, sensor=%s), "
            "PM10 at separate station %r (id=%s, sensor=%s) for lat=%s lon=%s",
            pm25_location.name,
            pm25_location.id,
            pm25_sensor_id,
            pm10_location.name,
            pm10_location.id,
            pm10_sensor_id,
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
    else:
        logger.info(
            "OpenAQ: resolved PM2.5 at station %r (id=%s, sensor=%s), "
            "PM10 not found within %dm radius for lat=%s lon=%s",
            pm25_location.name,
            pm25_location.id,
            pm25_sensor_id,
            _SEARCH_RADIUS_METERS,
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )

    return True


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _parse_datetime(dt_obj: _OpenAQDatetime | None) -> str | None:
    """Extract UTC ISO-8601 Z string from OpenAQ datetime sub-object.

    OpenAQ's datetime.utc field is already Z-form ("2026-06-22T10:00:00Z").
    We validate it's non-empty and return it directly.

    Falls back to None if both utc and local are absent or empty.
    We do NOT attempt to parse datetime.local here — ADR-020 mandates UTC Z
    form; the utc field is the authoritative field.
    """
    if dt_obj is None:
        return None
    if dt_obj.utc:
        utc_str = dt_obj.utc.strip()
        if utc_str:
            # Normalize: OpenAQ utc field is already Z-form, but validate format.
            # datetime.fromisoformat handles "2026-06-22T10:00:00Z" in Python 3.11+.
            # For Python 3.10 compatibility, strip trailing Z and re-add.
            # Simpler: if it ends in Z and looks like ISO, accept it as-is.
            if utc_str.endswith("Z") and "T" in utc_str:
                return utc_str
            # If no Z suffix (shouldn't happen for OpenAQ utc field),
            # treat as a protocol error — log and return None rather than guess.
            logger.warning(
                "OpenAQ datetime.utc field %r missing Z suffix; skipping",
                utc_str,
                extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
            )
    return None


def _wire_to_canonical(
    results: list[_OpenAQLatestResult],
    *,
    location_name: str | None,
    pm10_override: float | None = None,
) -> AQIReading | None:
    """Translate /latest results to canonical AQIReading.

    Scans results[] for pm25 and pm10 entries matching _resolved_pm25_sensor_id
    and _resolved_pm10_sensor_id.  Uses the pm25 result's datetime for observedAt.

    When pm10_override is provided (PM10 from a separate station), it takes
    precedence over any PM10 value found in results[].  This allows fetch() to
    merge values from two separate /latest calls.

    Args:
        results: /latest result entries from the PM2.5 station.
        location_name: Station name for aqiLocation (always from PM2.5 station).
        pm10_override: PM10 concentration from a separate PM10 station, or None.

    Returns:
        Canonical AQIReading or None if no PM2.5 value found.
    """
    pm25_value: float | None = None
    pm10_value_coloc: float | None = None
    observed_at: str | None = None

    for result in results:
        param_name = result.parameter.name.lower().replace(".", "").replace("-", "")
        is_pm25 = param_name in ("pm25", "pm2_5", "pm2.5")
        is_pm10 = param_name in ("pm10",)

        if is_pm25 and result.sensorsId == _resolved_pm25_sensor_id:
            if result.value is not None:
                pm25_value = result.value
            observed_at = _parse_datetime(result.datetime)

        elif (
            is_pm10
            and _resolved_pm10_sensor_id is not None
            and result.sensorsId == _resolved_pm10_sensor_id
            # Only capture co-located PM10 here; separate-station PM10 arrives
            # via pm10_override so we don't double-assign from stale sensor id.
            and _resolved_pm10_location_id == _resolved_pm25_location_id
        ):
            if result.value is not None:
                pm10_value_coloc = result.value

    # If no PM2.5 value, no useful reading.
    if pm25_value is None:
        return None

    # observedAt is required on AQIReading; if datetime was absent, log and skip.
    if observed_at is None:
        logger.warning(
            "OpenAQ: PM2.5 result has no parseable datetime; cannot build AQIReading",
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        return None

    # pm10_override (from separate station) takes precedence over co-located value.
    pm10_value = pm10_override if pm10_override is not None else pm10_value_coloc

    return AQIReading(
        aqi=None,
        aqiScale=None,
        aqiCategory=None,
        aqiMainPollutant=None,
        aqiLocation=location_name,
        pollutantPM25=pm25_value,
        pollutantPM10=pm10_value,
        pollutantO3=None,
        pollutantNO2=None,
        pollutantSO2=None,
        pollutantCO=None,
        observedAt=observed_at,
        source=PROVIDER_ID,
    )


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    api_key: str,
    http_client: ProviderHTTPClient | None = None,
) -> AQIReading | None:
    """Fetch latest PM2.5/PM10 from OpenAQ and return canonical AQIReading or None.

    Two-step flow:
      1. First call only: resolve nearest PM2.5 sensor via GET /v3/locations.
         Result cached in module-level state for process lifetime.
      2. Every call (cache miss): fetch latest via GET /v3/locations/{id}/latest.

    Cache-first: checks the response cache before making any outbound HTTP call.
    The response cache stores post-normalization AQIReading as model_dump() dict
    (JSON-serializable for Redis per ADR-017); reconstructed via model_validate().

    None return: provider responded but no useful reading available (no PM2.5
    sensor nearby, empty results, or null PM2.5 value in latest reading).

    L2 carry-forward (3b-4 audit F1): ProviderHTTPClient.get() raises canonical
    taxonomy exceptions (KeyInvalid, QuotaExhausted, TransientNetworkError,
    ProviderProtocolError) with all structured attributes set.  These propagate
    bare — do NOT re-construct.

    Args:
        lat: Station latitude (from services/station.py StationInfo).
        lon: Station longitude (from services/station.py StationInfo).
        api_key: OpenAQ API key (from env WEEWX_CLEARSKIES_OPENAQ_API_KEY).
        http_client: Optional ProviderHTTPClient override for testing.
            When None, the module-level singleton is used.

    Returns:
        Canonical AQIReading or None (no useful reading at this location).

    Raises:
        KeyInvalid: 401/403 from provider.
        QuotaExhausted: 429 from provider.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response JSON validation failed.
        GeographicallyUnsupported: No PM2.5 sensor found within search radius.
    """
    cache_key = _build_cache_key(lat, lon)
    cached = get_cache().get(cache_key)
    if cached is not None:
        logger.debug(
            "Cache hit for OpenAQ AQI",
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        if cached == {"_no_reading": True}:
            return None
        return AQIReading.model_validate(cached)

    logger.debug(
        "Cache miss for OpenAQ AQI at lat=%s lon=%s",
        round(lat, 4),
        round(lon, 4),
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )

    client = http_client or _client_for()

    # Step 1 (first call only): resolve PM2.5 and PM10 sensors.
    # Module-level state is populated once and reused for subsequent calls.
    if _resolved_pm25_location_id is None:
        found = _resolve_sensors(lat=lat, lon=lon, api_key=api_key, client=client)
        if not found:
            # No PM2.5 sensor within radius — geographic limitation.
            # Cache sentinel so re-polls within TTL don't hammer the locations endpoint.
            get_cache().set(
                cache_key,
                {"_no_reading": True},
                ttl_seconds=DEFAULT_TTL_SECONDS,
            )
            raise GeographicallyUnsupported(
                f"OpenAQ: no PM2.5 sensor found within "
                f"{_SEARCH_RADIUS_METERS}m of lat={round(lat, 4)} lon={round(lon, 4)}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            )

    # Step 2: fetch latest measurements from the PM2.5 station.
    url_pm25 = OPENAQ_BASE_URL + OPENAQ_LATEST_PATH_TMPL.format(
        location_id=_resolved_pm25_location_id,
    )
    headers = {"X-API-Key": api_key}

    _rate_limiter.acquire()

    # L2 carry-forward: client.get() raises canonical taxonomy with all
    # attributes set.  Do NOT catch and re-raise as a new canonical exception
    # (silently drops retry_after_seconds per 3b-4 audit F1 rule).
    response_pm25 = client.get(url_pm25, headers=headers)

    try:
        wire_pm25 = _OpenAQLatestResponse.model_validate(response_pm25.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "OpenAQ /latest response validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response_pm25.text,
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        raise ProviderProtocolError(
            f"OpenAQ /latest response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    if not wire_pm25.results:
        logger.info(
            "OpenAQ /latest: empty results for PM2.5 location_id=%s lat=%s lon=%s",
            _resolved_pm25_location_id,
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        get_cache().set(
            cache_key,
            {"_no_reading": True},
            ttl_seconds=DEFAULT_TTL_SECONDS,
        )
        return None

    # Step 3: if PM10 is on a DIFFERENT station, fetch its latest separately.
    pm10_override: float | None = None
    if (
        _resolved_pm10_location_id is not None
        and _resolved_pm10_location_id != _resolved_pm25_location_id
    ):
        url_pm10 = OPENAQ_BASE_URL + OPENAQ_LATEST_PATH_TMPL.format(
            location_id=_resolved_pm10_location_id,
        )
        _rate_limiter.acquire()
        # L2 carry-forward: propagate bare — no re-wrap.
        response_pm10 = client.get(url_pm10, headers=headers)
        try:
            wire_pm10 = _OpenAQLatestResponse.model_validate(response_pm10.json())
        except (ValidationError, ValueError) as exc:
            logger.error(
                "OpenAQ /latest (PM10 station) response validation failed: %s. "
                "Response body (first 2000 chars): %.2000s",
                exc,
                response_pm10.text,
                extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
            )
            raise ProviderProtocolError(
                f"OpenAQ /latest (PM10 station) response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        # Extract PM10 value from the separate station's results.
        for result in wire_pm10.results:
            param_name = result.parameter.name.lower().replace(".", "").replace("-", "")
            if (
                param_name in ("pm10",)
                and result.sensorsId == _resolved_pm10_sensor_id
                and result.value is not None
            ):
                pm10_override = result.value
                break

    record = _wire_to_canonical(
        wire_pm25.results,
        location_name=_resolved_pm25_location_name,
        pm10_override=pm10_override,
    )

    if record is None:
        # No PM2.5 value in results, or missing datetime.
        logger.info(
            "OpenAQ /latest: no usable PM2.5 reading for location_id=%s lat=%s lon=%s",
            _resolved_pm25_location_id,
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        get_cache().set(
            cache_key,
            {"_no_reading": True},
            ttl_seconds=DEFAULT_TTL_SECONDS,
        )
        return None

    get_cache().set(cache_key, record.model_dump(), ttl_seconds=DEFAULT_TTL_SECONDS)

    logger.info(
        "OpenAQ AQI fetched: pm25=%s pm10=%s aqiLocation=%r for lat=%s lon=%s",
        record.pollutantPM25,
        record.pollutantPM10,
        record.aqiLocation,
        round(lat, 4),
        round(lon, 4),
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )
    return record


# ---------------------------------------------------------------------------
# Test reset helpers
# ---------------------------------------------------------------------------


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None


def _reset_sensor_state_for_tests() -> None:
    """Reset module-level sensor resolution state. Used in tests only."""
    global _resolved_pm25_location_id, _resolved_pm25_location_name  # noqa: PLW0603
    global _resolved_pm25_sensor_id  # noqa: PLW0603
    global _resolved_pm10_location_id, _resolved_pm10_sensor_id  # noqa: PLW0603
    _resolved_pm25_location_id = None
    _resolved_pm25_location_name = None
    _resolved_pm25_sensor_id = None
    _resolved_pm10_location_id = None
    _resolved_pm10_sensor_id = None
