"""OpenWeatherMap One Call 3.0 alerts provider module (ADR-016, ADR-038, ADR-052).

Five responsibilities per ADR-038 §2:
  1. Outbound API call — single GET per cache miss:
       GET /data/3.0/onecall?lat=&lon=&appid=&exclude=current,minutely,hourly,daily
     This is alerts-only payload (inverse of forecast module's exclude set, which
     is "current,minutely,alerts"). Two distinct cache entries, one appid env var.
     See brief lead-call 22 for the inverse-exclude pattern rationale.
  2. Response parsing — wire-shape Pydantic models for the alerts-only projection.
  3. Translation to canonical AlertRecord (passthrough mode per ADR-052 +
     datetime conversion + synthetic id synthesis).
  4. Capability declaration — CAPABILITY symbol consumed at startup.
  5. Error handling — provider errors translated to canonical taxonomy via
     ProviderHTTPClient.get() (bare propagation EXCEPT for the Q1 narrow wrap
     documented below).

OWM is a keyed provider (ADR-006):
  Single `appid` query param on every request.
  Sourced from env var WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID at startup
  (ADR-027 §3).  Provider-scoped per 3b-5 brief Q2 user decision 2026-05-08;
  same key works for forecast + alerts (mirrors 3b-7 Aeris precedent).

Cross-module constant import (brief lead-call 21):
  OWM_BASE_URL and OWM_ONECALL_PATH are imported from
  providers.forecast.openweathermap rather than redefined locally.
  This is intentionally unusual — sibling-module import to share URL constants.
  When OWM's base URL changes, one file edit covers both modules.
  Both modules hit the same /data/3.0/onecall endpoint but with different
  exclude= sets; consolidating into one call would intermix two separate caches.

Q1 user decision (2026-05-10) — One-Call-401 basic-tier → graceful empty list:
  This module wraps the ONE outbound call in a narrow try/except KeyInvalid
  block. When client.get(/data/3.0/onecall) raises KeyInvalid AND
  exc.status_code == 401 (basic-tier key lacking One Call 3.0 subscription):
    catch, log WARN once per process, cache empty list for 300s (cache parity
    with success path per 3b-5 audit F2 remediation), return [].
  Dispatch is on attribute (exc.status_code == 401), NOT message string
  (rules/coding.md §3 — per brief lead-call 9).
  This is NOT an L2 re-construct: the exception is swallowed (not re-raised as a
  new KeyInvalid); it is a deliberate dispatch-on-attribute swallow at one specific
  call site (mirror of 3b-5 forecast/owm Q1 fetch() shape).
  All other canonical exceptions propagate bare (L2 carry-forward rule, 3b-4 F1).

Alert empty list is the EXPECTED MODAL response for most stations most of the time
(no active alerts). A 401-forced empty list is operationally identical from the
dashboard's perspective; the operator's recovery action is identical (verify key
at OWM dashboard).

PARTIAL-DOMAIN per L1 rule (3b-7 lesson):
  urgency, certainty, areaDesc, category are categorically NOT supplied by OWM
  (any tier). They populate as None on canonical AlertRecord unconditionally.
  These are NOT in CAPABILITY.supplied_canonical_fields.

Passthrough mode per ADR-052 (updated operator directive 2026-06-02):
  OWM strips structured severity metadata from the originating agency data.
  nativeName and color are set to None (no data available on any tier).
  severityLevel defaults to 2 ("Alert"-equivalent) and severityLabel to "Alert":
  if something is an alert at all, it deserves advisory-level visual treatment
  rather than neutral/gray. Level 2 avoids overstating (not extreme/severe) while
  giving it proper visibility.
  hazardType is derived from the wire `tags` array (first element, if present).
  alertSystem is parsed from sender_name for known agency IDs ("nws", "ukmet",
  "meteofrance"); unknown senders resolve to None.

ruff: noqa: N815  (field names match wire camelCase: senderName, etc.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import AlertRecord
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.datetime_utils import epoch_to_utc_iso8601
from weewx_clearskies_api.providers._common.errors import (
    KeyInvalid,
    ProviderProtocolError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

# Cross-module constant import (brief lead-call 21): import shared URL constants
# from the forecast OWM module rather than redefining locally. Intentional
# sibling-module import — see module docstring for rationale.
from weewx_clearskies_api.providers.forecast.openweathermap import (
    OWM_BASE_URL,
    OWM_ONECALL_PATH,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "openweathermap"
DOMAIN = "alerts"
DEFAULT_ALERTS_TTL_SECONDS = 300  # 5 minutes per ADR-016 + ADR-017
_API_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4, brief lead-call 16 + 17 + 18)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        # ADR-052 / operator directive 2026-06-02: severityLevel and severityLabel ARE
        # now supplied — defaulting to 2 / "Alert" because any OWM alert deserves
        # advisory-level treatment. nativeName and color remain absent (OWM has no data).
        # PARTIAL-DOMAIN per L1 rule: urgency, certainty, areaDesc, category are NOT
        # in CAPABILITY — OWM categorically does not supply these on any tier.
        "id",
        "headline",
        "description",
        "event",
        "effective",
        "expires",
        "senderName",
        "hazardType",      # derived from wire tags[0] when present
        "alertSystem",     # parsed from sender_name for known agency IDs
        "severityLevel",   # always 2 (operator directive: alerts deserve visibility)
        "severityLabel",   # always "Alert"
        # source is provider_id literal (canonical §3.6 field), not a fetched wire field.
        "source",
    ),
    geographic_coverage="global",  # OWM: global government alerts per ADR-016 day-1 table
    auth_required=("appid",),
    default_poll_interval_seconds=DEFAULT_ALERTS_TTL_SECONDS,
    operator_notes=(
        "OpenWeatherMap One Call 3.0 alerts (paid 'One Call by Call' subscription "
        "required for /data/3.0/onecall). Basic-tier appid returns 401 — module "
        "gracefully returns empty alert list (Q1 user decision 2026-05-10; "
        "mirror of 3b-5 forecast/owm Q1 pattern). Coverage global per ADR-016 "
        "day-1 table ('Global government alerts'). "
        "Passthrough provider per ADR-052 (updated operator directive 2026-06-02): "
        "OWM strips structured severity metadata from the originating agency; "
        "severityLevel defaults to 2 and severityLabel to 'Alert' — any OWM alert "
        "deserves advisory-level visibility, not neutral/gray treatment. "
        "nativeName and color are always null (no OWM data available). "
        "urgency, certainty, areaDesc, and category are not provided by OWM on any "
        "tier (PARTIAL-DOMAIN per canonical §4.3 OWM column); always null."
    ),
    refresh_interval=300,
    attribution=ProviderAttribution(
        attribution_required=True,
        display_name="OpenWeather",
        attribution_text="Weather data provided by OpenWeather",
        text_prefix="Weather data provided by",
        text_provider_name="OpenWeather",
        url="https://openweathermap.org/",
        logo_required=True,
    ),
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5, brief lead-call 23)
# Source: docs/reference/api-docs/openweathermap.md L203-211 (alerts[] entry)
# extras="ignore" so OWM additions don't break us; missing required fields
# (event, start) raise ValidationError → translated to ProviderProtocolError.
#
# NOT reused from providers/forecast/openweathermap.py because the forecast
# wire model deliberately does NOT enumerate alerts[] (forecast module uses
# exclude=current,minutely,alerts). Cross-module coupling at the wire-model
# level would force a refactor when either module's exclude set changes.
# Both modules reuse epoch_to_utc_iso8601 from _common/datetime_utils.py (DRY).
# ---------------------------------------------------------------------------


class _OWMAlertEntry(BaseModel):
    """One entry in the alerts[] array (One Call 3.0 wire shape)."""

    model_config = ConfigDict(extra="ignore")

    sender_name: str | None = None
    event: str                    # required for id synthesis + hazardType derivation
    start: int                    # epoch UTC seconds; required for id + effective
    end: int | None = None        # epoch UTC seconds; nullable for expires
    description: str | None = None
    tags: list[str] | None = None  # ADR-052: tags[0] → hazardType when present


class _OWMOneCallAlertsResponse(BaseModel):
    """Top-level One Call 3.0 response, alerts-only projection.

    Excludes hourly/daily/current/minutely — this module fires with
    exclude=current,minutely,hourly,daily per brief lead-call 22.
    """

    model_config = ConfigDict(extra="ignore")

    lat: float
    lon: float
    # timezone_offset NOT needed — alerts have no station-local date derivation
    alerts: list[_OWMAlertEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3, brief lead-call 20)
# "Be polite" guard — 5 req/s max. With 5-min TTL + single-worker default,
# never trips in normal use (~288 calls/day for alerts + ~48 forecast = ~336/day,
# well within 1000/day One Call by Call quota).
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="openweathermap-alerts",
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
# Cache key construction (ADR-017 §Cache key, brief lead-call 15)
# ---------------------------------------------------------------------------


def _build_alerts_cache_key(lat: float, lon: float) -> str:
    """Build a deterministic cache key for (provider_id, endpoint, {lat4, lon4}).

    No target_unit dimension — alerts have no unit conversion.
    Logical endpoint key "alerts" distinct from the forecast module's
    "forecast_bundle" — two cache entries per station, one per domain, even
    though both modules hit the same /data/3.0/onecall URL with different
    exclude= sets.
    Lat/lon rounded to 4 decimal places per ADR-017.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "alerts",
            "params": {
                "lat4": round(lat, 4),
                "lon4": round(lon, 4),
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# ADR-052 alertSystem derivation from sender_name
# ---------------------------------------------------------------------------


def _owm_alert_system_from_sender(sender_name: str | None) -> str | None:
    """Parse alertSystem id from OWM sender_name for known agencies.

    OWM relays alerts from national meteorological agencies globally.
    sender_name is the agency's self-reported name string — not a controlled
    OWM vocabulary.  We identify a small set of well-known agencies by substring
    match; unknown senders resolve to None (passthrough mode per ADR-052).

    Args:
        sender_name: OWM alert sender_name (may be None or empty string).

    Returns:
        Canonical alertSystem id, or None when sender is unknown.
    """
    if not sender_name:
        return None
    if "NWS" in sender_name:
        return "nws"
    if "Met Office" in sender_name:
        return "ukmet"
    if "Météo-France" in sender_name or "Meteo-France" in sender_name:
        return "meteofrance"
    return None


# ---------------------------------------------------------------------------
# ID synthesis (brief lead-call 13, canonical §4.3)
# ---------------------------------------------------------------------------


def _synthesize_alert_id(event: str, start: int, sender_name: str | None) -> str:
    """Synthesize a stable alert id from event + start + sender_name.

    Canonical §4.3 says id = concat(event + start + sender_name).
    Operationalized with '|' separator for human-readability + grep-ability
    in operator logs.  None/empty sender_name → empty trailing segment.

    Args:
        event: OWM alert event string (required; always present in real payloads).
        start: OWM alert start epoch UTC seconds (required).
        sender_name: OWM sender_name (may be None or empty string).

    Returns:
        String in form "event|start|sender_name" or "event|start|" if no sender.
    """
    sender_part = sender_name if sender_name else ""
    return f"{event}|{start}|{sender_part}"


# ---------------------------------------------------------------------------
# Wire → canonical translation (canonical-data-model §4.3)
# ---------------------------------------------------------------------------


def _owm_alert_to_canonical(entry: _OWMAlertEntry) -> AlertRecord:
    """Map one OWM alerts[] entry to a canonical AlertRecord.

    Field mapping per canonical-data-model §4.3 OWM column (ADR-052 passthrough):
      id            = concat(event + start + sender_name) via _synthesize_alert_id
      headline      = event (direct passthrough; canonical §3.6 = "Provider's event name")
      description   = description (direct passthrough; OWM has no instruction-append)
      urgency       = None (PARTIAL-DOMAIN — OWM does not provide on any tier)
      certainty     = None (PARTIAL-DOMAIN — OWM does not provide on any tier)
      event         = event (passthrough; human-readable agency label)
      effective     = epoch_to_utc_iso8601(start) — UTC ISO-8601 Z (ADR-020)
      expires       = epoch_to_utc_iso8601(end) or None — UTC ISO-8601 Z
      senderName    = sender_name (direct passthrough)
      areaDesc      = None (PARTIAL-DOMAIN — OWM does not provide on any tier)
      category      = None (PARTIAL-DOMAIN — OWM does not provide on any tier)
      source        = "openweathermap" (provider_id literal)
      --- ADR-052 passthrough fields (updated operator directive 2026-06-02) ---
      severityLevel = 2 (default: alerts deserve advisory-level visibility, not null/gray)
      severityLabel = "Alert" (matches level 2 advisory-equivalent label)
      nativeName    = None (OWM event field is already English; no native-language original)
      color         = None (OWM does not supply color codes)
      hazardType    = tags[0] if tags non-empty, else None
      alertSystem   = parsed from sender_name via _owm_alert_system_from_sender
    """
    # id synthesis (brief lead-call 13)
    alert_id = _synthesize_alert_id(entry.event, entry.start, entry.sender_name)

    # effective: epoch_to_utc_iso8601(start) — always present per wire model
    effective = epoch_to_utc_iso8601(
        entry.start, provider_id=PROVIDER_ID, domain=DOMAIN
    )

    # expires: epoch_to_utc_iso8601(end) or None
    expires: str | None = None
    if entry.end is not None:
        expires = epoch_to_utc_iso8601(
            entry.end, provider_id=PROVIDER_ID, domain=DOMAIN
        )

    # ADR-052: hazardType from wire tags array (first element when present)
    hazard_type: str | None = None
    if entry.tags:
        hazard_type = entry.tags[0]

    # ADR-052: alertSystem parsed from sender_name for known agencies
    alert_system = _owm_alert_system_from_sender(entry.sender_name)

    return AlertRecord(
        id=alert_id,
        headline=entry.event,
        description=entry.description or "",
        urgency=None,         # PARTIAL-DOMAIN — OWM does not provide (canonical §4.3)
        certainty=None,       # PARTIAL-DOMAIN — OWM does not provide (canonical §4.3)
        event=entry.event,
        effective=effective,
        expires=expires,
        senderName=entry.sender_name or None,
        areaDesc=None,        # PARTIAL-DOMAIN — OWM does not provide (canonical §4.3)
        category=None,        # PARTIAL-DOMAIN — OWM does not provide (canonical §4.3)
        source=PROVIDER_ID,
        # ADR-052 / operator directive 2026-06-02: default to level 2 / "Alert"
        # so OWM alerts receive advisory-level visual treatment rather than neutral/gray.
        severityLevel=2,          # alerts deserve visibility — level 2 (advisory-equivalent)
        severityLabel="Alert",    # matches level 2 label
        nativeName=None,      # OWM event field is already English
        color=None,           # OWM does not supply color codes
        hazardType=hazard_type,
        alertSystem=alert_system,
    )


# ---------------------------------------------------------------------------
# Module-level state for log-once-per-process basic-tier warning (Q1=A)
# ---------------------------------------------------------------------------

_owm_basic_tier_warned: bool = False


def _owm_basic_tier_warned_set() -> None:
    """Mark that the basic-tier warning has been emitted (module-level state)."""
    global _owm_basic_tier_warned  # noqa: PLW0603
    _owm_basic_tier_warned = True


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    appid: str | None,
    http_client: ProviderHTTPClient | None = None,
) -> list[AlertRecord]:
    """GET /data/3.0/onecall (alerts-only) and return canonical AlertRecord list.

    One outbound call per cache miss. Cache stores list[dict] for Redis
    JSON-compat per ADR-017; reconstructed via AlertRecord.model_validate()
    on hit.

    exclude= param is "current,minutely,hourly,daily" (alerts-only payload).
    This is the INVERSE of the forecast module's "current,minutely,alerts".
    Two distinct cache entries per station; two separate outbound paths; one
    shared appid env var (brief lead-call 22).

    Q1 user decision (2026-05-10) — narrow try/except KeyInvalid:
      This function wraps the One Call outbound call in a narrow try/except
      KeyInvalid block. When the call raises KeyInvalid AND exc.status_code == 401
      (basic-tier key hitting /data/3.0/onecall), the exception is intentionally
      swallowed and an empty list is returned. This is NOT an L2 re-construct
      (we do not raise a new KeyInvalid); it is a deliberate dispatch-on-attribute
      swallow at one specific call site (mirror of 3b-5 forecast/owm Q1 shape).
      Cache parity: empty list IS cached for 300s (same TTL as success path) per
      3b-5 audit F2 remediation pattern — prevents per-poll 401 hammering on
      misconfigured basic-tier deployments.
      All other canonical exceptions propagate bare (L2 carry-forward, 3b-4 F1).
      Dispatch is on attribute (exc.status_code == 401), NOT message string
      (rules/coding.md §3, brief lead-call 9).

    Args:
        lat: Station latitude from services/station.py StationInfo.
        lon: Station longitude from services/station.py StationInfo.
        appid: OWM API key from env var WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID.
            None if operator hasn't configured it.
        http_client: Optional ProviderHTTPClient override for testing.
            When None, the module-level singleton is used.

    Returns:
        List of canonical AlertRecord models, possibly empty.
        Empty list when no active alerts OR when basic-tier 401 (Q1=A).

    Raises:
        KeyInvalid: appid is None/empty (early-raise before HTTP, brief lead-call 8),
            or OWM returned 401 with status_code != 401 (defensive re-raise path).
        QuotaExhausted: OWM returned 429 (rate limit exceeded).
        ProviderProtocolError: Response validation failed (missing event or start).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
    """
    # Validate credentials before any HTTP call (brief lead-call 8).
    # Loud failure beats silent disable — operator intent is unambiguous when
    # [alerts] provider = openweathermap.
    if not appid:
        raise KeyInvalid(
            "OpenWeatherMap appid missing — set WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    cache_key = _build_alerts_cache_key(lat, lon)
    cached_dicts = get_cache().get(cache_key)
    if cached_dicts is not None:
        logger.debug(
            "Cache hit for OWM alerts",
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        # Cache always stores list[dict] (post model_dump()); reconstruct models.
        return [AlertRecord.model_validate(d) for d in cached_dicts]

    logger.debug(
        "Cache miss for OWM alerts; calling /data/3.0/onecall",
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )

    params: dict[str, str] = {
        "lat": str(round(lat, 6)),
        "lon": str(round(lon, 6)),
        "appid": appid,
        "exclude": "current,minutely,hourly,daily",
    }

    client = http_client or _client_for()

    _rate_limiter.acquire()

    # Q1 user decision (2026-05-10): narrow try/except KeyInvalid for the
    # One-Call-401 graceful-empty-list path. Dispatch on attribute
    # (exc.status_code), NOT message string (rules/coding.md §3, lead-call 9).
    # This is intentional: basic-tier key hitting /data/3.0/onecall returns 401;
    # we catch it and return empty list rather than propagating as 502.
    # Mirror of 3b-5 forecast/owm fetch() Q1 shape — see module docstring.
    # ALL OTHER canonical exceptions propagate bare (L2 carry-forward rule,
    # 3b-4 audit F1: re-construction drops retry_after_seconds from QuotaExhausted).
    try:
        response = client.get(OWM_BASE_URL + OWM_ONECALL_PATH, params=params)
    except KeyInvalid as exc:
        if exc.status_code == 401:
            # Basic-tier key lacks One Call 3.0 subscription (Q1 user decision).
            # Log WARN once per process; cache empty list (parity with success
            # path per 3b-5 audit F2 remediation); return empty list.
            if not _owm_basic_tier_warned:
                _owm_basic_tier_warned_set()
                logger.warning(
                    "OpenWeatherMap appid lacks One Call 3.0 subscription — "
                    "returning empty alerts list. "
                    "Upgrade to 'One Call by Call' at openweathermap.org/price. "
                    "(Q1 user decision 2026-05-10; mirror of 3b-5 forecast/owm)",
                    extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
                )
            # Cache empty list for 300s (same TTL as success path).
            # Without this, basic-tier-misconfigured deployments hit 401 on
            # every dashboard poll — rate-limited only at 5 req/s.
            get_cache().set(cache_key, [], ttl_seconds=DEFAULT_ALERTS_TTL_SECONDS)
            return []
        # status_code != 401 — defensive: let canonical taxonomy handle.
        raise

    # Parse and validate the alerts-only wire shape
    try:
        wire = _OWMOneCallAlertsResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "OWM alerts response validation failed: %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"OWM alerts response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    if not wire.alerts:
        # Empty alerts array — no active alerts for this location.
        logger.info(
            "OWM alerts: no active alerts for lat=%s lon=%s",
            round(lat, 4),
            round(lon, 4),
            extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
        )
        get_cache().set(cache_key, [], ttl_seconds=DEFAULT_ALERTS_TTL_SECONDS)
        return []

    # Translate each wire alert entry to canonical AlertRecord
    canonical_records: list[AlertRecord] = []
    for entry in wire.alerts:
        canonical_records.append(_owm_alert_to_canonical(entry))

    # Store as list of dicts for JSON-serializable caching (ADR-017 §Decision).
    get_cache().set(
        cache_key,
        [record.model_dump() for record in canonical_records],
        ttl_seconds=DEFAULT_ALERTS_TTL_SECONDS,
    )

    logger.info(
        "OWM alerts fetched: %d alert(s) for lat=%s lon=%s",
        len(canonical_records),
        round(lat, 4),
        round(lon, 4),
        extra={"provider_id": PROVIDER_ID, "domain": DOMAIN},
    )
    return canonical_records


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None


def _reset_basic_tier_warned_for_tests() -> None:
    """Reset module-level basic-tier warning flag. Used in tests only."""
    global _owm_basic_tier_warned  # noqa: PLW0603
    _owm_basic_tier_warned = False
