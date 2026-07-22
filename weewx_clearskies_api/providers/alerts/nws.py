"""NWS alerts provider module (ADR-016, ADR-038).

Five responsibilities per ADR-038 §2:
  1. Outbound API call — NWS /alerts/active?point=<lat>,<lon>
  2. Response parsing — wire-shape Pydantic models for _NwsAlertsActiveResponse
  3. Translation to canonical AlertRecord (event-name severity tier + datetime conversion)
  4. Capability declaration — CAPABILITY symbol consumed at startup
  5. Error handling — provider errors translated to canonical taxonomy

NWS User-Agent (ADR-006, brief §confirmed-call #7):
  Operators put their own contact email/URL in api.conf:
    [alerts] nws_user_agent_contact = me@example.com
  Module composes UA as "(weewx-clearskies-api/<version>, <contact>)" when set;
  "(weewx-clearskies-api/<version>)" + one-line WARN when unset.
  NO project-level hardcoded fallback — that would put the project on the hook
  for any individual operator's traffic patterns per ADR-006.

NWS outside-coverage handling (brief §confirmed-call #6):
  No bounding-box pre-check.  NWS handles non-US ?point= queries gracefully
  (200 + empty features).  Return AlertList(alerts=[], source="nws").

Cache layer (ADR-017):
  Caches the post-normalization canonical list, not raw GeoJSON.  Saves
  re-normalization cost on cache hit.
  Key: SHA-256 of (provider_id, endpoint, {"point": "<lat4>,<lon4>"}).
  TTL: 300s (5 min per ADR-016 + ADR-017 defaults table).

Marine zone alerts (ADR-089, PROVIDER-MANUAL §8):
  NWS marine alerts (Small Craft Advisory, Gale Warning, etc.) are issued
  against marine zone polygons, not lat/lon points — a point query on land
  never matches a water polygon.  When fetch() is called with a non-empty
  marine_zone_ids list, supplemental `?zone={zoneId}` queries run after the
  point query, using the same rate limiter/client/wire models, cached under
  a separate key per zone.  Results are merged with the point query and
  de-duplicated by alert `id`.  marine_zone_ids is None/empty by default —
  zero regression to the point-only query.

Wire-shape Pydantic (security-baseline §3.5):
  _NwsAlertProperties validates every property field from the real fixture
  at tests/fixtures/providers/nws/alerts_active.json — not a synthetic subset.
  extras="ignore" so NWS schema additions don't break us; missing required
  fields raise ValidationError → ProviderProtocolError.

ruff: noqa: N815  (field names match NWS wire camelCase: areaDesc, senderName, etc.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import AlertRecord
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.datetime_utils import to_utc_iso8601_from_offset
from weewx_clearskies_api.providers._common.errors import (
    ProviderProtocolError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "nws"
DOMAIN = "alerts"
NWS_BASE_URL = "https://api.weather.gov"
NWS_ALERTS_PATH = "/alerts/active"
_NWS_CACHE_TTL = 300  # 5 minutes per ADR-016 + ADR-017

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        "id",
        "headline",
        "description",
        "urgency",
        "certainty",
        "event",
        "effective",
        "expires",
        "ends",
        "senderName",
        "areaDesc",
        "category",
        "source",
        "severityLevel",
        "severityLabel",
        "alertSystem",
    ),
    geographic_coverage="us",  # US + territories + adjacent marine zones
    auth_required=(),
    default_poll_interval_seconds=_NWS_CACHE_TTL,
    operator_notes=(
        "NWS API requires a User-Agent identifying your app; "
        "set [alerts] nws_user_agent_contact in api.conf for best results."
    ),
    refresh_interval=300,
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
# Rate limiter (ADR-038 §3)
# "Be polite" guard — 5 req/s max.  Never trips in normal use:
# with 5-min TTL + single-worker default, we make ~1 req per 5 min per station.
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="nws-alerts",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Source: docs/reference/api-docs/nws.md + tests/fixtures/providers/nws/alerts_active.json
# ---------------------------------------------------------------------------


class _NwsAlertProperties(BaseModel):
    """NWS /alerts/active feature properties — wire shape.

    Source: https://api.weather.gov/openapi.json + recorded fixture at
    tests/fixtures/providers/nws/alerts_active.json.

    extras="ignore" so NWS schema additions don't break us; missing required
    fields raise ValidationError → ProviderProtocolError.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    areaDesc: str | None = None
    sent: str | None = None
    effective: str
    onset: str | None = None
    expires: str | None = None
    ends: str | None = None
    status: str | None = None
    messageType: str | None = None
    category: str | None = None
    severity: str  # CAP enum: Extreme/Severe/Moderate/Minor/Unknown
    certainty: str | None = None
    urgency: str | None = None
    event: str
    sender: str | None = None
    senderName: str | None = None
    headline: str
    description: str | None = ""
    instruction: str | None = None
    response: str | None = None


class _NwsAlertFeature(BaseModel):
    """NWS GeoJSON Feature wrapping an alert."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    type: Literal["Feature"]
    properties: _NwsAlertProperties


class _NwsAlertsActiveResponse(BaseModel):
    """NWS /alerts/active response envelope — wire shape (GeoJSON FeatureCollection)."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["FeatureCollection"]
    features: list[_NwsAlertFeature] = Field(default_factory=list)
    title: str | None = None
    updated: str | None = None


# ---------------------------------------------------------------------------
# HTTP client (module-level singleton — one per module, not per request)
# ---------------------------------------------------------------------------

# Constructed lazily on first fetch() call to allow user-agent override.
# _http_client is keyed by user_agent string so a different UA on re-run
# gets a fresh client.
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
# User-Agent construction (ADR-006 / brief §confirmed-call #7)
# ---------------------------------------------------------------------------

_API_VERSION = "0.1.0"


def _build_user_agent(contact: str | None) -> str:
    """Build the NWS User-Agent string per ADR-006.

    Contact should be an operator email or URL from api.conf
    [alerts] nws_user_agent_contact.  When unset, a WARN is logged
    (once at module load via fetch() first call) and the contact is omitted.

    NO project-level hardcoded fallback — would put the project on the hook
    for operator traffic patterns (ADR-006).
    """
    base = f"weewx-clearskies-api/{_API_VERSION}"
    if contact and contact.strip():
        return f"({base}, {contact.strip()})"
    return f"({base})"


# Track whether we've already warned about missing contact in this process.
_warned_missing_contact = False


def _warn_once_missing_contact() -> None:
    global _warned_missing_contact  # noqa: PLW0603
    if not _warned_missing_contact:
        logger.warning(
            "NWS User-Agent contact is not set. "
            "Set [alerts] nws_user_agent_contact = <email-or-url> in api.conf "
            "to reduce the risk of being blocked during NWS security events. "
            "See ADR-006 for the operator-managed compliance model."
        )
        _warned_missing_contact = True


# ---------------------------------------------------------------------------
# Cache key construction (ADR-017 §Cache key)
# ---------------------------------------------------------------------------


def _build_cache_key(lat: float, lon: float) -> str:
    """Build a deterministic cache key for (provider_id, endpoint, {point})."""
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": NWS_ALERTS_PATH,
            "params": {"point": f"{round(lat, 4)},{round(lon, 4)}"},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_zone_cache_key(zone_id: str) -> str:
    """Build a deterministic cache key for (provider_id, endpoint, {zone}).

    Distinct from _build_cache_key (point query) so point and zone queries
    never collide (ADR-089).  Each zone gets its own cache entry so one
    zone's cache expiry doesn't force a re-fetch of the others.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": NWS_ALERTS_PATH,
            "params": {"zone": zone_id},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Datetime normalization (ADR-020)
# ---------------------------------------------------------------------------

# Shared helper lifted to providers/_common/datetime_utils.py per
# rules/coding.md §3 DRY rule; forecast/nws.py imports from there too.
# Local alias so existing call sites (_to_canonical below) are unchanged.
def _to_utc_iso8601(s: str) -> str:
    """Convert NWS timestamp (ISO-8601 with offset) → UTC ISO-8601 with Z suffix.

    Thin wrapper around the shared to_utc_iso8601_from_offset helper so
    existing call sites in this module are unchanged.  The underlying
    implementation lives at providers/_common/datetime_utils.py.
    """
    return to_utc_iso8601_from_offset(s, provider_id=PROVIDER_ID, domain=DOMAIN)


# ---------------------------------------------------------------------------
# Severity normalization (canonical-data-model §4.3)
# ---------------------------------------------------------------------------


def _normalize_severity(event: str) -> tuple[int, str]:
    """Derive severity tier from the NWS event name suffix (ADR-052).

    NWS CAP severity field (Extreme/Severe/Moderate/Minor/Unknown) is unreliable
    — the same tier can appear on events of very different severity.  The actual
    severity for NWS alerts is encoded in the event name suffix per ADR-052.

    Returns:
        (severityLevel, severityLabel) tuple where severityLevel is 1–4 ordinal
        and severityLabel is the human-readable label.

    Tiers:
        4 / "Warning"   — event name ends with " Warning"
        3 / "Watch"     — event name ends with " Watch"
        2 / "Advisory"  — event name ends with " Advisory"
        1 / "Statement" — event name ends with " Statement" or no match
    """
    if event.endswith(" Warning"):
        return (4, "Warning")
    if event.endswith(" Watch"):
        return (3, "Watch")
    if event.endswith(" Advisory"):
        return (2, "Advisory")
    if event.endswith(" Statement"):
        return (1, "Statement")
    logger.warning(
        "NWS event name %r does not end with a recognized severity suffix "
        "(Warning/Watch/Advisory/Statement); defaulting to severityLevel=1 / Statement. "
        "This may indicate a new NWS event type — check the severity derivation.",
        event,
    )
    return (1, "Statement")


# ---------------------------------------------------------------------------
# Wire → canonical normalization (canonical-data-model §4.3)
# ---------------------------------------------------------------------------


def _to_canonical(props: _NwsAlertProperties) -> AlertRecord:
    """Map NWS alert properties to a canonical AlertRecord.

    description: NWS description field with instruction appended if present.
    Per canonical-data-model §4.3: description + "\n\n" + instruction (stripped).

    Severity is derived from the event name suffix per ADR-052 (CAP severity
    field is unreliable — see module docstring and ADR-052 for rationale).
    """
    description = props.description or ""
    if props.instruction:
        description = f"{description}\n\n{props.instruction}".strip()

    severity_level, severity_label = _normalize_severity(props.event)

    return AlertRecord(
        id=props.id,
        headline=props.headline,
        description=description,
        urgency=props.urgency,
        certainty=props.certainty,
        event=props.event,
        effective=_to_utc_iso8601(props.effective),
        expires=_to_utc_iso8601(props.expires) if props.expires else None,
        ends=_to_utc_iso8601(props.ends) if props.ends else None,
        senderName=props.senderName,
        areaDesc=props.areaDesc,
        category=props.category,
        source=PROVIDER_ID,
        severityLevel=severity_level,
        severityLabel=severity_label,
        alertSystem="nws",
        hazardType=None,
        nativeName=None,
        color=None,
    )


# ---------------------------------------------------------------------------
# Marine zone query helper (ADR-089)
# ---------------------------------------------------------------------------


def _fetch_zone_alerts(zone_id: str, client: ProviderHTTPClient) -> list[AlertRecord]:
    """Fetch, cache, and normalize alerts for a single NWS marine zone.

    Mirrors the point-query flow in fetch() but keyed/cached separately
    (ADR-089 §Cache key).  Uses the same rate limiter, HTTP client, wire
    models, and normalization as the point query — no new infrastructure.

    Raises the same canonical exceptions as fetch() (propagated, not
    re-wrapped, from ProviderHTTPClient / response validation).
    """
    zone_cache_key = _build_zone_cache_key(zone_id)
    cached_dicts = get_cache().get(zone_cache_key)
    if cached_dicts is not None:
        return [AlertRecord.model_validate(d) for d in cached_dicts]

    _rate_limiter.acquire()

    response = client.get(
        f"{NWS_BASE_URL}{NWS_ALERTS_PATH}",
        params={"zone": zone_id},
        headers={"Accept": "application/geo+json"},
    )

    try:
        wire = _NwsAlertsActiveResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS zone response validation failed for zone=%s: %s. "
            "Response body (first 2000 chars): %.2000s",
            zone_id,
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"NWS zone response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    zone_records = [_to_canonical(feature.properties) for feature in wire.features]

    get_cache().set(
        zone_cache_key,
        [record.model_dump() for record in zone_records],
        ttl_seconds=_NWS_CACHE_TTL,
    )

    return zone_records


def _deduplicate_by_id(records: list[AlertRecord]) -> list[AlertRecord]:
    """De-duplicate AlertRecord list by canonical `id` field, keeping first occurrence.

    The same alert can appear in both the point query and a zone query
    (e.g. a Coastal Flood Warning that touches both a land public zone and
    an overlapping marine zone).  Order-preserving so point-query results
    (which come first) win ties (ADR-089).
    """
    seen_ids: set[str] = set()
    unique_records: list[AlertRecord] = []
    for record in records:
        if record.id not in seen_ids:
            seen_ids.add(record.id)
            unique_records.append(record)
    return unique_records


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    user_agent_contact: str | None,
    marine_zone_ids: list[str] | None = None,
) -> list[AlertRecord]:
    """Call NWS /alerts/active and return canonical AlertRecord models.

    Cache stores post-normalization dicts (JSON-serialisable for Redis per
    ADR-017); on cache hit the dicts are reconstructed into AlertRecord models
    before returning.  Callers always receive list[AlertRecord].

    Marine zone supplementation (ADR-089): when marine_zone_ids is a non-empty
    list, additional `?zone={zoneId}` queries are made for each configured
    NWS marine zone (Small Craft Advisory, Gale Warning, etc. are issued
    against marine zone polygons, not point coordinates — a point query on
    land never matches a water polygon).  Results are merged with the
    point-based results and de-duplicated by alert `id`.  When
    marine_zone_ids is None or empty, behavior is identical to the
    point-only query (zero regression).

    Returns:
        List of canonical AlertRecord models, possibly empty.

    Raises:
        QuotaExhausted: NWS returned 429 (rate limit).
        KeyInvalid: NWS returned 401/403 (exotic; NWS is keyless).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed (NWS schema change).
    """
    if not user_agent_contact:
        _warn_once_missing_contact()

    cache_key = _build_cache_key(lat, lon)
    cached_dicts = get_cache().get(cache_key)
    if cached_dicts is not None:
        # Cache always stores list[dict] (post model_dump()); reconstruct models.
        point_records = [AlertRecord.model_validate(d) for d in cached_dicts]
    else:
        _rate_limiter.acquire()

        user_agent = _build_user_agent(user_agent_contact)
        client = _get_http_client(user_agent)

        point_str = f"{round(lat, 4)},{round(lon, 4)}"
        response = client.get(
            f"{NWS_BASE_URL}{NWS_ALERTS_PATH}",
            params={"point": point_str},
            headers={"Accept": "application/geo+json"},
        )

        try:
            wire = _NwsAlertsActiveResponse.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            logger.error(
                "NWS response validation failed: %s. Response body (first 2000 chars): %.2000s",
                exc,
                response.text,
            )
            raise ProviderProtocolError(
                f"NWS response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        point_records = [_to_canonical(feature.properties) for feature in wire.features]

        # Store as list of dicts for JSON-serialisable caching (ADR-017 §Decision).
        get_cache().set(
            cache_key,
            [record.model_dump() for record in point_records],
            ttl_seconds=_NWS_CACHE_TTL,
        )

        logger.info(
            "NWS alerts fetched: %d alert(s) for point=%s",
            len(point_records),
            point_str,
        )

    if not marine_zone_ids:
        return point_records

    # Marine zone supplementation (ADR-089).  Same client/UA as the point
    # query above; construct if the point query hit cache and skipped it.
    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    zone_records: list[AlertRecord] = []
    for zone_id in marine_zone_ids:
        zone_records.extend(_fetch_zone_alerts(zone_id, client))

    logger.info(
        "NWS marine zone alerts fetched: %d alert(s) from %d zone(s)",
        len(zone_records),
        len(marine_zone_ids),
    )

    return _deduplicate_by_id(point_records + zone_records)


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client, _http_client_ua, _warned_missing_contact  # noqa: PLW0603
    _http_client = None
    _http_client_ua = ""
    _warned_missing_contact = False
