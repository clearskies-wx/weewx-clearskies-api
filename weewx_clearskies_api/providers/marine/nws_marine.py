"""NWS marine zone text forecast provider module (ADR-083, PROVIDER-MANUAL §14.4).

Five responsibilities per the §1 Module Contract (PROVIDER-MANUAL):
  1. Outbound API call — NWS /zones/coastal/{zoneId}/forecast
  2. Response parsing — wire-shape Pydantic model for the periods list
  3. Translation to canonical MarineTextForecast (models/responses.py)
  4. Capability declaration — CAPABILITY symbol consumed at startup
  5. Error handling — provider errors translated to canonical taxonomy

Zone ID source (PROVIDER-MANUAL §14.4):
  The zone ID comes from the operator's marine location configuration
  (`nws_marine_zone_id` field), discovered at setup time by the shared
  utility at providers/_common/nws_zones.py (§14.8). This module does not
  itself perform zone discovery — it only fetches the forecast text for an
  already-known zone_id.

NWS User-Agent (ADR-006):
  Same construction pattern as providers/alerts/nws.py and
  providers/forecast/nws.py: "(weewx-clearskies-api/<version>, <contact>)"
  when configured, "(weewx-clearskies-api/<version>)" + one-time WARN when
  not. No project-level hardcoded fallback (ADR-006).

Wire format (PROVIDER-MANUAL §14.4):
  GET https://api.weather.gov/zones/coastal/{zoneId}/forecast
  Response is JSON-LD/GeoJSON. properties.periods[] periods carry, at
  minimum, `name` and `detailedForecast` — a live fixture capture against a
  land public zone (which shares the same /zones/{type}/{zoneId}/forecast
  resource shape) showed periods with only number/name/detailedForecast
  populated; windSpeed/windDirection are present on some zone types but not
  guaranteed for marine zones, matching the manual's note that wind/seas/
  visibility/weather must be derived from the narrative text or structured
  fields "when available" — never assumed present.

Normalization (PROVIDER-MANUAL §14.4, API-MANUAL §16 MarineTextForecast):
  name              -> periodName
  detailedForecast  -> text
  wind              -> composed from windDirection + windSpeed when NWS
                       supplies both structured fields; otherwise None (the
                       full narrative is still available via `text`).
  seas/visibility/weather -> None (no reliable structured source on this
                       endpoint; narrative text in `text` carries the content).

Cache layer (ADR-017):
  Key: SHA-256 of {"provider_id": "nws_marine", "zone_id": zone_id} — per
  the brief's explicit key shape (deliberately zone_id-only, no wrapping
  "endpoint" field, since this module has exactly one outbound call shape).
  TTL: 1800s (30 min) per PROVIDER-MANUAL §14.4.

Error handling (PROVIDER-MANUAL §14.4):
  Zone ID not found (404) -> ProviderProtocolError. ProviderHTTPClient
  already performs this translation for generic 4xx responses — no
  interception/re-wrap here (canonical exceptions from ProviderHTTPClient
  are never re-constructed; see providers/_common/errors.py and
  rules/coding.md).
  Rate limit / 5xx -> retried by ProviderHTTPClient, surfaces as
  QuotaExhausted / TransientNetworkError per canonical taxonomy.

Rate limiter: shared conceptually with the rest of api.weather.gov traffic;
a dedicated RateLimiter instance (name="nws-marine") is used here, matching
the established per-module-limiter pattern in this codebase (see
providers/forecast/nws.py's rate-limiter section for the "don't couple
domain quotas" rationale).

ruff: noqa: N815  (field names match NWS wire camelCase: detailedForecast, windSpeed, etc.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import MarineTextForecast
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
        "NWS marine zone text forecasts. No API key required. "
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
# Source: docs/reference/api-docs (zone forecast shares the /zones/{type}/
# {zoneId}/forecast resource with land public zones) + live fixture capture.
# ---------------------------------------------------------------------------


class _NwsMarinePeriod(BaseModel):
    """One period from /zones/coastal/{zoneId}/forecast.

    Only `name` and `detailedForecast` are guaranteed present.
    windSpeed/windDirection are opportunistic — populated only when NWS
    supplies structured wind fields for this zone/period.
    """

    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    name: str
    detailedForecast: str
    windSpeed: str | None = None
    windDirection: str | None = None


class _NwsMarineForecastProperties(BaseModel):
    """Properties block of the NWS zone forecast GeoJSON feature."""

    model_config = ConfigDict(extra="ignore")

    periods: list[_NwsMarinePeriod] = Field(default_factory=list)


class _NwsMarineForecastResponse(BaseModel):
    """NWS /zones/coastal/{zoneId}/forecast GeoJSON Feature envelope."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["Feature"]
    properties: _NwsMarineForecastProperties


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
# Wire -> canonical normalization (API-MANUAL §16 MarineTextForecast)
# ---------------------------------------------------------------------------


def _to_canonical(period: _NwsMarinePeriod) -> MarineTextForecast:
    """Map one NWS zone-forecast period to a canonical MarineTextForecast.

    wind: composed from windDirection + windSpeed only when NWS supplies
    both structured fields for this period; otherwise None — the full
    narrative is still available via `text` (PROVIDER-MANUAL §14.4).
    seas/visibility/weather: no reliable structured source on this endpoint;
    left None per the brief ("the text field carries the full content").
    """
    wind: str | None = None
    if period.windDirection and period.windSpeed:
        wind = f"{period.windDirection} {period.windSpeed}".strip()
    elif period.windSpeed:
        wind = period.windSpeed

    return MarineTextForecast(
        periodName=period.name,
        text=period.detailedForecast,
        wind=wind,
        seas=None,
        visibility=None,
        weather=None,
    )


# ---------------------------------------------------------------------------
# Public fetch entrypoint
# ---------------------------------------------------------------------------


def fetch(
    *,
    zone_id: str,
    user_agent_contact: str | None = None,
) -> list[MarineTextForecast]:
    """Fetch NWS marine zone text forecast.

    Returns list of MarineTextForecast periods (typically 6-8 periods
    covering ~3 days).

    Raises:
        QuotaExhausted: NWS returned 429 (rate limit).
        KeyInvalid: NWS returned 401/403 (exotic; NWS is keyless).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Zone ID not found (404) or response
            validation failed (NWS schema change).
    """
    if not user_agent_contact:
        _warn_once_missing_contact()

    cache_key = _build_cache_key(zone_id)
    cached_dicts = get_cache().get(cache_key)
    if cached_dicts is not None:
        return [MarineTextForecast.model_validate(d) for d in cached_dicts]

    _rate_limiter.acquire()

    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    url = f"{NWS_BASE_URL}/zones/coastal/{zone_id}/forecast"
    response = client.get(
        url,
        headers={"Accept": "application/geo+json"},
    )

    try:
        wire = _NwsMarineForecastResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS marine zone forecast response validation failed for zone %s: %s. "
            "Response body (first 2000 chars): %.2000s",
            zone_id,
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"NWS marine zone forecast response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    canonical_periods = [_to_canonical(period) for period in wire.properties.periods]

    get_cache().set(
        cache_key,
        [period.model_dump() for period in canonical_periods],
        ttl_seconds=_CACHE_TTL_SECONDS,
    )

    logger.info(
        "NWS marine zone forecast fetched: %d period(s) for zone=%s",
        len(canonical_periods),
        zone_id,
    )
    return canonical_periods


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client, _http_client_ua, _warned_missing_contact  # noqa: PLW0603
    _http_client = None
    _http_client_ua = ""
    _warned_missing_contact = False
