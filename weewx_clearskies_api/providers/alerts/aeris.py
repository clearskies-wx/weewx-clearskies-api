"""Aeris (AerisWeather/Xweather) alerts provider module (ADR-016, ADR-038).

Five responsibilities per ADR-038 §2:
  1. Outbound API call — single GET per cache miss:
       GET /alerts/{lat},{lon}?client_id=...&client_secret=...
  2. Response parsing — wire-shape Pydantic models for the
     success/error/response[] envelope and the alert-detail body.
  3. Translation to canonical AlertRecord (severity-priority-int map +
     datetime conversion + senderName disjunction).
  4. Capability declaration — CAPABILITY symbol consumed at startup.
  5. Error handling — provider errors translated to canonical taxonomy via
     ProviderHTTPClient.get() (no narrow wraps; L2 rule from 3b-4 audit F1).

Aeris is a keyed provider (ADR-006):
  client_id + client_secret passed as query params on every request.
  Sourced from env vars WEEWX_CLEARSKIES_AERIS_CLIENT_ID +
  WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET at startup (ADR-027 §3).
  Provider-scoped per brief Q1 user decision 2026-05-08; same key works for
  forecast + alerts.

Aeris /alerts returns active alerts only:
  api-docs §Alerts notes: "/alerts returns latest alerts only. For history
  use the (separate) archive endpoints."  No client-side active=true filter.

Envelope parsing:
  Aeris uses the same success/error/response[] envelope across ALL endpoints.
  success=false → ProviderProtocolError.
  success=true with warning → log WARNING + return empty list
    (e.g. warn_location for an off-grid lat/lon).
  _AerisEnvelope + _parse_aeris_envelope_raw duplicated from
  providers/forecast/aeris.py per brief lead-call 21 (out-of-scope to
  promote to shared module; if a third Aeris-domain module lands, then
  promote).

Cache layer (ADR-017):
  Caches post-normalization canonical list, not raw JSON.
  Key: SHA-256 of (provider_id, endpoint="alerts", {lat4, lon4}).
  TTL: 300s (5 min per ADR-016 + ADR-017 defaults table).
  No target_unit dimension — alerts have no unit conversion.
  Stores [record.model_dump() for record in records] (list of dicts —
  JSON-serializable for Redis per ADR-017).

senderName disjunction (brief call 19, Q2 user decision 2026-05-09):
  Prefer details.emergency when non-empty string; else place.name when
  present; else None. Canonical §3.6 senderName is nullable; canonical wins.

Severity mapping (brief call 12, canonical-data-model §4.3):
  details.priority integer → canonical enum:
    1 → "warning" (Extreme), 2 → "watch" (Severe), 3-5 → "advisory".
  Unknown integer → "advisory" with WARNING log.

Description (brief call 13):
  details.body straight passthrough. No NWS-style instruction-append.

Datetime conversion (brief call 14):
  Use ISO form (issuedISO / expiresISO) + to_utc_iso8601_from_offset()
  from _common/datetime_utils.py (DRY — already used by forecast modules).

L2 carry-forward (3b-4 audit F1): bare client.get() calls; no narrow wraps.
  ProviderHTTPClient.get() raises canonical taxonomy exceptions (KeyInvalid,
  QuotaExhausted, TransientNetworkError, ProviderProtocolError) with all
  structured attributes set (status_code, retry_after_seconds).
  Catching to re-construct silently drops attributes. Don't do it.

ruff: noqa: N815  (field names match Aeris camelCase: issuedISO, expiresISO, etc.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import AlertRecord
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import ProviderCapability
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
DOMAIN = "alerts"
AERIS_BASE_URL = "https://data.api.xweather.com"
_AERIS_CACHE_TTL = 300  # 5 minutes per ADR-016 + ADR-017

_API_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        # Paid-tier-max-surface per brief lead-call 16 (L1 rule).
        # urgency/certainty/category declared even if real fixture lacks them;
        # lead handles PARTIAL-DOMAIN at audit time if absent.
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
        # source is provider_id literal (canonical §3.6 field), not a fetched wire field.
        "source",
    ),
    geographic_coverage="us-ca-eu",  # ADR-016 day-1 set table column (US + Canada + Europe)
    auth_required=("client_id", "client_secret"),
    default_poll_interval_seconds=_AERIS_CACHE_TTL,
    operator_notes=(
        "Aeris (AerisWeather/Xweather) alerts. Requires client_id + client_secret "
        "bound to a registered domain or bundle id "
        "(see docs/reference/api-docs/aeris.md §Authentication). "
        "Returns active alerts only per Aeris api-docs §Alerts. "
        "warn_location responses (off-grid lat/lon) return empty list. "
        "Coverage per ADR-016 day-1 table: US + Canada + Europe (NWS + Environment "
        "Canada + MeteoAlarm + UK Met redistributed)."
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3, brief lead-call 11)
# "Be polite" guard — 5 req/s max. With 5-min TTL + single-worker default,
# never trips in normal use.
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="aeris-alerts",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Severity normalization map (canonical-data-model §4.3, brief lead-call 12)
# Aeris details.priority integer → canonical enum.
# ---------------------------------------------------------------------------

_AERIS_SEVERITY_MAP: dict[int, str] = {
    1: "warning",   # Extreme
    2: "watch",     # Severe
    3: "advisory",  # Moderate
    4: "advisory",  # Minor
    5: "advisory",  # Unknown/Other
}

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Source: docs/reference/api-docs/aeris.md §Alerts + brief §per-module spec
# extras="ignore" so Aeris additions don't break us; missing required fields
# raise ValidationError → translated to ProviderProtocolError.
# ---------------------------------------------------------------------------


class _AerisAlertDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str | None = None              # event short code ("WIN", "TOR") — maps to event
    name: str | None = None              # human-readable headline
    loc: str | None = None
    priority: int | None = None          # severity, mapped via _AERIS_SEVERITY_MAP
    color: str | None = None
    body: str | None = None              # description passthrough (brief call 13)
    # emergency: real Aeris wire returns boolean False when no emergency text is set.
    # Type declared as bool | str | None to accept both wire forms.
    # Brief spec said str | None; real-capture fixture (alerts.md) showed boolean False.
    # senderName logic in _to_canonical treats any falsy value as "use place.name fallback".
    emergency: bool | str | None = None  # senderName primary candidate (brief call 19, Q2)
    urgency: str | None = None           # CAP vocab passthrough (may be absent — call 16)
    certainty: str | None = None         # CAP vocab passthrough (may be absent — call 16)
    category: str | None = None          # CAP vocab passthrough (may be absent — call 16)


class _AerisAlertTimestamps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    issued: int | None = None
    issuedISO: str | None = None         # used for effective (brief call 14)
    expires: int | None = None
    expiresISO: str | None = None        # used for expires (brief call 14)
    begins: int | None = None            # not in canonical mapping
    beginsISO: str | None = None
    updated: int | None = None
    updatedISO: str | None = None


class _AerisAlertPlace(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None              # areaDesc + senderName fallback (brief call 19)
    state: str | None = None
    country: str | None = None


class _AerisAlertRecord(BaseModel):
    """One alert from response[]."""

    model_config = ConfigDict(extra="ignore")

    id: str
    dataSource: str | None = None        # not in canonical mapping; preserved for debug log
    active: bool | None = None
    details: _AerisAlertDetails
    timestamps: _AerisAlertTimestamps
    place: _AerisAlertPlace | None = None


class _AerisEnvelope(BaseModel):
    """Aeris response envelope — same shape as forecast/aeris.py.

    Duplicated per brief lead-call 21 (out-of-scope to promote to shared
    module; if a third Aeris-domain module lands, then promote).
    """

    model_config = ConfigDict(extra="ignore")

    success: bool
    error: dict[str, Any] | None = None
    # response is a list of alert objects (one per matching alert)
    response: list[dict[str, Any]] = Field(default_factory=list)


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


def _build_cache_key(lat: float, lon: float) -> str:
    """Build a deterministic cache key for (provider_id, endpoint, {lat, lon}).

    No target_unit dimension — alerts have no unit conversion.
    Lat/lon rounded to 4 decimal places per ADR-017.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "alerts",
            "params": {
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Envelope parsing helper (duplicated from forecast/aeris.py per brief call 21)
# ---------------------------------------------------------------------------


def _parse_aeris_envelope_raw(response: Any) -> list[dict[str, Any]]:
    """Parse the Aeris success/error envelope and return the raw response list.

    On success=false: raises ProviderProtocolError.
    On success=true with warn_location: logs WARNING and returns empty list
      (caller returns empty alert list; matches NWS outside-coverage handling).
    On success=true with response=[]: returns empty list.

    Raises:
        ProviderProtocolError: success=false or envelope parse failure.
    """
    try:
        envelope = _AerisEnvelope.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "Aeris alerts envelope parse failed: %s. Body (first 2000 chars): %.2000s",
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"Aeris alerts envelope parse failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    if not envelope.success:
        error_code = ""
        error_desc = "unknown error"
        if envelope.error:
            error_code = envelope.error.get("code", "")
            error_desc = envelope.error.get("description", "unknown error")
        raise ProviderProtocolError(
            f"Aeris alerts returned success=false: code={error_code!r} {error_desc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    # success=true with a warning (e.g. warn_location) — log and return empty
    if envelope.error:
        warn_code = envelope.error.get("code", "")
        warn_desc = envelope.error.get("description", "")
        logger.warning(
            "Aeris alerts returned success=true with warning: code=%r %s",
            warn_code,
            warn_desc,
        )
        return []

    return envelope.response


# ---------------------------------------------------------------------------
# Severity normalization (canonical-data-model §4.3, brief lead-call 12)
# ---------------------------------------------------------------------------


def _normalize_severity(priority: int | None) -> str:
    """Map Aeris details.priority integer to canonical {advisory, watch, warning}.

    Unknown values default to 'advisory' (least-severe canonical) per NWS
    precedent in 3b-1.  Logs at WARNING so a future Aeris schema change
    surfaces in operator logs without breaking the response.
    """
    if priority is None:
        logger.warning(
            "Aeris alert has null priority; defaulting to 'advisory'. "
            "This may indicate a schema change — check the severity mapping.",
        )
        return "advisory"
    canonical = _AERIS_SEVERITY_MAP.get(priority)
    if canonical is None:
        logger.warning(
            "Unknown Aeris priority %r; defaulting to 'advisory'. "
            "This may indicate a schema change — check the severity mapping.",
            priority,
        )
        return "advisory"
    return canonical


# ---------------------------------------------------------------------------
# Wire → canonical normalization (canonical-data-model §4.3)
# ---------------------------------------------------------------------------


def _to_canonical(record: _AerisAlertRecord) -> AlertRecord:
    """Map one Aeris alert record to a canonical AlertRecord.

    Field mapping per canonical-data-model §4.3:
      id = id (top-level)
      headline = details.name
      description = details.body (passthrough, no append — brief call 13)
      severity = details.priority via _AERIS_SEVERITY_MAP (brief call 12)
      urgency = details.urgency (CAP vocab passthrough)
      certainty = details.certainty (CAP vocab passthrough)
      event = details.type (short code, passthrough — operationalization call)
      effective = timestamps.issuedISO via to_utc_iso8601_from_offset (call 14)
      expires = timestamps.expiresISO via to_utc_iso8601_from_offset (call 14)
      senderName = details.emergency or place.name or None (call 19, Q2)
      areaDesc = place.name (passthrough)
      category = details.category (CAP vocab passthrough)
      source = "aeris" (provider_id literal)
    """
    # Effective timestamp: use ISO form for offset-aware UTC conversion
    effective: str | None = None
    if record.timestamps.issuedISO:
        effective = to_utc_iso8601_from_offset(
            record.timestamps.issuedISO, provider_id=PROVIDER_ID, domain=DOMAIN
        )
    else:
        # issuedISO absent — fallback to epoch seconds as UTC ISO string
        # (defensive; real Aeris responses always include issuedISO)
        logger.warning(
            "Aeris alert %r has no issuedISO; effective will be None",
            record.id,
        )

    # Expires timestamp
    expires: str | None = None
    if record.timestamps.expiresISO:
        expires = to_utc_iso8601_from_offset(
            record.timestamps.expiresISO, provider_id=PROVIDER_ID, domain=DOMAIN
        )

    # senderName disjunction (brief call 19, Q2 user decision 2026-05-09):
    # prefer details.emergency when non-empty string; else place.name; else None.
    # emergency may be: a non-empty string (use it), an empty string/None/False boolean
    # (real wire returns False when no emergency text — treat as absent), or a truthy
    # string that is all-whitespace (strip and treat as absent).
    sender_name: str | None = None
    emergency = record.details.emergency
    if isinstance(emergency, str) and emergency.strip():
        sender_name = emergency.strip()
    elif record.place and record.place.name and record.place.name.strip():
        sender_name = record.place.name.strip()

    # areaDesc = place.name passthrough
    area_desc: str | None = None
    if record.place and record.place.name:
        area_desc = record.place.name

    return AlertRecord(
        id=record.id,
        headline=record.details.name or "",
        description=record.details.body or "",
        severity=_normalize_severity(record.details.priority),
        urgency=record.details.urgency,
        certainty=record.details.certainty,
        event=record.details.type or "",
        effective=effective or "",
        expires=expires,
        senderName=sender_name,
        areaDesc=area_desc,
        category=record.details.category,
        source=PROVIDER_ID,
    )


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    client_id: str | None,
    client_secret: str | None,
) -> list[AlertRecord]:
    """GET /alerts/{lat,lon} and return canonical AlertRecord list.

    Cache-first: check cache before making outbound HTTP call.
    Cache stores post-normalization list[dict] (JSON-serializable for Redis
    per ADR-017); reconstructed into list[AlertRecord] on cache hit.

    KeyInvalid early-raise when credentials missing (brief call 8):
    Loud failure beats silent disable; operator intent to enable Aeris alerts
    is unambiguous when [alerts] provider = aeris.

    Bare client.get() — let canonical exceptions propagate (L2 carry-forward,
    3b-4 audit F1): ProviderHTTPClient.get() raises KeyInvalid, QuotaExhausted,
    TransientNetworkError, ProviderProtocolError with all structured attributes
    set. Catching to re-construct silently drops retry_after_seconds.

    Args:
        lat: Station latitude from services/station.py StationInfo.
        lon: Station longitude from services/station.py StationInfo.
        client_id: Aeris client_id from env var WEEWX_CLEARSKIES_AERIS_CLIENT_ID.
            None if operator hasn't configured it.
        client_secret: Aeris client_secret from env var
            WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET. None if not configured.

    Returns:
        List of canonical AlertRecord models, possibly empty.

    Raises:
        KeyInvalid: Credentials missing (both args None/empty) or 401/403.
        QuotaExhausted: Aeris returned 429.
        ProviderProtocolError: Response validation failed or success=false envelope.
        TransientNetworkError: Network failure / 5xx after retries.
    """
    # Validate credentials before any HTTP call (brief call 8).
    # Loud failure beats silent disable — operator intent is unambiguous.
    if not client_id or not client_secret:
        raise KeyInvalid(
            "Aeris alerts credentials missing — set WEEWX_CLEARSKIES_AERIS_CLIENT_ID "
            "and WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET env vars",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    cache_key = _build_cache_key(lat, lon)
    cached_dicts = get_cache().get(cache_key)
    if cached_dicts is not None:
        logger.debug(
            "Cache hit for Aeris alerts",
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        # Cache always stores list[dict] (post model_dump()); reconstruct models.
        return [AlertRecord.model_validate(d) for d in cached_dicts]

    logger.debug(
        "Cache miss for Aeris alerts; calling API",
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )

    _rate_limiter.acquire()

    location = f"{round(lat, 4)},{round(lon, 4)}"
    url = f"{AERIS_BASE_URL}/alerts/{location}"
    params = {
        "client_id": client_id,
        "client_secret": client_secret,
    }

    # ProviderHTTPClient.get() raises canonical taxonomy exceptions with all
    # structured attributes set. Let them propagate — do NOT re-wrap (L2 rule,
    # 3b-4 audit F1: re-construction dropped retry_after_seconds from QuotaExhausted).
    client = _client_for()
    response = client.get(url, params=params)

    # Parse envelope and extract raw alert list
    raw_alert_list = _parse_aeris_envelope_raw(response)

    if not raw_alert_list:
        # Empty response (no alerts) or warn_location — return empty canonical list.
        logger.info(
            "Aeris alerts: no active alerts for %s",
            location,
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        get_cache().set(cache_key, [], ttl_seconds=_AERIS_CACHE_TTL)
        return []

    # Validate each alert record against the wire-shape model
    canonical_records: list[AlertRecord] = []
    for raw_record in raw_alert_list:
        try:
            wire_record = _AerisAlertRecord.model_validate(raw_record)
        except ValidationError as exc:
            logger.error(
                "Aeris alert record validation failed: %s. Record (first 500 chars): %.500s",
                exc,
                str(raw_record),
            )
            raise ProviderProtocolError(
                f"Aeris alert record validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc
        canonical_records.append(_to_canonical(wire_record))

    # Store as list of dicts for JSON-serializable caching (ADR-017 §Decision).
    get_cache().set(
        cache_key,
        [record.model_dump() for record in canonical_records],
        ttl_seconds=_AERIS_CACHE_TTL,
    )

    logger.info(
        "Aeris alerts fetched: %d alert(s) for %s",
        len(canonical_records),
        location,
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )
    return canonical_records


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
