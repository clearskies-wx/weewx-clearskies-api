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

WFO determination (PROVIDER-MANUAL §14.5, §14.8; extended 2026-07-13,
Marine Remediation Plan T1.4 — see docs/planning/MARINE-REMEDIATION-PLAN.md):
  The coordinate-only `get_cwa()` lookup breaks for shoreline spots whose
  (lat, lon) resolves to a marine zone (e.g. PZZ655) rather than a county
  zone, and/or whose CWA differs from the WFO that actually issues the SRF
  for that county (observed for Huntington Beach, CA: get_cwa() resolves
  LOX, but Orange County's SRF is issued by SGX). fetch() now resolves the
  WFO with this priority:
    1. `wfo_override` kwarg, when the caller already knows the issuing WFO
       (operator config, mirroring nwps.py's `wfo_override` pattern).
    2. The zone's own `cwa` property from the `/zones/forecast/{zoneId}`
       response (already fetched for the zone *name* — see
       `_resolve_zone_name_and_cwa` below) — used only when the zone ID was
       auto-resolved from (lat, lon) rather than supplied as `county_zone`.
       This is the fix for the SGX/LOX case: the zone is the more reliable
       anchor than the raw coordinate CWA lookup.
    3. `get_cwa()` (the original coordinate → CWA lookup), used when the
       caller passed `county_zone` explicitly and no zone-derived cwa
       applies (preserves the original operator-config call shape).
  Separately, `_resolve_forecast_zone_id()` now detects when /points
  resolves to a marine zone (PZZ/AMZ/PKZ/PMZ/PHZ/PSZ/GMZ/ANZ/LMZ/LHZ/LEZ/
  LOZ/LSZ/SLZ prefix) instead of a county zone — SRF sections are keyed by
  county UGC codes, so a marine zone ID never matches any section. On a
  marine-zone result, it retries /points with latitude shifted +0.015°
  (~1.7 km, generally inland for US coasts) and uses that result if it
  resolves to a non-marine zone; if the retry is *also* marine, the
  original marine zone_id is kept (some configured spots genuinely sit in
  a marine zone with no nearby inland county zone).

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

SRF text parsing (rewritten 2026-07-11, Marine Remediation Plan T1.1 — the
prior period-then-zone design returned empty results against every real
SRF product; see docs/planning/MARINE-REMEDIATION-PLAN.md T1.1 for the P1,
P2, P4, P5, P6 defects this fixed):
  The real wire format is zone-then-period, not period-then-zone. The
  product text is one or more county-zone sections separated by "$$",
  each starting with a UGC line (e.g. "NCZ108-120515-", or a compressed
  multi-zone line "NCZ106-107-108-236230-" covering several zones with
  identical text), followed by the zone name, a beach list, and then
  day-period blocks (".REST OF TODAY...", ".SUNDAY...", ".EXTENDED...",
  etc.) each holding the labeled fields. Footnote definitions follow "&&"
  at the end of each zone section and are stripped before period parsing.

  Zone-section selection (_find_target_zone_section): primary match is by
  UGC zone code — the target zone ID's 2-letter-state+"Z" prefix and
  3-digit suffix are looked for among a section's UGC line(s), expanding
  compressed multi-zone lines to their individual codes
  (_expand_ugc_codes). Fallback 1: when the product has exactly one "$$"
  section, treat it as the target zone (single-zone SRF, no UGC needed).
  Fallback 2: fuzzy-match the resolved zone *name* (_zone_header_matches)
  against each section's header text only — the text before that
  section's first day-period marker (UGC line + zone name + beach list).
  Restricting the name fallback to the pre-period header area is
  deliberate: matching zone-name text anywhere in the section (the old
  design) false-matched field-adjacent lines like "Tides...",
  "Remarks...", "Weather..." (P2) and mixed data across zones (P6).

  Within the matched zone section, day-period labels are mapped to a day
  offset from the product's `issuanceTime` (_resolve_period_offset):
  TODAY/REST OF TODAY/THIS AFTERNOON/THIS MORNING/TONIGHT -> 0,
  TOMORROW/TOMORROW NIGHT -> 1, and the seven weekday names (SUNDAY
  through SATURDAY) -> computed dynamically as
  (target_weekday - issuance_weekday) % 7. A same-weekday label (e.g. an
  early-morning reissue labeling today ".SATURDAY..." instead of ".REST
  OF TODAY...") therefore resolves to offset 0 — NWS does label the
  issuance day by weekday name on some reissues (lead clarification,
  2026-07-11); there is no "assume next week" special case. "EXTENDED"
  and any other unrecognized label fall through to None and that block is
  logged at WARNING and skipped (partial result), per PROVIDER-MANUAL
  §14.5's "text parsing failure -> log WARNING ... return partial result"
  instruction. Likewise, a zone-period block missing a parseable RIP
  CURRENT RISK (the one non-optional canonical field on
  SurfZoneForecast) is logged at WARNING and skipped; all other fields
  are optional on the canonical model and are simply None when not
  parseable.

  Field labels use dot-leaders with 0-3 optional asterisk footnote
  markers between the label and the leading dots (e.g. "Rip Current
  Risk*...........Moderate.", "UV Index**...5."), and WIND's label is
  optionally plural ("Winds....." as well as "Wind..."). All six field
  regexes tolerate both. When a zone splits a field into sub-regions
  (e.g. "East of Ocean Isle Beach" / "Ocean Isle Beach West" each with
  their own "Rip Current Risk..." line), `_extract_field`'s `re.search`
  is leftmost-match by construction, so the first sub-region's value wins
  without any extra code — no averaging or last-match behavior.

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

# NWS marine (water) zone prefixes. A /points forecastZone resolving to one
# of these means the coordinate landed on/near a water polygon rather than a
# county/land zone — SRF sections are keyed by county UGC codes, so a marine
# zone ID never matches any SRF section (T1.4, Marine Remediation Plan).
_MARINE_ZONE_PREFIXES: tuple[str, ...] = (
    "PZZ",
    "AMZ",
    "PKZ",
    "PMZ",
    "PHZ",
    "PSZ",
    "GMZ",
    "ANZ",
    "LMZ",
    "LHZ",
    "LEZ",
    "LOZ",
    "LSZ",
    "SLZ",
)

# Latitude shift applied when retrying a marine-zone /points result — moves
# ~1.7 km north, generally inland for US coasts (T1.4).
_INLAND_RETRY_LAT_SHIFT = 0.015


def _is_marine_zone(zone_id: str) -> bool:
    return zone_id.upper().startswith(_MARINE_ZONE_PREFIXES)

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
    """Properties of a /zones/forecast/{zoneId} detail response.

    `cwa` (added T1.4, Marine Remediation Plan): the WFO office ID that
    issues forecast products for this zone. NWS returns this as either a
    bare string (e.g. "SGX") or a single-element list (observed shape
    varies by zone type) — both are normalized to `str | None` by
    `_resolve_zone_name_and_cwa`.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    cwa: str | list[str] | None = None


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


def _fetch_forecast_zone_id_raw(
    lat: float, lon: float, user_agent_contact: str | None
) -> str:
    """GET /points/{lat},{lon} -> properties.forecastZone -> zone ID.

    No caching, no marine-zone retry — the uncached single-shot lookup used
    by both the primary call and the inland-shifted retry in
    `_resolve_forecast_zone_id`.

    Raises:
        ProviderProtocolError: response validation failed, or the response
            omitted properties.forecastZone (unexpected NWS schema change
            for this coordinate — every US coastal point has one).
    """
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

    return forecast_zone_url.rstrip("/").rsplit("/", 1)[-1]


def _resolve_forecast_zone_id(lat: float, lon: float, user_agent_contact: str | None) -> str:
    """Resolve the NWS public forecast zone ID covering (lat, lon).

    GET /points/{lat},{lon} -> properties.forecastZone -> zone ID (final
    path segment of the forecastZone URL).

    Marine-zone retry (T1.4, Marine Remediation Plan): when the resolved
    zone has a marine prefix (see `_MARINE_ZONE_PREFIXES`), the coordinate
    landed on/near a water polygon rather than a county zone — SRF sections
    are keyed by county UGC codes, so a marine zone ID would never match.
    Retry once with latitude shifted `_INLAND_RETRY_LAT_SHIFT` degrees north
    (generally inland for US coasts); use the retried zone if it resolves
    to a non-marine zone, otherwise keep the original marine zone_id (some
    configured spots genuinely sit in a marine zone with no nearby inland
    county zone). The retry itself is not cached — only the final resolved
    zone_id is, keyed by the *original* (lat, lon), so repeat calls for the
    same point never re-run the retry within the cache window.

    Raises:
        ProviderProtocolError: response validation failed, or the response
            omitted properties.forecastZone (unexpected NWS schema change
            for this coordinate — every US coastal point has one).
    """
    cache_key = _build_points_cache_key(lat, lon)
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cast(str, cached)

    zone_id = _fetch_forecast_zone_id_raw(lat, lon, user_agent_contact)

    if _is_marine_zone(zone_id):
        logger.debug(
            "Forecast zone %s is a marine zone; retrying with inland-shifted coordinates",
            zone_id,
        )
        retried_zone_id = _fetch_forecast_zone_id_raw(
            lat + _INLAND_RETRY_LAT_SHIFT, lon, user_agent_contact
        )
        if not _is_marine_zone(retried_zone_id):
            zone_id = retried_zone_id
        # else: retry also landed in a marine zone — keep the original
        # marine zone_id (fallback for spots genuinely in a marine zone).

    get_cache().set(cache_key, zone_id, ttl_seconds=_ZONE_LOOKUP_TTL_SECONDS)
    return zone_id


def _resolve_zone_name_and_cwa(
    zone_id: str, user_agent_contact: str | None
) -> tuple[str, str | None]:
    """Resolve the human-readable name and issuing WFO (cwa) for a public
    forecast zone ID.

    GET /zones/forecast/{zoneId} -> properties.name (the SRF text carries
    zone *names* in its section headers, not zone IDs, so this name is what
    gets fuzzy-matched against the parsed text — see _zone_header_matches)
    and properties.cwa (T1.4, Marine Remediation Plan: the WFO that issues
    forecast products for this zone, used by fetch() as the preferred WFO
    source over the raw coordinate-based get_cwa() lookup when the zone was
    auto-resolved — see module docstring "WFO determination").

    Both values come from the same response and are cached together under
    one key, so adding cwa resolution costs no extra HTTP call.

    Raises:
        ProviderProtocolError: response validation failed.
    """
    cache_key = _build_zone_name_cache_key(zone_id)
    cached = get_cache().get(cache_key)
    if cached is not None:
        cached_dict = cast(dict[str, Any], cached)
        return cast(str, cached_dict["name"]), cast("str | None", cached_dict.get("cwa"))

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
    raw_cwa = wire.properties.cwa
    zone_cwa = (raw_cwa[0] if raw_cwa else None) if isinstance(raw_cwa, list) else raw_cwa

    get_cache().set(
        cache_key,
        {"name": zone_name, "cwa": zone_cwa},
        ttl_seconds=_ZONE_LOOKUP_TTL_SECONDS,
    )
    return zone_name, zone_cwa


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

# A day-period marker line, e.g. ".TODAY...", ".REST OF TODAY...",
# ".SUNDAY...", ".EXTENDED..."
_PERIOD_MARKER_RE = re.compile(r"^\.([A-Z][A-Z .]*?)\.\.\.[ \t]*$", re.MULTILINE)

_ZONE_HEADER_PREFIX_RE = re.compile(r"^SURF ZONE FORECAST FOR\s+", re.IGNORECASE)

# UGC zone-header line, e.g. "NCZ108-120515-" (single zone) or
# "NCZ106-107-108-236230-" (compressed multi-zone: first code carries the
# full state+"Z"+3-digit prefix, later 3-digit groups are additional zone
# suffixes sharing that prefix, and the final group is a 6-digit purge
# time stamp, not a zone code). See _expand_ugc_codes.
_UGC_LINE_RE = re.compile(r"^([A-Z]{2}Z\d{3}(?:-\d{3,6})+-)[ \t]*$", re.MULTILINE)
_ZONE_ID_RE = re.compile(r"^([A-Z]{2}Z)(\d{3})$")

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")

# Field-value terminator: a period followed by whitespace or end-of-string.
# This (rather than a bare "\.") avoids stopping mid-value on a decimal
# point, e.g. "SURF HEIGHT...2.5 TO 4.5 FEET." would incorrectly truncate
# to "2" with a naive non-greedy "\." terminator.
#
# All field labels tolerate 0-3 optional asterisk footnote markers between
# the label and the dot-leaders (e.g. "Rip Current Risk*...........
# Moderate.", "UV Index**...5.") — real NWS SRF products annotate several
# fields with footnote references. WIND's label is also optionally plural
# ("Winds....." as well as "Wind...").
#
# The dot-leader itself is "\.\.\.+" (3+ dots, greedy) rather than a fixed
# "\.\.\." — live products pad the leader to align values (often 8-11+
# dots), and a fixed-3 separator combined with the lazy "(.*?)" value
# capture would leave the extra leader dots inside the captured group
# (they'd only get swallowed once the lazy match walks past them looking
# for the terminating "\.(?=\s|$)", by which point they're already part
# of group(1)). Greedily consuming the whole leader as the separator keeps
# the capture group starting exactly at the value.
_SURF_HEIGHT_RE = re.compile(
    r"SURF\s+HEIGHT\*{0,3}\.\.\.+(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL
)
_RIP_RISK_RE = re.compile(
    r"RIP\s+CURRENT\s+RISK\*{0,3}\.\.\.+(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL
)
_UV_INDEX_RE = re.compile(
    r"UV\s+INDEX\*{0,3}\.\.\.+(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL
)
_WATER_TEMP_RE = re.compile(
    r"WATER\s+TEMPERATURE\*{0,3}\.\.\.+(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL
)
_WIND_RE = re.compile(r"WINDS?\*{0,3}\.\.\.+(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL)
_HAZARDS_RE = re.compile(
    r"HAZARDS\*{0,3}\.\.\.+(.*?)\.(?=\s|$)", re.IGNORECASE | re.DOTALL
)

_DAY_OFFSET_BY_LABEL: dict[str, int] = {
    "TODAY": 0,
    "REST OF TODAY": 0,
    "THIS MORNING": 0,
    "THIS AFTERNOON": 0,
    "TONIGHT": 0,
    "TOMORROW": 1,
    "TOMORROW NIGHT": 1,
}

# Monday=0 ... Sunday=6, matching datetime.weekday(). Used to compute a
# dynamic offset for weekday-name period labels (e.g. "SUNDAY"), which real
# SRF products use for days beyond tomorrow and, on some early-morning
# reissues, for the issuance day itself (see module docstring).
_WEEKDAYS: tuple[str, ...] = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)


def _resolve_period_offset(period_label: str, issuance_dt: datetime) -> int | None:
    """Map a day-period label to a day offset from the product's issuance date.

    Returns None for unrecognized labels (e.g. "EXTENDED", a multi-day
    summary block that isn't a single dated forecast) — the caller logs a
    WARNING and skips that block rather than guessing at an offset.
    """
    normalized = re.sub(r"\s+", " ", period_label.strip().upper()).rstrip(".")
    if normalized in _DAY_OFFSET_BY_LABEL:
        return _DAY_OFFSET_BY_LABEL[normalized]
    if normalized in _WEEKDAYS:
        issuance_weekday = issuance_dt.weekday()
        target_weekday = _WEEKDAYS.index(normalized)
        # Same weekday as issuance -> offset 0 (NWS labels the issuance day
        # by weekday name on some reissues; lead clarification 2026-07-11).
        return (target_weekday - issuance_weekday) % 7
    return None


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
    # Defensive: the dot-leader regexes are greedy over the separator so
    # this shouldn't happen, but strip any stray leading dots/whitespace
    # rather than surface them in a canonical field value (e.g. windText).
    return re.sub(r"\s+", " ", match.group(1)).strip(" .")


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


def _expand_ugc_codes(ugc_line: str) -> tuple[str | None, set[str]]:
    """Expand a UGC zone-header line to its (prefix, {zone-code-suffixes}).

    "NCZ108-120515-" -> ("NCZ", {"108"}). "NCZ106-107-108-236230-" (a
    compressed multi-zone line covering several zones with identical
    forecast text) -> ("NCZ", {"106", "107", "108"}) — the trailing
    6-digit group is a purge time stamp, not a zone code, and is dropped
    by the len==3 check.
    """
    parts = ugc_line.strip().rstrip("-").split("-")
    if not parts:
        return None, set()
    first_match = _ZONE_ID_RE.match(parts[0])
    if not first_match:
        return None, set()
    prefix = first_match.group(1)
    codes = {first_match.group(2)}
    for part in parts[1:]:
        if len(part) == 3 and part.isdigit():
            codes.add(part)
    return prefix, codes


def _find_target_zone_section(
    text: str, target_zone_id: str, target_zone_name: str
) -> str | None:
    """Find the "$$"-delimited zone section covering `target_zone_id`.

    Primary: match a UGC zone-header line's expanded codes against the
    target zone ID. Fallback 1: a single-section product (no per-zone UGC
    needed) is assumed to be the target zone. Fallback 2: fuzzy-match the
    resolved zone name against each section's pre-first-period header text
    only (never the field-heavy period blocks — see module docstring "SRF
    text parsing" for why that scoping matters, P2).
    """
    sections = [s for s in text.split("$$") if s.strip()]
    if not sections:
        return None

    zone_id_match = _ZONE_ID_RE.match(target_zone_id.strip().upper())
    if zone_id_match:
        target_prefix, target_suffix = zone_id_match.group(1), zone_id_match.group(2)
        for section in sections:
            for ugc_line in _UGC_LINE_RE.findall(section):
                prefix, codes = _expand_ugc_codes(ugc_line)
                if prefix == target_prefix and target_suffix in codes:
                    return section

    if len(sections) == 1:
        return sections[0]

    for section in sections:
        first_period = _PERIOD_MARKER_RE.search(section)
        header_area = section[: first_period.start()] if first_period else section
        for line in header_area.splitlines():
            if _zone_header_matches(line, target_zone_name):
                return section

    return None


def _parse_srf_text(
    text: str,
    issuance_dt: datetime,
    target_zone_id: str,
    target_zone_name: str,
) -> list[SurfZoneForecast]:
    """Parse a full SRF product text into per-day SurfZoneForecast records
    for the target zone.

    Zone-then-period: first isolate the target zone's "$$"-delimited
    section (_find_target_zone_section), strip its trailing footnotes
    (everything after "&&"), then walk that section's day-period markers
    only — never another zone's data (fixes P6). Unrecognized day-period
    labels and zone-period blocks missing RIP CURRENT RISK are logged at
    WARNING and skipped (partial result), per PROVIDER-MANUAL §14.5.
    """
    results: list[SurfZoneForecast] = []

    section = _find_target_zone_section(text, target_zone_id, target_zone_name)
    if section is None:
        logger.warning(
            "NWS SRF text contained no zone section matching %s (UGC code "
            "or zone name %r); returning empty result. "
            "Raw text (first 2000 chars): %.2000s",
            target_zone_id,
            target_zone_name,
            text,
        )
        return results

    # Footnote definitions follow "&&" at the end of a zone section — strip
    # before period parsing so footnote text never leaks into field values.
    section = section.split("&&", 1)[0]

    period_matches = list(_PERIOD_MARKER_RE.finditer(section))
    if not period_matches:
        logger.warning(
            "NWS SRF zone section for %s contained no recognizable "
            "day-period markers (.REST OF TODAY..., .SUNDAY..., etc). "
            "Raw section (first 2000 chars): %.2000s",
            target_zone_id,
            section,
        )
        return results

    for i, match in enumerate(period_matches):
        period_label = match.group(1)
        offset = _resolve_period_offset(period_label, issuance_dt)
        block_start = match.end()
        block_end = (
            period_matches[i + 1].start() if i + 1 < len(period_matches) else len(section)
        )
        block_text = section[block_start:block_end]

        if offset is None:
            logger.warning(
                "NWS SRF text for zone %s used unrecognized period label %r; "
                "skipping this block. Raw block (first 500 chars): %.500s",
                target_zone_id,
                period_label,
                block_text,
            )
            continue

        fields = _extract_zone_fields(block_text, target_zone_id)
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
    wfo_override: str | None = None,
    user_agent_contact: str | None = None,
) -> dict[str, object]:
    """Fetch NWS SRF for the WFO covering (lat, lon).

    Args:
        lat: Spot latitude, WGS84 decimal degrees, must be within [-90, 90].
        lon: Spot longitude, WGS84 decimal degrees, must be within [-180, 180].
        county_zone: NWS public forecast zone ID (e.g. "NCZ027") if already
            known (e.g. from operator config, mirroring the
            nws_marine_zone_id pattern). When None, resolved from (lat, lon)
            via /points -> properties.forecastZone (with marine-zone retry,
            see `_resolve_forecast_zone_id`).
        wfo_override: WFO office ID (e.g. "SGX") if the caller already knows
            which WFO issues the SRF for this spot (operator config; naming
            mirrors nwps.py's `wfo_override` pattern). Takes priority over
            both zone-derived and coordinate-derived WFO resolution — use
            this when a spot's SRF-issuing WFO differs from its coordinate
            CWA (T1.4, Marine Remediation Plan; see module docstring "WFO
            determination").
        user_agent_contact: Operator-configured NWS UA contact (ADR-006).

    Returns:
        dict with:
          - "forecasts": list[SurfZoneForecast] — may be empty if the
            covering WFO issues no SRF product, or if the SRF text doesn't
            cover the spot's zone.
          - "wfo": str — the resolved WFO (CWA) covering the spot.

    Raises:
        ValueError: lat/lon out of range, or county_zone/wfo_override is an
            empty string.
        GeographicallyUnsupported: get_cwa() found no NWS coverage for
            (lat, lon) (non-US location). Not raised when wfo_override is
            supplied, or when the zone was auto-resolved and its cwa
            property was present (get_cwa() is skipped in both cases).
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
    if wfo_override is not None and not wfo_override.strip():
        raise ValueError("wfo_override must not be empty when provided")

    if not user_agent_contact:
        _warn_once_missing_contact()

    zone_auto_resolved = county_zone is None
    zone_id = (
        county_zone
        if county_zone is not None
        else _resolve_forecast_zone_id(lat, lon, user_agent_contact)
    )
    zone_name, zone_cwa = _resolve_zone_name_and_cwa(zone_id, user_agent_contact)

    # WFO priority (T1.4, Marine Remediation Plan; see module docstring
    # "WFO determination"): wfo_override > zone-derived cwa (auto-resolved
    # zones only) > get_cwa(lat, lon) (unchanged path for explicit
    # county_zone callers).
    if wfo_override:
        wfo = wfo_override
    elif zone_auto_resolved and zone_cwa:
        wfo = zone_cwa
    else:
        wfo = get_cwa(lat, lon, user_agent_contact=user_agent_contact)

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
