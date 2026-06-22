"""AirNow AQI provider module (ADR-013, ADR-038).

US EPA regulatory monitor AQI data via the AirNow Data API.

Five responsibilities per ADR-038 §2:
  1. Outbound API call — single GET per cache miss:
       GET https://www.airnowapi.org/aq/observation/latLong/current/
           ?latitude={lat}&longitude={lon}&distance={distance}
           &API_KEY={key}&format=application/json
     Returns a JSON array; each element is one pollutant's AQI reading.
     Lat/lon rounded to 6 decimal places per OWM/Aeris/IQAir precedent.
  2. Response parsing — wire-shape Pydantic models with extra="ignore":
     _AirNowCategory / _AirNowObservation (one per pollutant entry).
     ParameterName selects the dominant-AQI entry for the canonical aqi field.
     No raw-concentration fields — AirNow observation endpoint returns AQI
     index values, NOT µg/m³ concentrations (see Known Limitation below).
  3. Translation to canonical AQIReading (_wire_to_canonical):
       - Scans the response array; finds the entry with the highest AQI value
         (most significant pollutant); that entry is the canonical "dominant."
       - aqi = dominant entry's AQI integer.
       - aqiScale = "epa" (AirNow is an EPA regulatory system; 0-500 native).
       - aqiCategory: populated from the dominant entry's Category.Name field
         (AirNow supplies "Good" / "Moderate" / "Unhealthy for Sensitive Groups" /
         "Unhealthy" / "Very Unhealthy" / "Hazardous" — canonical spellings per
         canonical-data-model §3.8).
       - aqiMainPollutant: normalized from ParameterName to canonical id via
         _PARAMETERNAME_TO_CANONICAL lookup (PM2.5→"PM2.5", PM10→"PM10",
         OZONE→"O3", NO2→"NO2", CO→"CO", PM2.5-LOCAL→"PM2.5").
       - aqiLocation = data.ReportingArea (provider-supplied area name; e.g.
         "Los Angeles"). StateCode appended when present: "{area}, {state}".
       - pollutantPM25 / pollutantPM10 / pollutantO3 / pollutantNO2 /
         pollutantSO2 / pollutantCO = None.
         Known Limitation: AirNow observation endpoint returns AQI index
         values (0-500 EPA scale) for each pollutant, NOT raw concentrations
         in µg/m³ or ppm.  There is no sub_aqi_to_concentration() reverse
         function in _units.py (only the forward concentration_to_sub_aqi).
         To supply raw concentrations, a separate AirNow concentrations
         endpoint call would be required (out of scope for this round).
         The haze engine needs µg/m³; it cannot use AQI index values directly.
       - Per-pollutant AQI values (PM2.5_AQI, PM10_AQI, OZONE_AQI, etc.) are
         NOT carried into the canonical AQIReading (no field for them).
       - observedAt: built from DateObserved + HourObserved + LocalTimeZone
         using zoneinfo for IANA lookup. Falls back to UTC if TZ abbreviation
         is unresolvable (logged as WARNING). See _build_observed_at().
       - source = "airnow"
  4. Capability declaration — CAPABILITY symbol consumed at startup.
     Conservative scope: only the fields verifiably populated on the free
     observation endpoint (aqi, aqiCategory, aqiMainPollutant, observedAt,
     source). pollutantPM25/PM10 excluded (Known Limitation above).
  5. Error handling:
       - ProviderHTTPClient.get() raises canonical taxonomy with all attributes
         set. Do NOT re-construct (would drop retry_after_seconds per ADR-038
         audit F1 rule).
       - No 200-success-false envelope (AirNow returns HTTP 4xx/5xx for errors).
       - Wire-shape validation: (ValidationError, ValueError) → ProviderProtocolError
         (intentional wrap — adds wire-context the inner layer didn't have;
         per OWM/Aeris/IQAir precedent). Documented per non-obvious-provenance rule.
       - Pre-call empty/None key check → KeyInvalid (fail-fast guard).
       - Non-US coordinate check → returns None (geographic coverage is US only;
         checked against CONUS + Alaska + Hawaii bounding boxes).

AirNow is US-only (CONUS + Alaska + Hawaii).  Returns None for non-US coordinates
before making a network call.  Coverage bounding boxes per task brief.

Cache layer (ADR-017):
  TTL: 900s (15 min) per ADR-017 AQI domain.
  Key: SHA-256 of (provider_id="airnow", endpoint="aqi_current", {lat4, lon4}).
  Credential NOT in key (privacy/leakage concern; per IQAir/OWM/Aeris precedent).
  Sentinel: {"_no_reading": True} for empty-array or no-dominant-entry response.
  Reconstruction on hit: AQIReading.model_validate(cached_dict).

Rate limiter:
  max_calls=8, window_seconds=60 — conservative guard below the 500/hour free tier.
  500/hour ÷ 60 minutes ≈ 8.3/min. With 15-min TTL → ~96 calls/day, well within
  the free tier.

Known Limitation (pollutant concentrations):
  The AirNow /aq/observation/latLong/current/ endpoint returns AQI index values
  for each pollutant, not raw µg/m³ concentrations.  The haze detection engine
  needs µg/m³ for PM2.5/PM10.  To obtain them, one would need to:
    a) Call the AirNow HourlyData endpoint for raw concentration data, OR
    b) Implement a reverse AQI-to-concentration function using the EPA breakpoint
       table in _units.py's _EPA_BREAKPOINTS (the forward direction already
       exists as concentration_to_sub_aqi; the reverse is mathematically well-
       defined via the same piecewise-linear interpolation in reverse).
  This round populates pollutantPM25/PM10 as None. A future round can add the
  reverse function to _units.py and call it here.
"""

# ruff: noqa: N815  (wire field names include UpperCamelCase from AirNow)

from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, ConfigDict, ValidationError

from weewx_clearskies_api.models.responses import AQIReading
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import ProviderCapability
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

PROVIDER_ID = "airnow"
DOMAIN = "aqi"
DEFAULT_AQI_TTL_SECONDS = 900  # 15 min per ADR-017 AQI domain
_API_VERSION = "0.1.0"

AIRNOW_BASE_URL = "https://www.airnowapi.org"
AIRNOW_OBSERVATION_PATH = "/aq/observation/latLong/current/"

# Default search radius in miles for the AirNow nearest-station query.
AIRNOW_DEFAULT_DISTANCE_MILES = 25

# AirNow ParameterName → canonical pollutant id.
# Confirmed values from AirNow API documentation and published examples.
# "PM2.5-LOCAL" and "PM2.5_LC" are non-FRM/FEM PM2.5 sensors at some sites.
# OZONE maps to canonical "O3".
# Unmappable codes → None for aqiMainPollutant + logger.info (mirrors IQAir LC3).
_PARAMETERNAME_TO_CANONICAL: dict[str, str] = {
    "PM2.5": "PM2.5",
    "PM2.5-LOCAL": "PM2.5",
    "PM2.5_LC": "PM2.5",
    "PM10": "PM10",
    "OZONE": "O3",
    "O3": "O3",
    "NO2": "NO2",
    "SO2": "SO2",
    "CO": "CO",
}

# US geographic bounding boxes for coverage check.
# Coordinates are approximate; used to short-circuit non-US requests before
# making a network call.  Source: task brief + FAA aeronautical chart bounds.
# Structure: list of (lat_min, lat_max, lon_min, lon_max) tuples.
_US_BOUNDING_BOXES: list[tuple[float, float, float, float]] = [
    # CONUS
    (24.4, 49.4, -125.0, -66.9),
    # Alaska
    (51.2, 71.4, -179.1, -129.9),
    # Hawaii
    (18.9, 22.2, -160.2, -154.8),
]

# AirNow timezone abbreviation → IANA timezone name.
# AirNow returns abbreviated timezone strings (e.g. "PST", "EDT", "CST").
# zoneinfo requires IANA names; this lookup table covers all US timezone
# abbreviations that AirNow documentation references.
# Standard time (ST) and Daylight time (DT) abbreviations are both included.
# AirNow API appears to return standard-time abbreviations regardless of DST
# (per community reports); we handle both just in case.
_AIRNOW_TZ_TO_IANA: dict[str, str] = {
    # Eastern
    "EST": "America/New_York",
    "EDT": "America/New_York",
    # Central
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    # Mountain
    "MST": "America/Denver",
    "MDT": "America/Denver",
    # Mountain (no-DST — Arizona, except Navajo Nation)
    "MST7": "America/Phoenix",
    # Pacific
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    # Alaska
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    # Hawaii — Hawaii does not observe DST
    "HST": "Pacific/Honolulu",
    # UTC / GMT (sometimes returned for offshore / federal sites)
    "UTC": "UTC",
    "GMT": "UTC",
}

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        # Conservative scope: only fields verifiably populated from the free
        # observation endpoint. pollutantPM25/PM10 excluded — see Known
        # Limitation in module docstring.
        "aqi", "aqiCategory", "aqiMainPollutant",
        "observedAt", "source",
    ),
    geographic_coverage="us",
    auth_required=("api_key",),
    default_poll_interval_seconds=DEFAULT_AQI_TTL_SECONDS,
    operator_notes=(
        "AirNow Data API /aq/observation/latLong/current/ endpoint (free tier). "
        "Auth: query-param API_KEY= (uppercase, per AirNow docs). "
        "Env var: WEEWX_CLEARSKIES_AIRNOW_API_KEY. "
        "Coverage: US only (CONUS + Alaska + Hawaii). Returns null for non-US coordinates. "
        "Rate limiter: 8/min (window_seconds=60) — conservative guard below 500/hour free tier. "
        "With 15-min TTL → ~96 calls/day, well within free tier. "
        "Response is a JSON array; each element is one pollutant parameter. "
        "Dominant pollutant = highest AQI value in the array. "
        "aqiScale = 'epa' (AirNow returns EPA 0-500 AQI index values natively). "
        "aqiCategory populated from wire Category.Name field "
        "(canonical spellings: Good / Moderate / Unhealthy for Sensitive Groups / "
        "Unhealthy / Very Unhealthy / Hazardous). "
        "aqiMainPollutant from ParameterName via _PARAMETERNAME_TO_CANONICAL lookup "
        "(PM2.5/PM10/OZONE/NO2/SO2/CO confirmed; PM2.5-LOCAL/PM2.5_LC variants included). "
        "Unmappable ParameterName codes → None + logger.info notice. "
        "pollutantPM25/PM10/O3/NO2/SO2/CO = None — Known Limitation: AirNow observation "
        "endpoint returns AQI index values, not raw µg/m³ concentrations. "
        "A future round can add a reverse AQI-to-concentration function to _units.py "
        "and populate these fields. "
        "observedAt built from DateObserved + HourObserved + LocalTimeZone using zoneinfo. "
        "No 200-success-false envelope; errors are HTTP 4xx/5xx. "
        "is_observed_source=True (EPA regulatory monitor data). "
        "Free API key registration: https://docs.airnowapi.org/"
    ),
    is_observed_source=True,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (extra="ignore"; required fields enumerated)
# Source: https://docs.airnowapi.org/aq101 + AirNow API documentation
# ---------------------------------------------------------------------------


class _AirNowCategory(BaseModel):
    """Category sub-object within an AirNow observation array element.

    Number: 1=Good, 2=Moderate, 3=Unhealthy for Sensitive Groups,
            4=Unhealthy, 5=Very Unhealthy, 6=Hazardous.
    Name: Human-readable category string; canonical spellings per AirNow docs.
    extra="ignore" drops any future AirNow additions.
    """

    model_config = ConfigDict(extra="ignore")

    Number: int | None = None
    Name: str | None = None


class _AirNowObservation(BaseModel):
    """One pollutant observation entry from the AirNow response array.

    AirNow returns one element per pollutant measured at the nearest reporting
    area. Fields are UpperCamelCase per AirNow wire shape.
    extra="ignore" drops any future AirNow additions.
    """

    model_config = ConfigDict(extra="ignore")

    DateObserved: str | None = None   # "YYYY-MM-DD" station-local date
    HourObserved: int | None = None   # local hour (0-23)
    LocalTimeZone: str | None = None  # e.g. "PST", "EDT"
    ReportingArea: str | None = None  # e.g. "Los Angeles"
    StateCode: str | None = None      # e.g. "CA"
    Latitude: float | None = None
    Longitude: float | None = None
    ParameterName: str | None = None  # "PM2.5", "PM10", "OZONE", "NO2", "SO2", "CO"
    AQI: int | None = None            # EPA AQI 0-500 for this pollutant
    Category: _AirNowCategory | None = None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="airnow-aqi",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=8,
    window_seconds=60,  # conservative guard below 500/hour free tier
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
# Coverage check (US bounding boxes)
# ---------------------------------------------------------------------------


def _is_us_location(lat: float, lon: float) -> bool:
    """Return True if (lat, lon) falls within a US bounding box.

    Checks CONUS, Alaska, and Hawaii bounding boxes from _US_BOUNDING_BOXES.
    Used to short-circuit non-US requests before making a network call.
    AirNow only covers US EPA monitoring sites; non-US coordinates return no
    useful data (and would waste the operator's quota).

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        True if inside any US bounding box; False otherwise.
    """
    for lat_min, lat_max, lon_min, lon_max in _US_BOUNDING_BOXES:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return True
    return False


# ---------------------------------------------------------------------------
# Cache key construction (ADR-017)
# ---------------------------------------------------------------------------


def _build_cache_key(lat: float, lon: float) -> str:
    """Build a deterministic SHA-256 cache key for (provider_id, endpoint, {lat4, lon4}).

    Credentials NOT in the key per IQAir/OWM/Aeris precedent — privacy/leakage
    concern; cache scope is per-location-per-provider, not per-tenant.

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
# observedAt construction from AirNow date/hour/timezone fields
# ---------------------------------------------------------------------------


def _build_observed_at(
    date_observed: str,
    hour_observed: int,
    local_tz: str,
    *,
    provider_id: str,
    domain: str,
) -> str:
    """Build UTC ISO-8601 observedAt from AirNow date/hour/timezone fields.

    AirNow does not supply a combined ISO-8601 timestamp; instead it provides:
      DateObserved: "YYYY-MM-DD" (station-local date)
      HourObserved: 0-23 integer (station-local hour, on the hour)
      LocalTimeZone: timezone abbreviation (e.g. "PST", "EDT")

    This function:
      1. Looks up the IANA timezone name via _AIRNOW_TZ_TO_IANA.
      2. Constructs a timezone-aware datetime using zoneinfo.ZoneInfo.
      3. Converts to UTC and returns in ISO-8601 Z form per ADR-020.

    Fallback: if the timezone abbreviation is not in _AIRNOW_TZ_TO_IANA,
    the datetime is treated as UTC (conservative; logs a WARNING so the
    operator knows the offset was not applied). This avoids a hard failure
    from an unexpected TZ abbreviation.

    Args:
        date_observed: "YYYY-MM-DD" station-local date string.
        hour_observed: Station-local hour (0-23).
        local_tz: Timezone abbreviation from AirNow wire (e.g. "PST").
        provider_id: Provider identifier for error context.
        domain: Domain identifier for error context.

    Returns:
        UTC ISO-8601 Z string, e.g. "2026-06-21T22:00:00Z".

    Raises:
        ProviderProtocolError: date_observed cannot be parsed as YYYY-MM-DD,
            or hour_observed is out of 0-23 range.
    """
    from datetime import UTC, datetime

    # Validate date format: YYYY-MM-DD
    try:
        year, month, day = (int(p) for p in date_observed.split("-"))
    except (ValueError, AttributeError) as exc:
        raise ProviderProtocolError(
            f"AirNow DateObserved parse failed for {date_observed!r}: {exc}",
            provider_id=provider_id,
            domain=domain,
        ) from exc

    # Validate hour range
    if not 0 <= hour_observed <= 23:
        raise ProviderProtocolError(
            f"AirNow HourObserved {hour_observed!r} out of range 0-23",
            provider_id=provider_id,
            domain=domain,
        )

    # Resolve timezone
    iana_name = _AIRNOW_TZ_TO_IANA.get(local_tz.upper() if local_tz else "")
    if iana_name is None:
        logger.warning(
            "AirNow LocalTimeZone %r not in _AIRNOW_TZ_TO_IANA; "
            "treating as UTC. Add to lookup table if confirmed real-capture.",
            local_tz,
            extra={"provider_id": provider_id, "domain": domain},
        )
        iana_name = "UTC"

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(iana_name)
        dt_local = datetime(year, month, day, hour_observed, 0, 0, tzinfo=tz)
    except Exception as exc:  # noqa: BLE001
        # zoneinfo.ZoneInfoNotFoundError or datetime construction error
        raise ProviderProtocolError(
            f"AirNow timestamp construction failed for "
            f"date={date_observed!r} hour={hour_observed!r} tz={local_tz!r}: {exc}",
            provider_id=provider_id,
            domain=domain,
        ) from exc

    return dt_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _wire_to_canonical(observations: list[_AirNowObservation]) -> AQIReading | None:
    """Translate a list of AirNow observations to canonical AQIReading.

    AirNow returns one observation per pollutant. The canonical AQIReading
    needs a single dominant AQI value. Selection strategy: the observation
    with the highest AQI value is the dominant pollutant (most significant
    air quality concern).

    Returns:
        Canonical AQIReading or None if no observations have a valid AQI.
    """
    # Filter to observations with a non-None, non-negative AQI value.
    valid = [obs for obs in observations if obs.AQI is not None and obs.AQI >= 0]
    if not valid:
        return None

    # Select the dominant observation: highest AQI value.
    # When two pollutants tie, the first one in the array wins (AirNow
    # ordering is typically PM2.5 first, which is reasonable as the default).
    dominant = max(valid, key=lambda obs: obs.AQI)  # type: ignore[arg-type]

    aqi_val = dominant.AQI  # int; not None (filtered above)

    # aqiMainPollutant: normalize ParameterName to canonical id.
    # Unmappable codes → None + logger.info (mirrors IQAir LC3 pattern).
    main_pollutant: str | None = None
    if dominant.ParameterName:
        param_upper = dominant.ParameterName.upper().strip()
        main_pollutant = _PARAMETERNAME_TO_CANONICAL.get(param_upper)
        if main_pollutant is None:
            logger.info(
                "AirNow ParameterName %r not in _PARAMETERNAME_TO_CANONICAL; "
                "aqiMainPollutant=None. Add to lookup table if confirmed by real-capture.",
                dominant.ParameterName,
                extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
            )

    # aqiCategory: from the dominant entry's Category.Name (AirNow supplies
    # canonical EPA category strings directly).
    aqi_category: str | None = None
    if dominant.Category and dominant.Category.Name:
        aqi_category = dominant.Category.Name.strip() or None

    # aqiLocation: "{ReportingArea}, {StateCode}" when both present.
    # Falls back to ReportingArea-only if StateCode absent.
    aqi_location: str | None = None
    if dominant.ReportingArea:
        if dominant.StateCode:
            aqi_location = f"{dominant.ReportingArea}, {dominant.StateCode}"
        else:
            aqi_location = dominant.ReportingArea

    # observedAt: build from date/hour/timezone fields on the dominant entry.
    # AirNow does not supply a combined ISO-8601 timestamp.
    observed_at: str
    if (
        dominant.DateObserved is not None
        and dominant.HourObserved is not None
        and dominant.LocalTimeZone is not None
    ):
        observed_at = _build_observed_at(
            dominant.DateObserved,
            dominant.HourObserved,
            dominant.LocalTimeZone,
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )
    else:
        # Fields missing — this is a protocol violation; raise rather than silently
        # returning a sentinel because observedAt is a required field per OpenAPI.
        raise ProviderProtocolError(
            "AirNow dominant observation missing DateObserved/HourObserved/"
            f"LocalTimeZone fields: {dominant!r}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    # pollutantPM25/PM10/O3/NO2/SO2/CO: all None — Known Limitation.
    # AirNow observation endpoint returns AQI index values, not raw µg/m³.
    # See module docstring for the forward path to populate these in a future round.

    return AQIReading(
        aqi=float(aqi_val),
        aqiScale="epa",          # AirNow is EPA regulatory; 0-500 native scale
        aqiCategory=aqi_category,
        aqiMainPollutant=main_pollutant,
        aqiLocation=aqi_location,
        pollutantPM25=None,      # Known Limitation — AQI index only, not µg/m³
        pollutantPM10=None,      # Known Limitation — AQI index only, not µg/m³
        pollutantO3=None,        # Known Limitation — AQI index only, not ppm
        pollutantNO2=None,       # Known Limitation — AQI index only, not ppm
        pollutantSO2=None,       # Known Limitation — AQI index only, not ppm
        pollutantCO=None,        # Known Limitation — AQI index only, not ppm
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
    api_key: str | None,
    http_client: ProviderHTTPClient | None = None,
    distance_miles: int = AIRNOW_DEFAULT_DISTANCE_MILES,
) -> AQIReading | None:
    """GET /aq/observation/latLong/current/ and return canonical AQIReading.

    Cache-first: checks the cache before making an outbound HTTP call.
    Cache stores post-normalization AQIReading as model_dump() dict (JSON-
    serializable for Redis per ADR-017); reconstructed via model_validate() on hit.

    Coverage check: returns None immediately for non-US coordinates
    (CONUS / Alaska / Hawaii bounding boxes).

    None return: provider responded but no useful reading available
    (empty array or no valid AQI entries).

    L2 carry-forward (ADR-038 §3 / audit F1): ProviderHTTPClient.get() raises
    canonical taxonomy exceptions (KeyInvalid, QuotaExhausted,
    TransientNetworkError, ProviderProtocolError) with all structured attributes
    set (status_code, retry_after_seconds).  These propagate bare — do NOT
    re-construct (re-wrapping drops attributes per ADR rule).

    Wire-shape validation wrap (ValidationError, ValueError) → ProviderProtocolError
    IS an intentional wrap: adds wire-context the inner layer didn't have.
    Per OWM/Aeris/IQAir precedent. Documented per non-obvious-provenance rule.

    Args:
        lat: Station latitude (from services/station.py StationInfo).
        lon: Station longitude (from services/station.py StationInfo).
        api_key: AirNow API key (from settings.aqi.airnow_api_key).
        http_client: Optional ProviderHTTPClient override for testing.
            When None, the module-level singleton is used.
        distance_miles: Search radius in miles (default 25). Passed as the
            AirNow `distance` query parameter.

    Returns:
        Canonical AQIReading or None (non-US location, empty array, or no
        valid AQI entries).

    Raises:
        KeyInvalid: api_key is empty/None (pre-call guard), OR provider
            returned 401/403.
        QuotaExhausted: Provider returned 429.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response JSON validation failed, or unexpected
            response shape.
    """
    # Pre-call guard: fail fast on missing credential.
    # Raise KeyInvalid before hitting the network rather than getting a cryptic
    # 401 from AirNow. Mirrors IQAir LC13 / OWM appid guard pattern.
    if not api_key:
        raise KeyInvalid(
            "AirNow api_key is empty or None — set WEEWX_CLEARSKIES_AIRNOW_API_KEY env var",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    # Coverage check: AirNow is US-only. Return None for non-US coordinates
    # before making a network call (saves quota; avoids an empty response).
    if not _is_us_location(lat, lon):
        logger.debug(
            "AirNow AQI: lat=%s lon=%s outside US bounding boxes; skipping",
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        return None

    cache_key = _build_cache_key(lat, lon)
    cached = get_cache().get(cache_key)
    if cached is not None:
        logger.debug(
            "Cache hit for AirNow AQI",
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        if cached == {"_no_reading": True}:
            return None
        return AQIReading.model_validate(cached)

    logger.debug(
        "Cache miss for AirNow AQI; calling %s",
        AIRNOW_BASE_URL + AIRNOW_OBSERVATION_PATH,
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )

    # api_key in params dict (not URL path) — avoids logging credentials at
    # INFO level if the URL is logged (security baseline §3.4 / IQAir LC14).
    # Lat/lon rounded to 6 decimal places per OWM/Aeris/IQAir precedent.
    params = {
        "latitude": str(round(lat, 6)),
        "longitude": str(round(lon, 6)),
        "distance": str(distance_miles),
        "API_KEY": api_key,
        "format": "application/json",
    }

    client = http_client or _client_for()
    _rate_limiter.acquire()

    # L2 carry-forward: client.get() raises canonical taxonomy with all
    # attributes set. Do NOT catch and re-raise as a new canonical exception
    # (would silently drop retry_after_seconds per ADR-038 audit F1 rule).
    response = client.get(AIRNOW_BASE_URL + AIRNOW_OBSERVATION_PATH, params=params)

    # Parse raw JSON.
    try:
        raw_json = response.json()
    except ValueError as exc:
        logger.error(
            "AirNow AQI response is not valid JSON: %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        raise ProviderProtocolError(
            f"AirNow AQI response is not valid JSON: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    # AirNow returns a JSON array. Guard against unexpected root type.
    if not isinstance(raw_json, list):
        logger.error(
            "AirNow AQI response is not a JSON array (got %s). "
            "Response body (first 2000 chars): %.2000s",
            type(raw_json).__name__,
            response.text,
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        raise ProviderProtocolError(
            f"AirNow AQI response is not a JSON array (got {type(raw_json).__name__})",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    # Empty array: no monitoring stations within the search radius.
    if len(raw_json) == 0:
        logger.info(
            "AirNow AQI: empty response array for lat=%s lon=%s (distance=%s miles); "
            "no monitoring stations in range",
            round(lat, 4),
            round(lon, 4),
            distance_miles,
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        get_cache().set(
            cache_key,
            {"_no_reading": True},
            ttl_seconds=DEFAULT_AQI_TTL_SECONDS,
        )
        return None

    # Wire-shape validation: intentional (ValidationError, ValueError) →
    # ProviderProtocolError wrap. Adds wire-context the inner layer didn't have.
    # Per OWM/Aeris/IQAir precedent. Documented per non-obvious-provenance rule.
    try:
        observations = [_AirNowObservation.model_validate(item) for item in raw_json]
    except (ValidationError, ValueError) as exc:
        logger.error(
            "AirNow AQI response validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        raise ProviderProtocolError(
            f"AirNow AQI response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    record = _wire_to_canonical(observations)

    if record is None:
        # No valid AQI entries in the array.
        logger.info(
            "AirNow AQI: no valid AQI entries in response for lat=%s lon=%s",
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        get_cache().set(
            cache_key,
            {"_no_reading": True},
            ttl_seconds=DEFAULT_AQI_TTL_SECONDS,
        )
        return None

    get_cache().set(cache_key, record.model_dump(), ttl_seconds=DEFAULT_AQI_TTL_SECONDS)

    logger.info(
        "AirNow AQI fetched: aqi=%s mainPollutant=%s aqiLocation=%r for lat=%s lon=%s",
        record.aqi,
        record.aqiMainPollutant,
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
