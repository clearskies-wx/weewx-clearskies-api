"""NWS alerts provider module (ADR-016, ADR-038).

Five responsibilities per ADR-038 §2:
  1. Outbound API call — NWS /alerts/active?point=<lat>,<lon>
  2. Response parsing — wire-shape Pydantic models for _NwsAlertsActiveResponse
  3. Translation to canonical AlertRecord (severity map + datetime conversion)
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
from datetime import UTC, datetime
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import AlertRecord
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import ProviderCapability
from weewx_clearskies_api.providers._common.errors import (
    ProviderProtocolError,
    TransientNetworkError,
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
        "severity",
        "urgency",
        "certainty",
        "event",
        "effective",
        "expires",
        "senderName",
        "areaDesc",
        "category",
        "source",
    ),
    geographic_coverage="us",  # US + territories + adjacent marine zones
    auth_required=(),
    default_poll_interval_seconds=_NWS_CACHE_TTL,
    operator_notes=(
        "NWS API requires a User-Agent identifying your app; "
        "set [alerts] nws_user_agent_contact in api.conf for best results."
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
# Severity normalization map (canonical-data-model §4.3)
# ---------------------------------------------------------------------------

_NWS_SEVERITY_MAP: dict[str, str] = {
    "Extreme": "warning",
    "Severe": "watch",
    "Moderate": "advisory",
    "Minor": "advisory",
    "Unknown": "advisory",
}

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
    type: str | None = None
    properties: _NwsAlertProperties


class _NwsAlertsActiveResponse(BaseModel):
    """NWS /alerts/active response envelope — wire shape (GeoJSON FeatureCollection)."""

    model_config = ConfigDict(extra="ignore")

    type: str
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


# ---------------------------------------------------------------------------
# Datetime normalization (ADR-020)
# ---------------------------------------------------------------------------


def _to_utc_iso8601(s: str) -> str:
    """Convert NWS timestamp (ISO-8601 with offset) → UTC ISO-8601 with Z suffix.

    NWS always emits timestamps with a timezone offset (e.g. 2026-04-30T16:00:00-07:00).
    ADR-020 mandates UTC ISO-8601 with explicit Z on the wire.

    Raises:
        ProviderProtocolError: Timestamp can't be parsed or has no timezone offset.
    """
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ProviderProtocolError(
            f"NWS timestamp parse failed for {s!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc
    if dt.tzinfo is None:
        # NWS always emits offset; bare-naive is a protocol violation.
        raise ProviderProtocolError(
            f"NWS timestamp {s!r} has no timezone offset",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Severity normalization (canonical-data-model §4.3)
# ---------------------------------------------------------------------------


def _normalize_severity(nws_severity: str) -> str:
    """Map NWS CAP severity to canonical {advisory, watch, warning}.

    Unknown values default to 'advisory' (least severe canonical) per the §4.3
    mapping table.  Logs at WARNING so a future NWS schema change surfaces in
    operator logs without breaking the response.
    """
    canonical = _NWS_SEVERITY_MAP.get(nws_severity)
    if canonical is None:
        logger.warning(
            "Unknown NWS CAP severity %r; defaulting to 'advisory'. "
            "This may indicate a NWS schema change — check the severity mapping.",
            nws_severity,
        )
        return "advisory"
    return canonical


# ---------------------------------------------------------------------------
# Wire → canonical normalization (canonical-data-model §4.3)
# ---------------------------------------------------------------------------


def _to_canonical(props: _NwsAlertProperties) -> AlertRecord:
    """Map NWS alert properties to a canonical AlertRecord.

    description: NWS description field with instruction appended if present.
    Per canonical-data-model §4.3: description + "\n\n" + instruction (stripped).
    """
    description = props.description or ""
    if props.instruction:
        description = f"{description}\n\n{props.instruction}".strip()

    return AlertRecord(
        id=props.id,
        headline=props.headline,
        description=description,
        severity=_normalize_severity(props.severity),
        urgency=props.urgency,
        certainty=props.certainty,
        event=props.event,
        effective=_to_utc_iso8601(props.effective),
        expires=_to_utc_iso8601(props.expires) if props.expires else None,
        senderName=props.senderName,
        areaDesc=props.areaDesc,
        category=props.category,
        source=PROVIDER_ID,
    )


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    user_agent_contact: str | None,
) -> list[dict]:  # type: ignore[type-arg]
    """Call NWS /alerts/active and return canonical AlertRecord dicts.

    Return type is list[dict] for JSON-serialisability across both cache
    backends (ADR-017): MemoryCache stores dicts as-is; RedisCache serialises
    via JSON. Callers (endpoint) reconstruct AlertRecord objects with
    AlertRecord.model_validate(d).

    Returns:
        List of canonical AlertRecord dicts, possibly empty.

    Raises:
        QuotaExhausted: NWS returned 429 (rate limit).
        KeyInvalid: NWS returned 401/403 (exotic; NWS is keyless).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed (NWS schema change).
    """
    if not user_agent_contact:
        _warn_once_missing_contact()

    cache_key = _build_cache_key(lat, lon)
    cached = get_cache().get(cache_key)
    if cached is not None:
        # MemoryCache may store list[AlertRecord] when tests pre-populate the cache.
        # Normalise to list[dict] for uniform return type.
        return [
            item.model_dump() if isinstance(item, AlertRecord) else item
            for item in cached
        ]

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

    canonical_records = [_to_canonical(feature.properties) for feature in wire.features]

    # Store as list of dicts for JSON-serialisable caching (ADR-017 §Decision).
    canonical_dicts = [r.model_dump() for r in canonical_records]
    get_cache().set(cache_key, canonical_dicts, ttl_seconds=_NWS_CACHE_TTL)

    logger.info(
        "NWS alerts fetched: %d alert(s) for point=%s",
        len(canonical_records),
        point_str,
    )
    return canonical_dicts


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client, _http_client_ua, _warned_missing_contact  # noqa: PLW0603
    _http_client = None
    _http_client_ua = ""
    _warned_missing_contact = False
