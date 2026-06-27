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
  Real-wire amendment 2026-05-09: details.emergency is a JSON boolean (False)
  when no emergency text is set; isinstance(..., str) check filters it out.

Severity mapping (ADR-052 §Severity level mapping — amended 2026-06-01):
  details.priority is NOT a severity field — it's a NOAA hazard-map
  display-priority code (60=Wind Advisory, 96=Fire Weather Watch, etc.).
  Severity is encoded as the SUFFIX on details.type.
  ADR-052 replaces the old string enum (advisory/watch/warning) with:
    severityLevel: int 1–4 (4=highest)
    severityLabel: native system label string (e.g. "Warning", "Amber", "Orange")
    US/CA (NWS VTEC): "XX.YY.Z" where Z is the action/severity code:
      .W → level 4, label "Warning"
      .A → level 3, label "Watch"
      .Y → level 2, label "Advisory"
      .S → level 1, label "Statement"
    Non-US (Aeris severity suffix):
      .EX → level 4
      .SV → level 3
      .MD → level 2
      .MN → level 1
    severityLabel cross-mapped by dataSource:
      meteoalarm: EX→"Red", SV→"Orange", MD→"Yellow", MN→"Green"
      ukmet:      EX→"Red", SV→"Amber",  MD→"Yellow"
      noaa/envca: extracted from event name (" Warning"/Watch/Advisory/Statement)
      others:     Aeris suffix readable name (Extreme/Severe/Moderate/Minor)
  Unknown suffix or no suffix → level None, label None with WARNING log.

Real-wire amendment 2026-05-09 (3b-7 fixture-capture evidence):
  - details.urgency / details.certainty / details.category are NOT documented
    Aeris response fields and were absent from real-wire capture; PARTIAL-DOMAIN
    per L1 rule extension. CAPABILITY drops urgency + certainty.
  - The category field is named details.cat in real wire, not details.category.
  - details.emergency is bool | str (False when no text, string when set).
  - event field maps from details.name (human-readable, e.g. "FIRE WEATHER WATCH"),
    not details.type (structured code like "FW.A").

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
        # Real-wire amendment 2026-05-09: urgency + certainty are NOT documented
        # Aeris response fields (per Aeris docs + 3b-7 fixture capture).
        # PARTIAL-DOMAIN per L1 rule extension. category IS supplied (via the
        # details.cat wire field; canonical-data-model §4.3 amended).
        # ADR-052 (2026-06-01): severity string replaced by severityLevel (int 1–4)
        # + severityLabel (native display label). New fields alertSystem, hazardType,
        # nativeName, color added.
        "id",
        "headline",
        "description",
        "severityLevel",
        "severityLabel",
        "alertSystem",
        "hazardType",
        "nativeName",
        "color",
        "event",
        "effective",
        "expires",
        "senderName",
        "areaDesc",
        "category",
        # source is provider_id literal (canonical §3.6 field), not a fetched wire field.
        "source",
    ),
    # ADR-052 (2026-06-01): updated from "us-ca-eu" to reflect all documented regions.
    # Aeris alerts covers 10+ regions per current Aeris alerts endpoint docs
    # (GLOBAL-ALERT-SYSTEMS-RESEARCH.md §1b + live API verification 2026-06-01):
    # US (NWS), Canada (Environment Canada), Europe (MeteoAlarm), UK (Met Office),
    # Japan (JMA), Australia (BoM), India (IMD), Brazil (INMET),
    # South Africa (SAWS), South Korea (KMA), Mexico (CONAGUA/SMN).
    geographic_coverage="us-ca-eu-uk-jp-au-in-br-za-kr-mx",
    auth_required=("client_id", "client_secret"),
    default_poll_interval_seconds=_AERIS_CACHE_TTL,
    operator_notes=(
        "Aeris (AerisWeather/Xweather) alerts. Requires client_id + client_secret "
        "bound to a registered domain or bundle id "
        "(see docs/reference/api-docs/aeris.md §Authentication). "
        "Returns active alerts only per Aeris api-docs §Alerts. "
        "warn_location responses (off-grid lat/lon) return empty list. "
        "Coverage: US (NWS/noaa_nws), Canada (Environment Canada/envca), "
        "Europe (MeteoAlarm/meteoalarm), UK (Met Office/ukmet), Japan (JMA), "
        "Australia (BoM), India (IMD), Brazil (INMET), South Africa (SAWS), "
        "South Korea (KMA), Mexico (CONAGUA/SMN). "
        "urgency and certainty are not provided by Aeris (PARTIAL-DOMAIN); "
        "always null on the canonical bundle for this provider. "
        "severityLabel is cross-mapped to the source system's native terminology "
        "(e.g. 'Amber' for UK Met, 'Orange' for MeteoAlarm). "
        "details.color is Aeris's own rendering hex color — NOT the national system's color."
    ),
    refresh_interval=300,
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
# Severity dispatch tables (ADR-052, amended from 2026-05-09 string enum
# to integer levels 1–4)
#
# Severity is encoded as the SUFFIX on details.type, NOT in details.priority.
# US/CA alerts use NWS VTEC format "XX.YY.Z" where Z is the action/severity
# code; non-US alerts use Aeris's documented EX/SV/MD/MN suffix scheme.
# Real fixture: "FW.A" (Fire Weather Watch) → suffix "A" → 3 (Watch).
# api-docs example: "AW.TS.MD" (Moderate Thunderstorm) → suffix "MD" → 2.
# ADR-052 §Severity level mapping: 4=highest, 1=lowest.
# ---------------------------------------------------------------------------

# US/Canadian alerts: NWS VTEC suffix codes → integer severity level (ADR-052)
# Reference: https://www.weather.gov/vtec/
_VTEC_SUFFIX_TO_LEVEL: dict[str, int] = {
    "W": 4,   # Warning — highest
    "A": 3,   # Watch
    "Y": 2,   # Advisory
    "S": 1,   # Statement — lowest
}

# VTEC suffix → native label for severityLabel
_VTEC_SUFFIX_TO_LABEL: dict[str, str] = {
    "W": "Warning",
    "A": "Watch",
    "Y": "Advisory",
    "S": "Statement",
}

# Non-US alerts: Aeris severity suffix codes → integer severity level (ADR-052)
# Reference: https://www.xweather.com/docs/weather-api/endpoints/alerts
_AERIS_SUFFIX_TO_LEVEL: dict[str, int] = {
    "EX": 4,  # Extreme — highest
    "SV": 3,  # Severe
    "MD": 2,  # Moderate
    "MN": 1,  # Minor — lowest
}

# ---------------------------------------------------------------------------
# Cross-mapping: (dataSource, suffix) → native severity label (ADR-052 §6)
#
# Based on the cross-mapping table in
# docs/reference/GLOBAL-ALERT-SYSTEMS-RESEARCH.md §6 and live API verification
# 2026-06-01. Only the fully-documented or live-API-confirmed mappings are here;
# undocumented sources (JMA, BoM, IMD, INMET, SAWS, KMA, SMN) fall through to
# the Aeris suffix label as a readable fallback.
# ---------------------------------------------------------------------------

_DATASOURCE_SUFFIX_TO_LABEL: dict[tuple[str, str], str] = {
    # MeteoAlarm — 4 awareness color levels (EU pan-European)
    ("meteoalarm", "EX"): "Red",
    ("meteoalarm", "SV"): "Orange",
    ("meteoalarm", "MD"): "Yellow",
    ("meteoalarm", "MN"): "Green",
    # UK Met Office — 3 warning color levels (no Green)
    ("ukmet", "EX"): "Red",
    ("ukmet", "SV"): "Amber",
    ("ukmet", "MD"): "Yellow",
    # Environment Canada (bilingual; use English tier names matching NWS convention)
    ("envca", "W"): "Warning",
    ("envca", "A"): "Watch",
    ("envca", "Y"): "Advisory",
    ("envca", "S"): "Special Weather Statement",
}

# Aeris suffix → readable fallback label (used when dataSource has no entry above)
_AERIS_SUFFIX_FALLBACK_LABEL: dict[str, str] = {
    "EX": "Extreme",
    "SV": "Severe",
    "MD": "Moderate",
    "MN": "Minor",
}

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Source: docs/reference/api-docs/aeris.md §Alerts + brief §per-module spec
# extras="ignore" so Aeris additions don't break us; missing required fields
# raise ValidationError → translated to ProviderProtocolError.
# ---------------------------------------------------------------------------


class _AerisAlertDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str | None = None              # VTEC code (e.g. "FW.A") — used to derive severity
    name: str | None = None              # human-readable name (e.g. "FIRE WEATHER WATCH") — maps to event/headline
    loc: str | None = None
    priority: int | None = None          # NOAA hazard-map display-priority (NOT severity); kept for diagnostics
    color: str | None = None
    body: str | None = None              # description passthrough (brief call 13)
    # emergency: real Aeris wire returns boolean False when no emergency text is set.
    # Type declared as bool | str | None to accept both wire forms.
    # Real-capture fixture (alerts.md) showed boolean False; canonical-data-model §4.3 amended.
    # senderName logic in _to_canonical uses isinstance(..., str) check; falsy/bool falls
    # through to place.name fallback.
    emergency: bool | str | None = None  # senderName primary candidate (brief call 19, Q2)
    cat: str | None = None               # category — real wire uses 'cat', not 'category' (§4.3 amended)


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


class _AerisLocalLanguage(BaseModel):
    """One entry in the localLanguages array (ADR-052 §nativeName)."""

    model_config = ConfigDict(extra="ignore")

    language: str | None = None   # ISO 639 two-letter code (e.g. "fr", "en")
    name: str | None = None       # native-language alert name (e.g. "Vigilance jaune orages")
    body: str | None = None       # native-language description


class _AerisAlertRecord(BaseModel):
    """One alert from response[]."""

    model_config = ConfigDict(extra="ignore")

    id: str
    # dataSource: source system identifier (ADR-052 §alertSystem)
    # e.g. "noaa_nws", "meteoalarm", "ukmet", "envca"
    # Located at the top level of each advisory object per Aeris wire format.
    dataSource: str | None = None  # noqa: N815 — matches Aeris camelCase wire field
    active: bool | None = None
    details: _AerisAlertDetails
    timestamps: _AerisAlertTimestamps
    place: _AerisAlertPlace | None = None
    # localLanguages: native-language names (ADR-052 §nativeName, international alerts only)
    # noqa: N815 — matches Aeris camelCase wire field
    localLanguages: list[_AerisLocalLanguage] | None = None  # noqa: N815


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
# Severity normalization (ADR-052 §Severity level mapping, amended 2026-06-01)
# ---------------------------------------------------------------------------


def _parse_severity_suffix(type_code: str | None) -> str:
    """Extract the severity/action suffix from Aeris details.type.

    Aeris details.type is a dotted code (e.g. "FW.A", "AW.TS.MD"). The LAST
    segment is the severity/action suffix.

    Args:
        type_code: Aeris details.type string (e.g. "FW.A") or None.

    Returns:
        The suffix string (e.g. "A", "MD"), or "" if not present.
    """
    if not type_code:
        return ""
    parts = type_code.split(".")
    return parts[-1] if parts else ""


def _parse_severity_level(type_code: str | None) -> int | None:
    """Map the Aeris details.type suffix to an ADR-052 severity level integer (1–4).

    Aeris details.type is a dotted code (e.g. "FW.A", "AW.TS.MD"). The LAST
    segment is the severity/action suffix:

      - US/Canadian alerts use NWS VTEC: W→4, A→3, Y→2, S→1
      - Non-US alerts use Aeris: EX→4, SV→3, MD→2, MN→1

    Try VTEC first (one-letter), then Aeris severity (two-letter). Unknown
    suffix or no suffix → None with WARNING log to surface schema drift.

    Args:
        type_code: Aeris details.type string (e.g. "FW.A") or None.

    Returns:
        Severity level 1–4, or None on unknown/absent suffix.
    """
    suffix = _parse_severity_suffix(type_code)

    if not suffix:
        logger.warning(
            "Aeris alert has null/empty details.type; severityLevel will be None. "
            "This may indicate a schema change — check the severity dispatch."
        )
        return None

    # Try VTEC (single-letter, US/Canadian) first
    if suffix in _VTEC_SUFFIX_TO_LEVEL:
        return _VTEC_SUFFIX_TO_LEVEL[suffix]

    # Try Aeris severity (two-letter, non-US)
    if suffix in _AERIS_SUFFIX_TO_LEVEL:
        return _AERIS_SUFFIX_TO_LEVEL[suffix]

    # Unknown suffix → None + WARNING log
    logger.warning(
        "Unknown Aeris details.type suffix %r (full type=%r); "
        "severityLevel will be None. This may indicate a schema change — "
        "check VTEC and Aeris suffix dispatch tables.",
        suffix,
        type_code,
    )
    return None


def _parse_severity_label(
    type_code: str | None,
    data_source: str | None,
    event_name: str | None,
) -> str | None:
    """Derive the native severity label string for display (ADR-052 §severityLabel).

    Strategy (in priority order):
    1. US/CA (dataSource contains "noaa" or "envca"): extract tier from event
       name (" Warning" / " Watch" / " Advisory" / " Statement" suffix) or fall
       back to VTEC suffix label. Environment Canada shares NWS tiers.
    2. Documented international source (meteoalarm, ukmet): use
       _DATASOURCE_SUFFIX_TO_LABEL keyed by (dataSource, suffix).
    3. Unknown/undocumented source: use Aeris suffix readable name from
       _AERIS_SUFFIX_FALLBACK_LABEL (e.g. "Severe", "Moderate").
    4. No suffix: return None.

    Args:
        type_code: Aeris details.type string (e.g. "FW.A") or None.
        data_source: Aeris top-level dataSource field (e.g. "noaa_nws") or None.
        event_name: Aeris details.name human-readable string (e.g. "FIRE WEATHER WATCH")
            or None. Used for US/CA tier extraction.

    Returns:
        Native severity label string for display, or None.
    """
    suffix = _parse_severity_suffix(type_code)
    if not suffix:
        return None

    ds = (data_source or "").lower()

    # 1. US/CA: extract tier from event name where possible
    if "noaa" in ds or ds == "envca":
        if event_name:
            name_upper = event_name.upper()
            if name_upper.endswith(" WARNING"):
                return "Warning"
            if name_upper.endswith(" WATCH"):
                return "Watch"
            if name_upper.endswith(" ADVISORY"):
                return "Advisory"
            if name_upper.endswith(" STATEMENT"):
                return "Statement"
        # Fallback to VTEC suffix label when event name doesn't have a clear suffix
        # (covers edge cases; real Aeris names always contain the tier)
        vtec_label = _VTEC_SUFFIX_TO_LABEL.get(suffix)
        if vtec_label:
            return vtec_label

    # 2. Documented international source with cross-mapping table entry
    mapped = _DATASOURCE_SUFFIX_TO_LABEL.get((ds, suffix))
    if mapped:
        return mapped

    # 3. Aeris suffix readable fallback for undocumented sources
    fallback = _AERIS_SUFFIX_FALLBACK_LABEL.get(suffix)
    if fallback:
        return fallback

    # 4. VTEC suffix as final fallback (handles Statement/Advisory for envca
    #    and any new undocumented suffix that shares VTEC naming)
    return _VTEC_SUFFIX_TO_LABEL.get(suffix)


# ---------------------------------------------------------------------------
# Wire → canonical normalization (canonical-data-model §4.3)
# ---------------------------------------------------------------------------


def _to_canonical(record: _AerisAlertRecord) -> AlertRecord:
    """Map one Aeris alert record to a canonical AlertRecord.

    Field mapping per canonical-data-model §4.3 + ADR-052 (amended 2026-06-01):
      id = id (top-level)
      headline = details.name
      description = details.body (passthrough, no append — brief call 13)
      severityLevel = _parse_severity_level(details.type) → int 1–4 or None (ADR-052)
      severityLabel = _parse_severity_label(details.type, dataSource, details.name) (ADR-052)
      alertSystem = dataSource (ADR-052 §alertSystem)
      hazardType = details.cat (ADR-052 §hazardType; same source as category)
      nativeName = localLanguages[0].name if present (ADR-052 §nativeName)
      color = details.color hex string (ADR-052 §color; Aeris rendering color, not national)
      urgency = None (PARTIAL-DOMAIN — Aeris does not provide)
      certainty = None (PARTIAL-DOMAIN — Aeris does not provide)
      event = details.name (human-readable; details.type is the structured code)
      effective = timestamps.issuedISO via to_utc_iso8601_from_offset (call 14)
      expires = timestamps.expiresISO via to_utc_iso8601_from_offset (call 14)
      senderName = details.emergency (string only) ⇢ place.name ⇢ None (call 19, Q2)
      areaDesc = place.name (passthrough)
      category = details.cat (real wire uses 'cat', not 'category' — §4.3 amended)
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

    # ADR-052: nativeName = localLanguages[0].name when array is present
    native_name: str | None = None
    if record.localLanguages:
        first_lang = record.localLanguages[0]
        if first_lang.name and first_lang.name.strip():
            native_name = first_lang.name.strip()

    return AlertRecord(
        id=record.id,
        headline=record.details.name or "",
        description=record.details.body or "",
        # ADR-052: integer severity level (1–4) replaces old string severity enum
        severityLevel=_parse_severity_level(record.details.type),
        # ADR-052: native severity label for display (cross-mapped per dataSource)
        severityLabel=_parse_severity_label(
            record.details.type,
            record.dataSource,
            record.details.name,
        ),
        # ADR-052: source system identifier
        alertSystem=record.dataSource,
        # ADR-052: hazard type for icon selection (same source as category)
        hazardType=record.details.cat,
        # ADR-052: native-language alert name from first localLanguages entry
        nativeName=native_name,
        # ADR-052: Aeris rendering hex color (not the national system's color)
        color=record.details.color,
        urgency=None,   # PARTIAL-DOMAIN — Aeris does not provide (canonical §4.3 amended 2026-05-09)
        certainty=None, # PARTIAL-DOMAIN — Aeris does not provide (canonical §4.3 amended 2026-05-09)
        event=record.details.name or "",
        effective=effective or "",
        expires=expires,
        senderName=sender_name,
        areaDesc=area_desc,
        category=record.details.cat,   # real wire uses 'cat', not 'category' (§4.3 amended)
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
