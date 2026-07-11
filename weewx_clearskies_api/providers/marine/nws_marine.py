"""NWS marine zone text forecast provider module (ADR-083, PROVIDER-MANUAL §14.4).

Five responsibilities per the §1 Module Contract (PROVIDER-MANUAL):
  1. Outbound API call — NWS CWF (Coastal Waters Forecast) text product:
     /products/types/CWF/locations/{wfo} -> /products/{id}.
  2. Response parsing — free-text CWF product parsing (per-zone UGC
     sections, `.PERIOD...` markers) via regex.
  3. Translation to canonical MarineTextForecast (models/responses.py).
  4. Capability declaration — CAPABILITY symbol consumed at startup.
  5. Error handling — provider errors translated to canonical taxonomy.

Wire format fix (2026-07-11): This module previously called
`GET /zones/coastal/{zoneId}/forecast`, which does not exist on the live
NWS API (confirmed 404 "Forecasts for marine areas are not yet supported by
this API"). Marine zone forecasts are only published as CWF free-text
products, keyed by WFO (not by zone) — the same product-list/product-detail
shape used by `providers/marine/nws_srf.py` for SRF (PROVIDER-MANUAL §14.5),
just a different product type (CWF vs SRF) and free-text grammar. See
PROVIDER-MANUAL §14.4 for the corrected wire format.

Zone ID source (PROVIDER-MANUAL §14.4):
  The zone ID comes from the operator's marine location configuration
  (`nws_marine_zone_id` field), discovered at setup time by the shared
  utility at providers/_common/nws_zones.py (§14.8). This module does not
  itself perform zone discovery.

WFO determination (PROVIDER-MANUAL §14.4, §14.8):
  `fetch()` accepts `zone_id` only (no lat/lon required) — the WFO that
  issues the covering CWF product is resolved via
  `providers/_common/nws_zones.py::get_wfo_for_zone()`, which reuses the
  already-cached (24h) `type=coastal` zone list's `cwa` property (the same
  list `discover_marine_zones()` uses). This keeps the existing
  `fetch(zone_id=...)` call shape used by `endpoints/marine.py` and by
  `tests/integration/test_nws_marine_integration.py` unchanged. An optional
  `wfo_override` kwarg (naming mirrors `providers/marine/nwps.py`'s
  `wfo_override` pattern) lets a caller that already knows the WFO skip
  this lookup.

NWS User-Agent (ADR-006):
  Same construction pattern as providers/alerts/nws.py and
  providers/forecast/nws.py: "(weewx-clearskies-api/<version>, <contact>)"
  when configured, "(weewx-clearskies-api/<version>)" + one-time WARN when
  not. No project-level hardcoded fallback (ADR-006).

Wire format (PROVIDER-MANUAL §14.4):
  1. GET https://api.weather.gov/products/types/CWF/locations/{wfo} — a
     JSON-LD envelope with an `@graph` array of product stubs, most recent
     first in practice (this module always treats `@graph[0]` as "most
     recent", matching nws_srf.py's SRF handling — never assumes ordering
     beyond that). Each stub carries an `@id` URL (preferred) or an `id`
     (UUID, used to construct `{base}/products/{id}` as a fallback).
  2. GET that product URL -> `productText`, the raw CWF text.

CWF text parsing (PROVIDER-MANUAL §14.4):
  A CWF product concatenates one UGC (Universal Geographic Code) header
  segment per zone-group, e.g. "AMZ250-121115-" (zone id + 6-digit
  expiration, trailing dash), each segment terminated by a "$$" line. A
  header may abbreviate additional zones sharing identical text down to
  their 3-digit suffix, e.g. "AMZ250-256-262-121115-" means
  ["AMZ250", "AMZ256", "AMZ262"] (see `_expand_zone_codes`). The segment
  for the operator's configured `zone_id` is located, then split into
  forecast periods on `.PERIOD...` markers (e.g. ".TONIGHT...",
  ".SUN...", ".SUN NIGHT...") — unlike nws_srf.py's SRF day-period markers,
  a CWF period marker is immediately followed by its narrative on the same
  line, not a standalone header line.

  Per period, PROVIDER-MANUAL §14.4's field extraction rule ("wind
  typically starts with a compass direction and 'winds'"; "seas starts
  with 'Seas'") is applied by sentence-splitting the period narrative and
  classifying each sentence:
    - `wind`: first sentence matching `<compass> winds...` (e.g. "W winds
      10 to 15 kt with gusts up to 20 kt.").
    - `seas`: first sentence starting with "Seas" (e.g. "Seas 2 to 4
      ft."); a "Wave Detail..." sentence immediately following is folded
      into `seas` (same semantic unit in CWF text).
    - `visibility`: first sentence starting with "Visibility" (rare in CWF
      coastal-waters text; usually None).
    - `weather`: remaining, unclassified sentences (e.g. "A chance of
      showers and tstms, mainly this evening.").
  `text` always carries the full, whitespace-normalized narrative
  regardless of what could be classified — the brief's "text field carries
  the full content" guarantee holds even when wind/seas/visibility/weather
  extraction misses (unrecognized phrasing, non-English variant, etc).

Cache layer (ADR-017):
  Key: SHA-256 of {"provider_id": "nws_marine", "zone_id": zone_id} —
  unchanged from before this fix (the brief's explicit key shape,
  deliberately zone_id-only). TTL: 1800s (30 min) per PROVIDER-MANUAL
  §14.4. WFO is not part of the cache key: a zone_id maps to a stable WFO
  in practice, and the WFO lookup itself is independently cached by
  nws_zones.py.

Error handling (PROVIDER-MANUAL §14.4):
  - zone_id not in the NWS coastal zone list (no WFO can be determined) ->
    ProviderProtocolError, raised by `nws_zones.get_wfo_for_zone()`.
  - WFO has no CWF product registered (empty `@graph`, or 404 on the
    products-list/product-detail call) -> empty result, NOT an error
    (mirrors nws_srf.py's "WFO issues no SRF" handling) — logged at INFO,
    cached as `[]` for the normal TTL.
  - CWF product fetched successfully but does not contain a section for
    `zone_id` (misconfigured/stale zone_id, or a zone this WFO doesn't
    cover) -> ProviderProtocolError (the direct analog of the old
    "zone ID not found (404)" case).
  - CWF product's zone section has no parseable `.PERIOD...` markers ->
    empty result, NOT an error (logged at WARNING with the raw block).
  - Rate limit / 5xx -> retried by ProviderHTTPClient, surfaces as
    QuotaExhausted / TransientNetworkError per canonical taxonomy. Never
    re-wrapped here (rules/coding.md §1; canonical exceptions from
    ProviderHTTPClient/nws_zones.py propagate as-is).

Rate limiter: shared conceptually with the rest of api.weather.gov traffic;
a dedicated RateLimiter instance (name="nws-marine") is used here, matching
the established per-module-limiter pattern in this codebase (see
providers/forecast/nws.py's rate-limiter section for the "don't couple
domain quotas" rationale). The WFO lookup via nws_zones.get_wfo_for_zone()
uses nws_zones.py's own "nws-zones" limiter instance, not this one.

ruff: noqa: N815  (field names match NWS wire camelCase: productText, etc.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import MarineTextForecast
from weewx_clearskies_api.providers._common import nws_zones
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.errors import ProviderProtocolError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "nws_marine"
DOMAIN = "marine"
NWS_BASE_URL = "https://api.weather.gov"
_CACHE_TTL_SECONDS = 1800  # 30 min per PROVIDER-MANUAL §14.4
_API_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Capability declaration (§1 Module Contract)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        "periodName",
        "text",
        "wind",
        "seas",
        "visibility",
        "weather",
    ),
    geographic_coverage="us_coastal",
    auth_required=(),
    default_poll_interval_seconds=_CACHE_TTL_SECONDS,
    operator_notes=(
        "NWS marine zone text forecasts (CWF product). No API key required. "
        "US coastal coverage. Zone ID is discovered at setup time via the "
        "shared NWS zone discovery utility (PROVIDER-MANUAL §14.8)."
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
    name="nws-marine",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Mirrors providers/marine/nws_srf.py's product-list/product-detail models —
# same NWS `/products/types/{type}/locations/{wfo}` resource shape, just a
# different product type (CWF vs SRF).
# ---------------------------------------------------------------------------


class _NwsProductStub(BaseModel):
    """One product stub from /products/types/CWF/locations/{wfo}.

    `id` is always present (UUID). `@id` (aliased to `at_id`) is the full
    URL to the product detail resource when NWS supplies it; when absent,
    fetch() falls back to constructing the URL from `id`.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    at_id: str | None = Field(default=None, alias="@id")


class _NwsProductListResponse(BaseModel):
    """NWS /products/types/CWF/locations/{wfo} JSON-LD envelope."""

    model_config = ConfigDict(extra="ignore")

    graph: list[_NwsProductStub] = Field(default_factory=list, alias="@graph")


class _NwsProductDetail(BaseModel):
    """NWS /products/{id} JSON-LD product record — only the field this module needs."""

    model_config = ConfigDict(extra="ignore")

    id: str
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
            "NWS marine User-Agent contact is not set. "
            "Set [marine] nws_user_agent_contact = <email-or-url> in api.conf "
            "to reduce the risk of being blocked during NWS security events. "
            "See ADR-006 for the operator-managed compliance model."
        )
        _warned_missing_contact = True


# ---------------------------------------------------------------------------
# Cache key construction (brief's explicit key shape: zone_id-only)
# ---------------------------------------------------------------------------


def _build_cache_key(zone_id: str) -> str:
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "zone_id": zone_id},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# CWF product list + detail fetch (mirrors nws_srf.py's SRF fetch, different
# product type)
# ---------------------------------------------------------------------------


def _fetch_latest_cwf_product(
    wfo: str, user_agent_contact: str | None
) -> _NwsProductDetail | None:
    """Fetch the most recent CWF (Coastal Waters Forecast) product for `wfo`.

    Returns None (not an error) when the WFO has no recent CWF product —
    empty `@graph` on the list call, or 404 on the list or detail call.
    """
    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    _rate_limiter.acquire()
    list_url = f"{NWS_BASE_URL}/products/types/CWF/locations/{wfo}"
    try:
        list_response = client.get(list_url, headers={"Accept": "application/ld+json"})
    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            logger.info(
                "No CWF product type registered for WFO %s (404); returning no product.",
                wfo,
            )
            return None
        raise

    try:
        list_wire = _NwsProductListResponse.model_validate(list_response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS CWF product list response validation failed for WFO %s: %s. "
            "Response body (first 2000 chars): %.2000s",
            wfo,
            exc,
            list_response.text,
        )
        raise ProviderProtocolError(
            f"NWS CWF product list response validation failed for WFO {wfo!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    if not list_wire.graph:
        logger.info("WFO %s has no recent CWF products; returning no product.", wfo)
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
                "CWF product %s for WFO %s returned 404; returning no product.",
                product_url,
                wfo,
            )
            return None
        raise

    try:
        return _NwsProductDetail.model_validate(detail_response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS CWF product detail response validation failed for %s: %s. "
            "Response body (first 2000 chars): %.2000s",
            product_url,
            exc,
            detail_response.text,
        )
        raise ProviderProtocolError(
            f"NWS CWF product detail response validation failed for {product_url!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc


# ---------------------------------------------------------------------------
# CWF free-text parsing (module docstring "CWF text parsing")
# ---------------------------------------------------------------------------

# A UGC zone-group header line, e.g. "AMZ250-121115-" or
# "AMZ250-256-262-121115-" (multiple zones sharing identical text, later
# ones abbreviated to their 3-digit suffix — see _expand_zone_codes).
_ZONE_HEADER_RE = re.compile(r"^([A-Z]{2,3}\d{3}(?:-\d{3})*)-\d{6}-[ \t]*$", re.MULTILINE)

# A forecast-period marker, e.g. ".TONIGHT...", ".SUN...", ".SUN NIGHT...".
# Unlike nws_srf.py's SRF day-period markers, the narrative follows
# immediately on the same line rather than being a standalone header line.
_PERIOD_MARKER_RE = re.compile(r"^\.([A-Z][A-Z0-9 ]*?)\.\.\.", re.MULTILINE)

_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=\.)\s+")
_WIND_SENTENCE_RE = re.compile(r"^[A-Za-z]{1,3}(?:-[A-Za-z]{1,3})?\s+winds?\b", re.IGNORECASE)
_SEAS_SENTENCE_RE = re.compile(r"^seas\b", re.IGNORECASE)
_WAVE_DETAIL_SENTENCE_RE = re.compile(r"^wave\s+detail\b", re.IGNORECASE)
_VISIBILITY_SENTENCE_RE = re.compile(r"^visibility\b", re.IGNORECASE)


def _expand_zone_codes(codes_field: str) -> list[str]:
    """Expand a UGC header's zone-code field into full zone IDs.

    NWS UGC headers abbreviate subsequent same-type/state zone codes to
    just their 3-digit suffix, e.g. "AMZ250-256-262" means
    ["AMZ250", "AMZ256", "AMZ262"].
    """
    parts = codes_field.split("-")
    if not parts:
        return []
    expanded = [parts[0]]
    prefix_match = re.match(r"^([A-Z]{2,3})", parts[0])
    prefix = prefix_match.group(1) if prefix_match else ""
    for part in parts[1:]:
        if part.isdigit() and prefix:
            expanded.append(f"{prefix}{part}")
        else:
            expanded.append(part)
    return expanded


def _find_zone_block(product_text: str, zone_id: str) -> str | None:
    """Locate the text segment for `zone_id` within a full CWF product.

    A CWF product carries one UGC header per zone-group segment, each
    terminated by a "$$" line. Returns None if `zone_id` is not covered by
    this product (caller translates that to ProviderProtocolError — see
    module docstring "Error handling").
    """
    matches = list(_ZONE_HEADER_RE.finditer(product_text))
    for i, match in enumerate(matches):
        codes = _expand_zone_codes(match.group(1))
        if zone_id not in codes:
            continue
        block_start = match.end()
        next_header_start = matches[i + 1].start() if i + 1 < len(matches) else len(product_text)
        dollar_idx = product_text.find("$$", block_start)
        block_end = (
            min(dollar_idx, next_header_start) if dollar_idx != -1 else next_header_start
        )
        return product_text[block_start:block_end]
    return None


def _split_periods(zone_block: str) -> list[tuple[str, str]]:
    """Split a zone's text block into (period_label, period_text) pairs."""
    matches = list(_PERIOD_MARKER_RE.finditer(zone_block))
    periods: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        label = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(zone_block)
        periods.append((label, zone_block[start:end]))
    return periods


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _split_sentences(text: str) -> list[str]:
    normalized = _normalize_whitespace(text)
    if not normalized:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(normalized) if s.strip()]


def _extract_period_fields(period_text: str) -> dict[str, str | None]:
    """Extract wind/seas/visibility/weather from a period's narrative text.

    Per PROVIDER-MANUAL §14.4: wind typically starts with a compass
    direction and "winds" (e.g. "W winds 10 to 15 kt"); seas starts with
    "Seas" (e.g. "Seas 2 to 4 ft."). A "Wave Detail..." sentence
    immediately following the seas sentence is folded into `seas` (same
    semantic unit in CWF text). Remaining, unclassified sentences become
    `weather`.
    """
    sentences = _split_sentences(period_text)
    used: set[int] = set()

    wind: str | None = None
    for i, sentence in enumerate(sentences):
        if _WIND_SENTENCE_RE.match(sentence):
            wind = sentence
            used.add(i)
            break

    seas: str | None = None
    for i, sentence in enumerate(sentences):
        if i in used:
            continue
        if _SEAS_SENTENCE_RE.match(sentence):
            seas = sentence
            used.add(i)
            if i + 1 < len(sentences) and _WAVE_DETAIL_SENTENCE_RE.match(sentences[i + 1]):
                seas = f"{seas} {sentences[i + 1]}"
                used.add(i + 1)
            break

    visibility: str | None = None
    for i, sentence in enumerate(sentences):
        if i in used:
            continue
        if _VISIBILITY_SENTENCE_RE.match(sentence):
            visibility = sentence
            used.add(i)
            break

    weather_sentences = [s for i, s in enumerate(sentences) if i not in used]
    weather = " ".join(weather_sentences).strip() or None

    return {"wind": wind, "seas": seas, "visibility": visibility, "weather": weather}


def _to_canonical(period_label: str, period_text: str) -> MarineTextForecast:
    """Map one parsed (period_label, period_text) pair to a canonical MarineTextForecast."""
    fields = _extract_period_fields(period_text)
    return MarineTextForecast(
        periodName=period_label.title(),
        text=_normalize_whitespace(period_text),
        wind=fields["wind"],
        seas=fields["seas"],
        visibility=fields["visibility"],
        weather=fields["weather"],
    )


# ---------------------------------------------------------------------------
# Public fetch entrypoint
# ---------------------------------------------------------------------------


def fetch(
    *,
    zone_id: str,
    wfo_override: str | None = None,
    user_agent_contact: str | None = None,
) -> list[MarineTextForecast]:
    """Fetch NWS marine zone text forecast (CWF product) for `zone_id`.

    Args:
        zone_id: NWS coastal marine zone ID (e.g. "AMZ250").
        wfo_override: If provided, skip the zone_id -> WFO lookup and use
            this WFO code directly (mirrors providers/marine/nwps.py's
            wfo_override pattern). When None, the WFO is resolved via
            providers/_common/nws_zones.py::get_wfo_for_zone(zone_id).
        user_agent_contact: Operator-configured NWS UA contact (ADR-006).

    Returns list of MarineTextForecast periods (typically 4-8 periods
    covering ~3-5 days), or an empty list when the covering WFO has no CWF
    product, or the CWF's zone section has no parseable periods.

    Raises:
        ValueError: zone_id (or an explicitly-provided wfo_override) is empty.
        QuotaExhausted: NWS returned 429 (rate limit).
        KeyInvalid: NWS returned 401/403 (exotic; NWS is keyless).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: zone_id is not a known NWS coastal zone (no
            WFO could be determined), response validation failed (NWS
            schema change), or the fetched CWF product does not contain a
            section for zone_id.
    """
    if not zone_id or not zone_id.strip():
        raise ValueError("zone_id must not be empty")
    zone_id = zone_id.strip()
    if wfo_override is not None and not wfo_override.strip():
        raise ValueError("wfo_override must not be empty when provided")

    if not user_agent_contact:
        _warn_once_missing_contact()

    cache_key = _build_cache_key(zone_id)
    cached_dicts = get_cache().get(cache_key)
    if cached_dicts is not None:
        return [MarineTextForecast.model_validate(d) for d in cached_dicts]

    wfo = (
        wfo_override.strip()
        if wfo_override
        else nws_zones.get_wfo_for_zone(zone_id, user_agent_contact=user_agent_contact)
    )

    product = _fetch_latest_cwf_product(wfo, user_agent_contact)
    if product is None:
        get_cache().set(cache_key, [], ttl_seconds=_CACHE_TTL_SECONDS)
        return []

    zone_block = _find_zone_block(product.productText, zone_id)
    if zone_block is None:
        raise ProviderProtocolError(
            f"NWS CWF product for WFO {wfo!r} did not contain a section for "
            f"zone {zone_id!r}; the zone_id may be misconfigured or not "
            "covered by this WFO's coastal waters forecast.",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    period_blocks = _split_periods(zone_block)
    if not period_blocks:
        logger.warning(
            "NWS CWF zone %s (wfo=%s) block contained no parseable forecast "
            "periods. Raw block (first 1000 chars): %.1000s",
            zone_id,
            wfo,
            zone_block,
        )
        get_cache().set(cache_key, [], ttl_seconds=_CACHE_TTL_SECONDS)
        return []

    canonical_periods = [_to_canonical(label, text) for label, text in period_blocks]

    get_cache().set(
        cache_key,
        [period.model_dump() for period in canonical_periods],
        ttl_seconds=_CACHE_TTL_SECONDS,
    )

    logger.info(
        "NWS marine zone forecast fetched: %d period(s) for zone=%s wfo=%s",
        len(canonical_periods),
        zone_id,
        wfo,
    )
    return canonical_periods


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client, _http_client_ua, _warned_missing_contact  # noqa: PLW0603
    _http_client = None
    _http_client_ua = ""
    _warned_missing_contact = False
