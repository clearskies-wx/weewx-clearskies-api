"""NWS Surf Zone Forecast (SRF) text product provider module (ADR-083,
PROVIDER-MANUAL §14.5).

Five responsibilities per the §1 Module Contract (PROVIDER-MANUAL):
  1. Outbound API call — NWS /products/types/SRF/locations/{wfo} (+ detail
     fetch of the most recent product's full text).
  2. Response parsing — free-text SRF product parsing (day-period markers,
     per-county-zone sections, labeled fields) via regex.
  3. Translation to canonical SurfZoneForecast (models/responses.py).
  4. Capability declaration — CAPABILITY symbol consumed at startup.
  5. Error handling — provider errors translated to canonical taxonomy.

WFO determination (PROVIDER-MANUAL §14.5, §14.8):
  Reuses `get_cwa()` from the shared zone discovery utility at
  providers/_common/nws_zones.py — the same /points → cwa lookup used by
  providers/marine/nws_marine.py.

County-zone determination (PROVIDER-MANUAL §14.5, lead-approved design
2026-07-09 — self-contained in this module, nws_zones.py NOT extended):
  The SRF text is per coastal county and carries zone *names* in its
  section headers (e.g. "CARTERET COUNTY BEACHES..."), not zone *IDs*.
  To match the spot's coordinates to the right section:
    1. GET /points/{lat},{lon} -> properties.forecastZone (a URL like
       ".../zones/forecast/NCZ027") -> the public forecast zone ID.
       This is a SEPARATE /points call from get_cwa()'s — get_cwa()'s wire
       model (providers/_common/nws_zones.py::_NwsPointCwaResponse) only
       extracts `cwa` and ignores `forecastZone` (extra="ignore"), so this
       module cannot piggyback on that call's parsed result. The lead
       confirmed (2026-07-09) this deliberate duplicate call is acceptable
       for a non-hot-path fetch and instructed a 3600s cache TTL for it
       (see _ZONE_LOOKUP_TTL_SECONDS below) rather than nws_zones.py's 24h.
    2. GET /zones/forecast/{zoneId} -> properties.name -> the zone's
       human-readable name, used to fuzzy-match against SRF text headers
       (see _zone_header_matches).
  Callers that already know the zone ID (e.g. from operator config,
  mirroring the nws_marine_zone_id pattern) may pass `county_zone`
  directly to fetch() to skip step 1.

NWS User-Agent (ADR-006):
  Same construction pattern as providers/marine/nws_marine.py and
  providers/_common/nws_zones.py: "(weewx-clearskies-api/<version>,
  <contact>)" when configured, "(weewx-clearskies-api/<version>)" +
  one-time WARN when not. No project-level hardcoded fallback (ADR-006).

Wire format (PROVIDER-MANUAL §14.5):
  GET https://api.weather.gov/products/types/SRF/locations/{wfo} returns a
  JSON-LD envelope with an `@graph` array of product stubs (most recent
  first in practice; this module does not assume ordering and always
  treats `@graph[0]` as "most recent" per the brief). Each stub carries an
  `@id` URL (preferred) or an `id` (UUID, used to construct the URL as a
  fallback: f"{NWS_BASE_URL}/products/{id}"). Fetching that URL returns the
  full product record, including `productText` — the raw SRF text.

SRF text parsing:
  The SRF is a free-text product with day-period markers (".TODAY...",
  ".TONIGHT...", ".TOMORROW...", etc.), each containing one or more
  per-county-zone sections. A zone section is identified by a line that
  ends in "..." with nothing following on that line (field lines like
  "SURF HEIGHT...2 TO 4 FEET." always have trailing content after the
  "...", which is what distinguishes them from zone-header lines).
  Recognized period labels are mapped to a day offset from the product's
  `issuanceTime`; only TODAY/TONIGHT/THIS AFTERNOON/THIS MORNING (offset 0)
  and TOMORROW/TOMORROW NIGHT (offset 1) are recognized — an unrecognized
  period label is logged at WARNING and that block is skipped (partial
  result), per PROVIDER-MANUAL §14.5's "text parsing failure -> log
  WARNING ... return partial result" instruction. Likewise, a zone section
  missing a parseable RIP CURRENT RISK (the one non-optional canonical
  field on SurfZoneForecast) is logged at WARNING and skipped; all other
  fields are optional on the canonical model and are simply None when not
  parseable.

  Compound rip-current-risk values (e.g. "MODERATE TO HIGH", which NWS
  forecasters use when risk increases through the period) resolve to the
  higher-risk category — weather data is safety-critical (rules/coding.md
  §1), so surfacing the worse case is the safer default over averaging or
  taking the first token.

Cache (ADR-017):
  Key: SHA-256 of {"provider_id": "nws_srf", "wfo": wfo, "county_zone":
  zone_id} — per the brief's explicit key shape (mirrors nws_marine.py's
  zone_id-only key style). TTL: 3600s (60 min) per PROVIDER-MANUAL §14.5
  ("SRF is issued 1-2 times/day"). The two zone-lookup helper calls
  (/points forecastZone, /zones/forecast/{zoneId} name) use their own
  cache keys with a 3600s TTL per lead instruction (2026-07-09) — shorter
  than nws_zones.py's 24h zone-geometry cache since this is a much simpler
  lookup and the module doesn't need the aggressive TTL that discovery-time
  geometry fetches use.

Error handling (PROVIDER-MANUAL §14.5):
  WFO with no SRF product (empty `@graph`, or 404 on the products-list or
  product-detail call) -> empty result, NOT an error: returns
  {"forecasts": [], "wfo": wfo}. Text parsing failure (unrecognized period
  label, or a zone section missing RIP CURRENT RISK) -> log WARNING with
  the raw block, skip that entry, keep parsing the rest (partial result).
  Any other provider error (rate limit, 5xx, schema violation elsewhere)
  propagates via the canonical taxonomy — canonical exceptions from
  ProviderHTTPClient/get_cwa() are never re-wrapped here (rules/coding.md
  §1 "Dispatch on exception state via attributes, not message strings").

Rate limiting: dedicated `RateLimiter(name="nws-srf", ...)` instance,
matching the established per-module-limiter pattern (see
providers/marine/nws_marine.py's rate-limiter section).

ruff: noqa: N815  (canonical camelCase fields match SurfZoneForecast, e.g.
ripCurrentRisk, surfHeightMin/Max, uvIndex, waterTemp, windText,
hazardsText.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import SurfZoneForecast
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.errors import ProviderProtocolError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.nws_zones import get_cwa
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter
from weewx_clearskies_api.units.conversion import convert

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "nws_srf"
DOMAIN = "marine"
NWS_BASE_URL = "https://api.weather.gov"
_CACHE_TTL_SECONDS = 3600  # 60 min per PROVIDER-MANUAL §14.5
_ZONE_LOOKUP_TTL_SECONDS = 3600  # lead instruction 2026-07-09 (see module docstring)
_API_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Capability declaration (§1 Module Contract)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        "ripCurrentRisk",
        "surfHeightMin",
        "surfHeightMax",
        "uvIndex",
        "waterTemp",
        "windText",
        "hazardsText",
    ),
    geographic_coverage="us_coastal",
    auth_required=(),
    default_poll_interval_seconds=_CACHE_TTL_SECONDS,
    operator_notes=(
        "NWS Surf Zone Forecast (SRF) text product. No API key required. "
        "US coastal coverage; not all WFOs issue SRF products (empty result, "
        "not an error, when the covering WFO doesn't issue one). WFO is "
        "determined via the shared NWS zone discovery utility "
        "(PROVIDER-MANUAL §14.8)."
    ),
    refresh_interval=_CACHE_TTL_SECONDS,
    attribution=ProviderAttribution(
        attribution_required=False,
        display_name="National Weather Service",
        attribution_text="Data courtesy of the National Weather Service",
        text_prefix="Data courtesy of",
        text_provider_name="the National Weather Service",
        url="https://www.weather.gov/",
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter (separate instance — see module docstring)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="nws-srf",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# ---------------------------------------------------------------------------


class _NwsPointForecastZoneProperties(BaseModel):
    """Minimal /points/{lat,lon} properties — only forecastZone is needed here.

    Deliberately separate from providers/_common/nws_zones.py's
    _NwsPointCwaProperties, which only extracts `cwa` (extra="ignore" drops
    forecastZone there). See module docstring "County-zone determination".
    """

    model_config = ConfigDict(extra="ignore")

    forecastZone: str | None = None


class _NwsPointForecastZoneResponse(BaseModel):
    """NWS /points/{lat,lon} GeoJSON Feature envelope (forecastZone-only wire shape)."""

    model_config = ConfigDict(extra="ignore")

    properties: _NwsPointForecastZoneProperties


class _NwsForecastZoneProperties(BaseModel):
    """Properties of a /zones/forecast/{zoneId} detail response."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str


class _NwsForecastZoneResponse(BaseModel):
    """NWS /zones/forecast/{zoneId} GeoJSON Feature envelope."""

    model_config = ConfigDict(extra="ignore")

    properties: _NwsForecastZoneProperties


class _NwsProductStub(BaseModel):
    """One product stub from /products/types/SRF/locations/{wfo}.

    `id` is always present (UUID). `@id` (aliased to `at_id`) is the full
    URL to the product detail resource when NWS supplies it; when absent,
    fetch() falls back to constructing the URL from `id`.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    at_id: str | None = Field(default=None, alias="@id")


class _NwsProductListResponse(BaseModel):
    """NWS /products/types/SRF/locations/{wfo} JSON-LD envelope."""

    model_config = ConfigDict(extra="ignore")

    graph: list[_NwsProductStub] = Field(default_factory=list, alias="@graph")


class _NwsProductDetail(BaseModel):
    """NWS /products/{id} JSON-LD product record — only the fields this module needs."""

    model_config = ConfigDict(extra="ignore")

    id: str
    issuanceTime: str
    productText: str


# ---------------------------------------------------------------------------
# HTTP client (module-level singleton — one per module, not per request)
# ---------------------------------------------------------------------------

_http_client: ProviderHTTPClient | None = None
_http_client_ua: str = ""


def _get_http_client(user_agent: str) -> ProviderHTTPClient:
    """Return the module-level HTTP client, (re-)constructing if UA changed."""
    global _http_client, _http_client_ua  # noqa: PLW0603
    if _http_client is None or _http_client_ua != user_agent:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent=user_agent,
        )
        _http_client_ua = user_agent
    return _http_client


# ---------------------------------------------------------------------------
# User-Agent construction (ADR-006)
# ---------------------------------------------------------------------------


def _build_user_agent(contact: str | None) -> str:
    """Build the NWS User-Agent string per ADR-006.

    NO project-level hardcoded fallback — would put the project on the hook
    for operator traffic patterns (ADR-006).
    """
    base = f"weewx-clearskies-api/{_API_VERSION}"
    if contact and contact.strip():
        return f"({base}, {contact.strip()})"
    return f"({base})"


_warned_missing_contact = False


def _warn_once_missing_contact() -> None:
    global _warned_missing_contact  # noqa: PLW0603
    if not _warned_missing_contact:
        logger.warning(
            "NWS SRF User-Agent contact is not set. "
            "Set [marine] nws_user_agent_contact = <email-or-url> in api.conf "
            "to reduce the risk of being blocked during NWS security events. "
            "See ADR-006 for the operator-managed compliance model."
        )
        _warned_missing_contact = True


# ---------------------------------------------------------------------------
# Cache key construction (ADR-017)
# ---------------------------------------------------------------------------


def _build_points_cache_key(lat: float, lon: float) -> str:
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "points_forecast_zone",
            "params": {"lat4": round(lat, 4), "lon4": round(lon, 4)},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_zone_name_cache_key(zone_id: str) -> str:
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "forecast_zone_name",
            "params": {"zone_id": zone_id},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_srf_cache_key(wfo: str, county_zone: str) -> str:
    """Brief's explicit key shape: (provider_id, wfo, county_zone), no wrapping envelope."""
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "wfo": wfo, "county_zone": county_zone},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# County-zone resolution (module docstring "County-zone determination")
# ---------------------------------------------------------------------------


def _resolve_forecast_zone_id(lat: float, lon: float, user_agent_contact: str | None) -> str:
    """Resolve the NWS public forecast zone ID covering (lat, lon).

    GET /points/{lat},{lon} -> properties.forecastZone (a URL like
    "https://api.weather.gov/zones/forecast/NCZ027") -> zone ID is the
    final path segment.

    Raises:
        ProviderProtocolError: response validation failed, or the response
            omitted properties.forecastZone (unexpected NWS schema change
            for this coordinate — every US coastal point has one).
    """
    cache_key = _build_points_cache_key(lat, lon)
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cast(str, cached)

    _rate_limiter.acquire()
    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    lat4 = round(lat, 4)
    lon4 = round(lon, 4)
    url = f"{NWS_BASE_URL}/points/{lat4},{lon4}"
    response = client.get(url, headers={"Accept": "application/geo+json"})

    try:
        wire = _NwsPointForecastZoneResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS /points response validation failed (SRF zone resolution) "
            "for lat=%s,lon=%s: %s. Response body (first 2000 chars): %.2000s",
            lat4,
            lon4,
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"NWS /points response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    forecast_zone_url = wire.properties.forecastZone
    if not forecast_zone_url:
        raise ProviderProtocolError(
            f"NWS /points response for lat={lat4},lon={lon4} did not include "
            "properties.forecastZone; cannot resolve county zone for SRF matching.",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    zone_id = forecast_zone_url.rstrip("/").rsplit("/", 1)[-1]
    get_cache().set(cache_key, zone_id, ttl_seconds=_ZONE_LOOKUP_TTL_SECONDS)
    return zone_id


def _resolve_zone_name(zone_id: str, user_agent_contact: str | None) -> str:
    """Resolve the human-readable name for a public forecast zone ID.

    GET /zones/forecast/{zoneId} -> properties.name. The SRF text carries
    zone *names* in its section headers, not zone IDs, so this name is what
    gets fuzzy-matched against the parsed text (see _zone_header_matches).

    Raises:
        ProviderProtocolError: response validation failed.
    """
    cache_key = _build_zone_name_cache_key(zone_id)
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cast(str, cached)

    _rate_limiter.acquire()
    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    url = f"{NWS_BASE_URL}/zones/forecast/{zone_id}"
    response = client.get(url, headers={"Accept": "application/geo+json"})

    try:
        wire = _NwsForecastZoneResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS /zones/forecast/%s response validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            zone_id,
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"NWS /zones/forecast/{zone_id} response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    zone_name = wire.properties.name
    get_cache().set(cache_key, zone_name, ttl_seconds=_ZONE_LOOKUP_TTL_SECONDS)
    return zone_name


# ---------------------------------------------------------------------------
# SRF product list + detail fetch
# ---------------------------------------------------------------------------


def _fetch_latest_srf_product(
    wfo: str, user_agent_contact: str | None
) -> _NwsProductDetail | None:
    """Fetch the most recent SRF product for `wfo`.

    Returns None (not an error) when the WFO issues no SRF product — either
    an empty `@graph` on the list call, or a 404 on the list or detail call
    (PROVIDER-MANUAL §14.5: "not all WFOs issue SRF").
    """
    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    _rate_limiter.acquire()
    list_url = f"{NWS_BASE_URL}/products/types/SRF/locations/{wfo}"
    try:
        list_response = client.get(list_url, headers={"Accept": "application/ld+json"})
    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            logger.info(
                "No SRF product type registered for WFO %s (404); returning empty result.",
                wfo,
            )
            return None
        raise

    try:
        list_wire = _NwsProductListResponse.model_validate(list_response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS SRF product list response validation failed for WFO %s: %s. "
            "Response body (first 2000 chars): %.2000s",
            wfo,
            exc,
            list_response.text,
        )
        raise ProviderProtocolError(
            f"NWS SRF product list response validation failed for WFO {wfo!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    if not list_wire.graph:
        logger.info("WFO %s has no recent SRF products; returning empty result.", wfo)
        return None

    stub = list_wire.graph[0]
    if stub.at_id:
        product_url = stub.at_id
    elif stub.id.startswith("http"):
        product_url = stub.id
    else:
        product_url = f"{NWS_BASE_URL}/products/{stub.id}"

    _rate_limiter.acquire()
    try:
        detail_response = client.get(product_url, headers={"Accept": "application/ld+json"})
    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            logger.info(
                "SRF product %s for WFO %s returned 404; returning empty result.",
                product_url,
                wfo,
            )
            return None
        raise

    try:
        return _NwsProductDetail.model_validate(detail_response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS SRF product detail response validation failed for %s: %s. "
            "Response body (first 2000 chars): %.2000s",
            product_url,
            exc,
            detail_response.text,
        )
        raise ProviderProtocolError(
            f"NWS SRF product detail response validation failed for {product_url!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc


# ---------------------------------------------------------------------------
# SRF free-text parsing (module docstring "SRF text parsing")
# ---------------------------------------------------------------------------

# A day-period marker line, e.g. ".TODAY...", ".TONIGHT...", ".TOMORROW..."
_PERIOD_MARKER_RE = re.compile(r"^\.([A-Z][A-Z .]*?)\.\.\.[ \t]*$", re.MULTILINE)

# A zone-header line: ends in "..." with nothing following on the line, and
# does not start with "." (which would make it a period marker instead).
# Field lines (SURF HEIGHT...2 TO 4 FEET.) always have trailing content
# after "...", so they never match this pattern.
_ZONE_HEADER_RE = re.compile(r"^(?!\.)([A-Za-z][^\n]*?)\.\.\.[ \t]*$", re.MULTILINE)

_ZONE_HEADER_PREFIX_RE = re.compile(r"^SURF ZONE FORECAST FOR\s+", re.IGNORECASE)

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")

# Field-value terminator: a period followed by whitespace or end-of-string.
# This (rather than a bare "\.") avoids stopping mid-value on a decimal
# point, e.g. "SURF HEIGHT...2.5 TO 4.5 FEET." would incorrectly truncate
# to "2" with a naive non-greedy "\." terminator.
_SURF_HEIGHT_RE = re.compile(r"SURF\s+HEIGHT\.\.\.(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL)
_RIP_RISK_RE = re.compile(
    r"RIP\s+CURRENT\s+RISK\.\.\.(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL
)
_UV_INDEX_RE = re.compile(r"UV\s+INDEX\.\.\.(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL)
_WATER_TEMP_RE = re.compile(
    r"WATER\s+TEMPERATURE\.\.\.(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL
)
_WIND_RE = re.compile(r"WIND\.\.\.(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL)
_HAZARDS_RE = re.compile(r"HAZARDS\.\.\.(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL)

_DAY_OFFSET_BY_LABEL: dict[str, int] = {
    "TODAY": 0,
    "THIS MORNING": 0,
    "THIS AFTERNOON": 0,
    "TONIGHT": 0,
    "TOMORROW": 1,
    "TOMORROW NIGHT": 1,
}


def _resolve_period_offset(period_label: str) -> int | None:
    """Map a day-period label to a day offset from the product's issuance date.

    Returns None for unrecognized labels (e.g. a weekday name used on a
    late-night reissue) — the caller logs a WARNING and skips that block
    rather than guessing at an offset.
    """
    normalized = re.sub(r"\s+", " ", period_label.strip().upper()).rstrip(".")
    return _DAY_OFFSET_BY_LABEL.get(normalized)


def _normalize_zone_text(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def _zone_header_matches(header_text: str, zone_name: str) -> bool:
    """Fuzzy-match an SRF zone-header line against the resolved zone name.

    Case-insensitive containment in either direction after normalization,
    with the common "Surf Zone Forecast for " prefix stripped. This handles
    both the brief's sample header style ("Surf Zone Forecast for {zone
    name}...") and the more common real-product style (the bare zone name,
    e.g. "CARTERET COUNTY BEACHES...").
    """
    cleaned = _ZONE_HEADER_PREFIX_RE.sub("", header_text.strip())
    header_normalized = _normalize_zone_text(cleaned)
    zone_normalized = _normalize_zone_text(zone_name)
    if not header_normalized or not zone_normalized:
        return False
    return header_normalized in zone_normalized or zone_normalized in header_normalized


def _extract_field(pattern: re.Pattern[str], block: str) -> str | None:
    match = pattern.search(block)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _parse_surf_height_range(raw: str | None) -> tuple[float | None, float | None]:
    if not raw:
        return None, None
    numbers = [float(n) for n in _NUMBER_RE.findall(raw)]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return min(numbers), max(numbers)


def _parse_uv_index(raw: str | None) -> int | None:
    if not raw:
        return None
    match = _NUMBER_RE.search(raw)
    if not match:
        return None
    return int(float(match.group(1)))


def _parse_water_temp_f(raw: str | None) -> float | None:
    if not raw:
        return None
    match = _NUMBER_RE.search(raw)
    if not match:
        return None
    return float(match.group(1))


def _parse_rip_current_risk(raw: str) -> str:
    """Normalize rip current risk text to "low" / "moderate" / "high".

    Compound values (e.g. "MODERATE TO HIGH") resolve to the higher-risk
    category — weather data is safety-critical (rules/coding.md §1); when a
    forecast spans a range, surfacing the worse case is the safer default.
    """
    upper = raw.upper()
    if "HIGH" in upper:
        return "high"
    if "MODERATE" in upper:
        return "moderate"
    if "LOW" in upper:
        return "low"
    return raw.strip().lower()


def _extract_zone_fields(sub_block: str, target_zone_id: str) -> dict[str, Any] | None:
    """Extract SurfZoneForecast field values from one zone's text sub-block.

    Returns None (logged at WARNING) when RIP CURRENT RISK — the one
    non-optional canonical field — cannot be found; all other fields are
    optional on SurfZoneForecast and are simply None when not parseable.
    """
    rip_raw = _extract_field(_RIP_RISK_RE, sub_block)
    if rip_raw is None:
        logger.warning(
            "NWS SRF parsing: could not find RIP CURRENT RISK for zone %s; "
            "skipping this entry. Raw block (first 1000 chars): %.1000s",
            target_zone_id,
            sub_block,
        )
        return None

    surf_raw = _extract_field(_SURF_HEIGHT_RE, sub_block)
    surf_min_ft, surf_max_ft = _parse_surf_height_range(surf_raw)

    uv_raw = _extract_field(_UV_INDEX_RE, sub_block)
    water_raw = _extract_field(_WATER_TEMP_RE, sub_block)
    wind_raw = _extract_field(_WIND_RE, sub_block)
    hazards_raw = _extract_field(_HAZARDS_RE, sub_block)

    water_temp_f = _parse_water_temp_f(water_raw)

    hazards_text: str | None = hazards_raw
    if hazards_text and hazards_text.strip().upper() == "NONE":
        hazards_text = None

    return {
        "ripCurrentRisk": _parse_rip_current_risk(rip_raw),
        "surfHeightMin": convert(surf_min_ft, "foot", "meter"),
        "surfHeightMax": convert(surf_max_ft, "foot", "meter"),
        "uvIndex": _parse_uv_index(uv_raw),
        "waterTemp": convert(water_temp_f, "degree_F", "degree_C"),
        "windText": wind_raw,
        "hazardsText": hazards_text,
    }


def _parse_zone_block(
    block_text: str, target_zone_id: str, target_zone_name: str
) -> dict[str, Any] | None:
    """Find the target zone's section within one day-period block and extract its fields.

    Returns None when the block has zone headers but none match the target
    zone (this WFO's SRF doesn't cover the spot's zone on this day), or
    when no headers are found at all and field extraction on the whole
    block still fails.
    """
    header_matches = list(_ZONE_HEADER_RE.finditer(block_text))
    if not header_matches:
        # No explicit per-zone header (e.g. a single-zone SRF product) —
        # treat the entire block as the target zone's data.
        return _extract_zone_fields(block_text, target_zone_id)

    for i, header_match in enumerate(header_matches):
        header_text = header_match.group(1)
        if not _zone_header_matches(header_text, target_zone_name):
            continue
        sub_start = header_match.end()
        sub_end = (
            header_matches[i + 1].start() if i + 1 < len(header_matches) else len(block_text)
        )
        return _extract_zone_fields(block_text[sub_start:sub_end], target_zone_id)

    return None


def _parse_srf_text(
    text: str,
    issuance_dt: datetime,
    target_zone_id: str,
    target_zone_name: str,
) -> list[SurfZoneForecast]:
    """Parse a full SRF product text into per-day SurfZoneForecast records
    for the target zone.

    Unrecognized day-period labels and zone sections missing RIP CURRENT
    RISK are logged at WARNING and skipped (partial result), per
    PROVIDER-MANUAL §14.5.
    """
    results: list[SurfZoneForecast] = []
    period_matches = list(_PERIOD_MARKER_RE.finditer(text))
    if not period_matches:
        logger.warning(
            "NWS SRF text for zone %s contained no recognizable day-period "
            "markers (.TODAY..., .TOMORROW..., etc). "
            "Raw text (first 2000 chars): %.2000s",
            target_zone_id,
            text,
        )
        return results

    sign_off = text.find("$$")
    text_end = sign_off if sign_off != -1 else len(text)

    for i, match in enumerate(period_matches):
        period_label = match.group(1)
        offset = _resolve_period_offset(period_label)
        block_start = match.end()
        block_end = (
            period_matches[i + 1].start() if i + 1 < len(period_matches) else text_end
        )
        block_text = text[block_start:block_end]

        if offset is None:
            logger.warning(
                "NWS SRF text for zone %s used unrecognized period label %r; "
                "skipping this block. Raw block (first 500 chars): %.500s",
                target_zone_id,
                period_label,
                block_text,
            )
            continue

        fields = _parse_zone_block(block_text, target_zone_id, target_zone_name)
        if fields is None:
            continue

        date_str = (issuance_dt.date() + timedelta(days=offset)).isoformat()
        results.append(
            SurfZoneForecast(
                date=date_str,
                countyZone=target_zone_id,
                ripCurrentRisk=fields["ripCurrentRisk"],
                surfHeightMin=fields["surfHeightMin"],
                surfHeightMax=fields["surfHeightMax"],
                uvIndex=fields["uvIndex"],
                waterTemp=fields["waterTemp"],
                windText=fields["windText"],
                hazardsText=fields["hazardsText"],
            )
        )

    return results


# ---------------------------------------------------------------------------
# Public fetch entrypoint
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    county_zone: str | None = None,
    user_agent_contact: str | None = None,
) -> dict[str, object]:
    """Fetch NWS SRF for the WFO covering (lat, lon).

    Args:
        lat: Spot latitude, WGS84 decimal degrees, must be within [-90, 90].
        lon: Spot longitude, WGS84 decimal degrees, must be within [-180, 180].
        county_zone: NWS public forecast zone ID (e.g. "NCZ027") if already
            known (e.g. from operator config, mirroring the
            nws_marine_zone_id pattern). When None, resolved from (lat, lon)
            via /points -> properties.forecastZone.
        user_agent_contact: Operator-configured NWS UA contact (ADR-006).

    Returns:
        dict with:
          - "forecasts": list[SurfZoneForecast] — may be empty if the
            covering WFO issues no SRF product, or if the SRF text doesn't
            cover the spot's zone.
          - "wfo": str — the resolved WFO (CWA) covering the spot.

    Raises:
        ValueError: lat/lon out of range, or county_zone is an empty string.
        GeographicallyUnsupported: get_cwa() found no NWS coverage for
            (lat, lon) (non-US location).
        QuotaExhausted: NWS returned 429 (rate limit).
        KeyInvalid: NWS returned 401/403 (exotic; NWS is keyless).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed (NWS schema
            change), or /points omitted properties.forecastZone.
    """
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"lat must be within [-90, 90]; got {lat!r}")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"lon must be within [-180, 180]; got {lon!r}")
    if county_zone is not None and not county_zone.strip():
        raise ValueError("county_zone must not be empty when provided")

    if not user_agent_contact:
        _warn_once_missing_contact()

    wfo = get_cwa(lat, lon, user_agent_contact=user_agent_contact)

    zone_id = county_zone if county_zone is not None else _resolve_forecast_zone_id(
        lat, lon, user_agent_contact
    )
    zone_name = _resolve_zone_name(zone_id, user_agent_contact)

    cache_key = _build_srf_cache_key(wfo, zone_id)
    cached = get_cache().get(cache_key)
    if cached is not None:
        return {
            "forecasts": [
                SurfZoneForecast.model_validate(d) for d in cached["forecasts"]
            ],
            "wfo": cached["wfo"],
        }

    product = _fetch_latest_srf_product(wfo, user_agent_contact)
    if product is None:
        empty_result = {"forecasts": [], "wfo": wfo}
        get_cache().set(cache_key, empty_result, ttl_seconds=_CACHE_TTL_SECONDS)
        return {"forecasts": [], "wfo": wfo}

    try:
        issuance_dt = datetime.fromisoformat(product.issuanceTime)
    except ValueError as exc:
        logger.warning(
            "NWS SRF product %s issuanceTime %r could not be parsed (%s); "
            "cannot derive forecast dates, returning empty result.",
            product.id,
            product.issuanceTime,
            exc,
        )
        empty_result = {"forecasts": [], "wfo": wfo}
        get_cache().set(cache_key, empty_result, ttl_seconds=_CACHE_TTL_SECONDS)
        return {"forecasts": [], "wfo": wfo}

    forecasts = _parse_srf_text(product.productText, issuance_dt, zone_id, zone_name)

    get_cache().set(
        cache_key,
        {"forecasts": [f.model_dump() for f in forecasts], "wfo": wfo},
        ttl_seconds=_CACHE_TTL_SECONDS,
    )

    logger.info(
        "NWS SRF fetched: %d forecast(s) for wfo=%s county_zone=%s",
        len(forecasts),
        wfo,
        zone_id,
    )
    return {"forecasts": forecasts, "wfo": wfo}


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client, _http_client_ua, _warned_missing_contact  # noqa: PLW0603
    _http_client = None
    _http_client_ua = ""
    _warned_missing_contact = False
