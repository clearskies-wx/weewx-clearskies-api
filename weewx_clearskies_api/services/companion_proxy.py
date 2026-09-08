"""Generic companion-service proxy (T6.1, ARCHITECTURE.md "Manifest registration
pattern", API-MANUAL §19).

At API startup, when ``[providers] marine_service_url`` is configured,
this module fetches ``GET {marine_service_url}/manifest`` (no auth — same
contract as the marine service's own ``GET /health``) and dynamically
mounts one proxy route per manifest entry under ``/api/v1/``. The manifest
is re-fetched every 5 minutes; endpoints added/removed/changed in the
manifest are reconciled without an API restart. When the marine service is
unreachable at startup, the API logs an ERROR and starts with no marine
routes, retrying on the same 5-minute clock as the periodic refresh.

**Generic, not marine-specific in principle** — the manifest schema (path,
method, upstream, cache_ttl) has no marine-specific fields — but today
``marine_service_url`` is the only companion service the API knows how to
configure (API-MANUAL §19.2), so this module reads exactly one config key
and one secret. A second companion service would need its own config key
and its own call into ``register_companion_proxy()``-shaped wiring; nothing
here assumes there is only ever one.

**Envelope wrapping and unit conversion (T6.2).** Every proxied 200 response
passes through ``_apply_response_transform()`` before being cached/returned.
That single call site does three things, in order: (1) SI→operator
display-unit conversion of every known-group numeric field, delegated to
``services/marine_response_conversion.py`` (kept in its own module so this
generic proxy file doesn't have to carry an eleven-route field inventory —
see that module's docstring for the full field→group mapping and how two
genuine field-name collisions are resolved); (2) a named, currently-identity
enrichment seam (``_apply_post_conversion_enrichment()``) for the next round
to plug station-observation/alert/conditionsText/quality restoration into
(C-24/C-29) — a real function boundary, not a comment, exactly as T6.1 left
this seam for T6.2; (3) envelope wrapping (``data``/``stationClock``/
``freshness``/``units``/``generatedAt``), reusing the same
``build_station_clock()`` / ``build_freshness()`` machinery every native
endpoint uses (no second envelope implementation). Do not add
conversion/envelope logic anywhere else in this file; the single call site
in ``_proxy_request()`` is the contract for where it plugs in.

**The three-state rule (MARINE-SERVICE-SEPARATION-PLAN.md T6.1 ⚠ CORRECTED
2026-07-25) is the single most load-bearing behaviour here.** Three
situations must stay distinguishable end to end:

  1. Marine service unreachable (network failure / non-JSON / any HTTP
     status other than 200 or 404) and no cached response exists for this
     route → this proxy's own **503** (``_proxy_request()``, the
     ``fetch_result is None`` / cache-miss branches). This is the proxy
     reporting on itself, not on the model.
  2. Marine service answers with HTTP 200, including a null payload
     carrying ``modelStatus: "unavailable"`` → passed through **untouched**
     as 200 (``_proxy_request()``'s success branch — there is no
     modelStatus-specific code path at all; a 200 is a 200, cached like any
     other). The model having no answer for this hour is a successful
     proxied response, not a proxy failure.
  3. Unknown location / bad parameter → the marine service's own **404**,
     passed through untouched, never cached (``_proxy_request()``'s
     ``status_code == 404`` branch).

Any other upstream status (5xx, 401/403 from a misconfigured secret, etc.)
is treated the same as "unreachable" — cache fallback, else 503 — because
none of those are one of the three states this manifest/route contract
defines; the proxy does not invent a fourth meaning for them.

**Wizard discovery pass-throughs (C-42).** ``marine_discovery_get()`` is a
second, smaller call path alongside the manifest proxy above — a direct
authenticated GET for wizard discovery lookups
(``/discovery/buoy-stations``, ``/discovery/tide-stations``,
``/discovery/ofs-model``, ``/discovery/grib-availability``, and
``/discovery/fishing-species``), which are
one-off setup-time calls from ``endpoints/setup.py``, not cacheable
``/api/v1/*`` resources. It raises ``MarineDiscoveryUnconfiguredError`` /
``MarineDiscoveryUnavailableError`` instead of building a ``JSONResponse``
so callers can produce wizard-appropriate error text — see those classes'
docstrings for why the two must never share one message.

**Gap reporting (C-10).** ``POST /report/gap`` is intentionally NOT in the
marine service's manifest (see ``weewx-clearskies-marine``'s
``endpoints/gap.py`` docstring — it is a fire-and-forget POST with no TTL
and no cacheable resource, so the manifest schema has nothing to describe).
The API calls it directly via ``report_gap()`` below, which is a straight
port of ``providers/nearshore/swan.py``'s ``report_gap()`` /
``_gap_report_worker()`` (same dedup LRU bound, same bounded queue, same
single background worker, same fire-and-forget contract — a broken client
here would fail silently, exactly like the original). ``swan.py`` was
deleted by T6.6; its call sites are ported to ``_report_model_gaps_from_
response()`` below, which inspects every proxied 200 body for a
``modelStatus: "unavailable"`` signal and calls ``report_gap()`` — see that
function's docstring for the two response shapes it recognizes.

**Auth.** Every authenticated call to the marine service (proxied GETs and
``report_gap()``) attaches ``Authorization: Bearer {MARINE_SERVICE_SECRET}``,
read fresh from the process environment at call time (never cached in this
module — matches ``providers/nearshore/swan.py``'s ``SURF_COMPUTE_SECRET``
precedent and ADR-027 §3: secrets never touch ``api.conf`` or in-memory
config objects, only the environment). This is the exact header the marine
service's own ``auth.py`` (``require_bearer_auth``) expects; verified by
reading that module before writing this one, not guessed.

**Middleware parity.** Proxy routes are registered directly on the same
``FastAPI`` app instance native routes use (``app.router.routes``), added
before the app starts serving. CORS, security headers, proxy-auth, and
metrics middleware wrap the whole ASGI app, not individual routes, so they
apply identically to proxied and native routes with no extra wiring here —
this satisfies plan Do item 7 by construction rather than by a parallel
auth/rate-limit implementation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue as _queue
import threading
import time
import zlib
from collections import OrderedDict
from datetime import UTC, date, datetime, time as clock_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from weewx_clearskies_api.config.settings import Settings
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.marine_enrichment import apply_marine_enrichment
from weewx_clearskies_api.services.marine_response_conversion import (
    collect_marine_unit_labels,
    convert_marine_payload,
)
from weewx_clearskies_api.services.station import build_station_clock, get_station_info

logger = logging.getLogger(__name__)

#: Per ADR-027 §3 / API-MANUAL §19.2 — never stored in Settings or api.conf,
#: read fresh from the environment at every call site.
MARINE_SERVICE_SECRET_ENV_VAR = "MARINE_SERVICE_SECRET"  # noqa: S105 — env var name, not a secret value

#: Manifest refresh cadence AND the startup-retry cadence when the marine
#: service is unreachable at startup (plan Do items 4 and 6 share one
#: clock — see register_companion_proxy()).
_MANIFEST_REFRESH_INTERVAL_S = 300

_MANIFEST_FETCH_TIMEOUT_S = 5.0
#: 45 s, not 15: the all-transects profile route computes 25-30 s on the
#: marine host; at 15 s the proxy gave up before the answer existed, so the
#: heat map could never load (EYEBALL-FIX-PLAN 2026-08-04, S-SPEC-5 / M1).
_PROXY_REQUEST_TIMEOUT_S = 45.0
_API_PREFIX = "/api/v1"
#: Marker prefix on every route this module registers, so route
#: reconciliation can rebuild "everything except ours" without touching
#: native routers' entries in app.router.routes.
_ROUTE_NAME_PREFIX = "companion_proxy:"
_FISHING_SCORING_INPUTS_PARAM = "scoringInputs"
_FISHING_SCORING_INPUTS_VERSION = 3
_FISHING_FORECAST_DAYS = 3


# ---------------------------------------------------------------------------
# Per-companion-service state
# ---------------------------------------------------------------------------


class CompanionProxyState:
    """Live state for one companion service.

    Deliberately an instance, not module-level globals (unlike
    ``providers/nearshore/swan.py``'s ``_remote_url``/etc.) — a proxy that
    fetches its behaviour from a manifest has no reason to assume there is
    only ever one companion service, even though today there is exactly
    one (marine, via ``marine_service_url``).
    """

    def __init__(self, *, service_url: str, verify_tls: bool = True) -> None:
        self.service_url = service_url.rstrip("/")
        #: Verify the marine service's TLS certificate on every request made
        #: through this state (manifest fetch, discovery, proxied GETs, gap
        #: report POST). Sourced from ``[providers] marine_verify_tls``
        #: (config/settings.py) at ``register_companion_proxy()`` time and
        #: carried here — not re-read from global Settings at each call
        #: site — so the gap-report worker thread (a daemon, not a request
        #: handler) can honour it without reaching for module-global config.
        #: Defaults to True (secure default; API-MANUAL §19.2).
        self.verify_tls = verify_tls
        #: path (manifest "path", e.g. "/surf/{location_id}") -> manifest entry.
        #: Guarded by _lock; read under lock, replaced wholesale on each
        #: successful reconciliation (never mutated in place).
        self.registered: dict[str, dict[str, Any]] = {}
        #: T6.3: the manifest's own top-level "capabilities" list, verbatim,
        #: as last successfully fetched. The marine service's real
        #: endpoints/manifest.py (weewx-clearskies-marine) emits this as
        #: list[str] (e.g. ["surf", "tides", ...]) — confirmed against that
        #: module's compute_capabilities() and the plan's own example
        #: manifest, NOT the richer {id, displayName, requiresConfig} object
        #: shape API-MANUAL §19.1's illustrative example showed (lead-logged
        #: doc error, C-35, corrected in the same commit as this). Replaced
        #: wholesale on each successful manifest fetch, same discipline as
        #: `registered`. Empty list before the first successful fetch, and
        #: whenever the marine service is unreachable (get_marine_
        #: capabilities() below then correctly reports "no marine
        #: capabilities" to /capabilities rather than serving stale ones —
        #: see that function's docstring for why this differs from
        #: `registered`'s stale-routes-stay-mounted behaviour).
        self.capabilities: list[str] = []
        self.lock = threading.Lock()


#: Set by register_companion_proxy() when marine_service_url is configured;
#: None otherwise. report_gap() below is a no-op when this is None, mirroring
#: providers/nearshore/swan.py's "if not _remote_url: return" contract.
_active_state: CompanionProxyState | None = None


# ---------------------------------------------------------------------------
# Response transform (T6.2 — conversion + envelope; see module docstring)
# ---------------------------------------------------------------------------


def _apply_post_conversion_enrichment(data: Any, *, manifest_entry: dict[str, Any]) -> Any:
    """Restore what the marine-service port dropped (C-24/C-25/C-29/C-37).

    Runs AFTER unit conversion, on the already-display-unit-converted
    ``data`` payload, BEFORE envelope wrapping. Delegates to
    ``services/marine_enrichment.py`` — see that module's docstring for the
    full field-by-field accounting and the one HELD item (surf t=0 station
    wind, pending an operator ruling — MARINE-SEP-CONCERNS.md C-24). Kept as
    a thin call site here, matching T6.1's original "a real function
    boundary, not a comment" intent for this seam.
    """
    return apply_marine_enrichment(data, manifest_path=manifest_entry["path"])


def _parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse a UTC timestamp without treating malformed bounds as current."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _regular_forecast_timezone() -> ZoneInfo:
    """Return the configured local time zone for regular forecast columns."""
    try:
        return ZoneInfo(get_station_info().timezone)
    except (RuntimeError, ZoneInfoNotFoundError):
        logger.warning("Marine CWF alignment has no usable station timezone; using UTC")
        return ZoneInfo("UTC")


def _forecast_column_key(valid_time: datetime, timezone: ZoneInfo) -> tuple[date, bool]:
    """Return the existing regular forecast's local 6am/6pm column key."""
    local_time = valid_time.astimezone(timezone)
    if 6 <= local_time.hour < 18:
        return local_time.date(), True
    return (
        local_time.date() - timedelta(days=1) if local_time.hour < 6 else local_time.date(),
        False,
    )


def _forecast_column_bounds(
    column_date: date, is_day: bool, timezone: ZoneInfo
) -> tuple[datetime, datetime]:
    """Return the UTC bounds of an existing local day or night forecast column."""
    start_hour = 6 if is_day else 18
    start = datetime.combine(column_date, clock_time(hour=start_hour), tzinfo=timezone)
    return start.astimezone(UTC), (start + timedelta(hours=12)).astimezone(UTC)


_WEEKDAY_LABELS = {
    "MONDAY": 0,
    "TUESDAY": 1,
    "WEDNESDAY": 2,
    "THURSDAY": 3,
    "FRIDAY": 4,
    "SATURDAY": 5,
    "SUNDAY": 6,
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def _cwf_label_column(
    label: Any, *, anchor_date: date
) -> tuple[date, bool] | None:
    """Map a CWF day/night label to one existing regular forecast column.

    CWF prose does not carry separate machine-readable period bounds. The
    regular forecast's local day/night columns therefore provide the time
    authority: an unrecognised CWF label remains unaligned rather than gaining
    an invented interval.
    """
    if not isinstance(label, str):
        return None
    normalized = " ".join(label.upper().split())
    if not normalized or "EXTENDED" in normalized:
        return None

    is_night = (
        normalized in {"TONIGHT", "OVERNIGHT", "THIS EVENING"}
        or normalized.endswith(" NIGHT")
    )
    if normalized in {"TODAY", "THIS MORNING", "THIS AFTERNOON", "THIS EVENING", "TONIGHT", "OVERNIGHT"}:
        return anchor_date, not is_night
    if normalized in {"TOMORROW", "TOMORROW MORNING", "TOMORROW AFTERNOON"}:
        return anchor_date + timedelta(days=1), True
    if normalized in {"TOMORROW NIGHT", "TOMORROW EVENING"}:
        return anchor_date + timedelta(days=1), False

    weekday_label = normalized.removesuffix(" NIGHT")
    weekday = _WEEKDAY_LABELS.get(weekday_label)
    if weekday is None:
        return None
    return anchor_date + timedelta(days=(weekday - anchor_date.weekday()) % 7), not is_night


def _join_regional_marine_additions(data: Any, *, manifest_path: str) -> Any:
    """Attach CWF regional fields to their matching location forecast columns.

    CWF never overwrites provider weather. Its period bounds are the matched
    existing regular-forecast column bounds, and its product issuance stays
    attached as source provenance.
    """
    if manifest_path != "/marine/{location_id}" or not isinstance(data, dict):
        return data
    forecast = data.get("regularForecast")
    periods = data.get("textForecast")
    if not isinstance(forecast, list) or not isinstance(periods, list):
        return data

    timezone = _regular_forecast_timezone()
    timed_points = [
        (point, _parse_utc_timestamp(point.get("validTime")))
        for point in forecast
        if isinstance(point, dict)
    ]
    timed_points = [(point, valid_time) for point, valid_time in timed_points if valid_time is not None]
    if not timed_points:
        return data
    now = datetime.now(tz=UTC)
    anchor_date = min(timed_points, key=lambda item: abs(item[1] - now))[1].astimezone(timezone).date()

    available_columns = {
        _forecast_column_key(valid_time, timezone)
        for _point, valid_time in timed_points
    }
    periods_by_column: dict[tuple[date, bool], dict[str, Any]] = {}
    for period in periods:
        if not isinstance(period, dict):
            continue
        column = _cwf_label_column(period.get("periodName"), anchor_date=anchor_date)
        if column is None or column not in available_columns:
            continue
        period_start, period_end = _forecast_column_bounds(*column, timezone)
        period["periodStart"] = utc_isoformat(period_start)
        period["periodEnd"] = utc_isoformat(period_end)
        periods_by_column[column] = period

    for point, valid_time in timed_points:
        column_date, is_day = _forecast_column_key(valid_time, timezone)
        matching_period = periods_by_column.get((column_date, is_day))
        if matching_period is None:
            continue
        point["marineAdditions"] = {
            "source": "nws_cwf",
            "validTime": point["validTime"],
            "periodStart": matching_period["periodStart"],
            "periodEnd": matching_period["periodEnd"],
            "periodName": matching_period.get("periodName"),
            "issuanceTime": matching_period.get("issuanceTime"),
            "wind": matching_period.get("wind"),
            "seas": matching_period.get("seas"),
            "visibility": matching_period.get("visibility"),
            "weather": matching_period.get("weather"),
            "text": matching_period.get("text"),
        }
    return data


def _apply_response_transform(body: Any, *, manifest_entry: dict[str, Any]) -> Any:
    """SI→operator-display-unit conversion + envelope wrapping (T6.2) +
    post-conversion enrichment (C-24/C-25/C-29/C-37).

    Every proxied 200 response — including a null payload carrying
    ``modelStatus: "unavailable"`` (three-state rule state 2) — flows
    through this single call site before being cached and returned. Do not
    add conversion/envelope logic anywhere else in this module.

    Order: convert (services/marine_response_conversion.py; never crashes
    on nulls — every marine numeric field is nullable and this walks past
    None values untouched) -> post-conversion enrichment seam
    (services/marine_enrichment.py — station observations, alerts, locale
    text composition; also never crashes on nulls) -> envelope wrap,
    reusing the same build_station_clock()/build_freshness() machinery
    every native endpoint uses (API-MANUAL §2 envelope shape) rather than a
    second implementation.
    """
    converted, units_block = convert_marine_payload(body)
    enriched = _apply_post_conversion_enrichment(converted, manifest_entry=manifest_entry)
    enriched = _join_regional_marine_additions(
        enriched,
        manifest_path=manifest_entry["path"],
    )
    collect_marine_unit_labels(enriched, units_block)
    return {
        "data": enriched,
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness(
            "marine", provider_refresh_interval=manifest_entry["cache_ttl"]
        ).model_dump(by_alias=True),
        "units": units_block,
        "generatedAt": utc_isoformat(datetime.now(tz=UTC)),
    }


# ---------------------------------------------------------------------------
# Manifest fetch
# ---------------------------------------------------------------------------


def _fetch_manifest(state: CompanionProxyState) -> dict[str, Any] | None:
    """GET {service_url}/manifest (no auth). Returns None on any failure —
    network error, non-2xx, or a body that isn't valid JSON — logging the
    reason. Never raises.
    """
    try:
        with httpx.Client(timeout=_MANIFEST_FETCH_TIMEOUT_S, verify=state.verify_tls) as client:
            response = client.get(f"{state.service_url}/manifest")
    except httpx.HTTPError as exc:
        logger.error(
            "Companion proxy: manifest fetch from %s failed: %s",
            state.service_url, exc,
        )
        return None

    if response.status_code != 200:
        logger.error(
            "Companion proxy: manifest fetch from %s returned HTTP %d",
            state.service_url, response.status_code,
        )
        return None

    try:
        manifest = response.json()
    except ValueError as exc:
        logger.error(
            "Companion proxy: manifest from %s is not valid JSON: %s",
            state.service_url, exc,
        )
        return None

    if not isinstance(manifest, dict) or not isinstance(manifest.get("endpoints"), list):
        logger.error(
            "Companion proxy: manifest from %s has no 'endpoints' list — ignoring",
            state.service_url,
        )
        return None

    return manifest


def _valid_manifest_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate and index manifest["endpoints"] by "path". Malformed entries
    (missing path/upstream, non-GET method, non-int cache_ttl) are dropped
    with a WARNING rather than crashing route reconciliation.
    """
    entries: dict[str, dict[str, Any]] = {}
    for raw_entry in manifest.get("endpoints", []):
        if not isinstance(raw_entry, dict):
            logger.warning("Companion proxy: skipping non-object manifest entry: %r", raw_entry)
            continue
        path = raw_entry.get("path")
        upstream = raw_entry.get("upstream")
        method = str(raw_entry.get("method", "GET")).upper()
        cache_ttl = raw_entry.get("cache_ttl")
        if (
            not isinstance(path, str) or not path
            or not isinstance(upstream, str) or not upstream
            or method != "GET"
            or not isinstance(cache_ttl, int) or isinstance(cache_ttl, bool) or cache_ttl < 0
        ):
            logger.warning("Companion proxy: skipping malformed manifest entry: %r", raw_entry)
            continue
        entries[path] = {
            "path": path, "upstream": upstream, "method": method, "cache_ttl": cache_ttl,
        }
    return entries


# ---------------------------------------------------------------------------
# Upstream fetch + the three-state rule
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    secret = os.environ.get(MARINE_SERVICE_SECRET_ENV_VAR, "")
    return {"Authorization": f"Bearer {secret}"} if secret else {}


# ---------------------------------------------------------------------------
# Wizard discovery pass-throughs (C-42, MARINE-SEP-CONCERNS.md).
#
# The manifest/_proxy_request() machinery above exists for cacheable
# /api/v1/* dashboard resources. The wizard's discovery lookups
# (nearby NDBC buoys, nearby CO-OPS tide stations, the covering OFS model,
# GRIB2 backend availability) are one-off setup-time calls made directly by
# endpoints/setup.py's own /setup/* handlers — they were never manifest
# entries and don't need a cache TTL. This is a separate, smaller call path
# that reuses the same auth header and same verify_tls-honouring httpx.Client
# construction as _fetch_upstream() (rules/coding.md DRY) rather than a
# second HTTP client.
# ---------------------------------------------------------------------------


class MarineDiscoveryError(Exception):
    """Base for wizard discovery pass-through failures (C-42).

    Per the operator ruling, the wizard must never see an empty result
    silently standing in for "the marine service couldn't be reached" —
    that reads as "there are no buoys near you," which is a wrong answer
    wearing a valid response's clothes. Callers in endpoints/setup.py must
    catch this (or a subclass) and surface an honest error to the wizard,
    never swallow it into an empty list/None.
    """


class MarineDiscoveryUnconfiguredError(MarineDiscoveryError):
    """``[providers] marine_service_url`` is not set.

    A legitimate operator state, not a fault — the operator has not
    installed/enabled the marine service. Message text must say so, not
    imply something is broken.
    """


class MarineDiscoveryUnavailableError(MarineDiscoveryError):
    """The marine service is configured but the discovery request failed
    (network error, non-JSON body, or a non-200 status). A genuine outage —
    message text is distinct from MarineDiscoveryUnconfiguredError's.

    ``status_code`` carries the HTTP status when the service answered with a
    non-200 (``None`` for network/parse failures) so callers can dispatch on
    state, never on the message string (rules/coding.md "Dispatch on
    exception state via attributes"). Gate M1-API finding (2026-08-27): the
    marine service answers 404 on ``GET /marine`` when it is installed but
    has no locations yet — a legitimate install state, not an outage.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_DISCOVERY_REQUEST_TIMEOUT_S = 15.0


def marine_discovery_get(path: str, params: dict[str, Any]) -> Any:
    """Direct (non-manifest) authenticated GET to the marine service.

    Used by endpoints/setup.py's wizard discovery pass-throughs:
    ``/discovery/buoy-stations``, ``/discovery/tide-stations``,
    ``/discovery/ofs-model``, ``/discovery/grib-availability``, and the
    matrix-backed ``/discovery/fishing-species``. Returns parsed JSON on 200.

    Raises:
        MarineDiscoveryUnconfiguredError: ``marine_service_url`` is not
            configured (``register_companion_proxy()`` was a no-op).
        MarineDiscoveryUnavailableError: the marine service is configured
            but the request failed — network error, non-JSON body, or any
            non-200 status. The exception message names the reason for
            operator logs; callers decide the wizard-facing text.
    """
    if _active_state is None:
        raise MarineDiscoveryUnconfiguredError(
            "marine_service_url is not configured; marine features require the marine service."
        )
    state = _active_state
    url = f"{state.service_url}{path}"

    try:
        with httpx.Client(timeout=_DISCOVERY_REQUEST_TIMEOUT_S, verify=state.verify_tls) as client:
            response = client.get(url, params=params, headers=_auth_headers())
    except httpx.HTTPError as exc:
        raise MarineDiscoveryUnavailableError(
            f"The marine service at {state.service_url} is unreachable: {exc}"
        ) from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise MarineDiscoveryUnavailableError(
            f"The marine service returned a non-JSON response for {path}"
        ) from exc

    if response.status_code != 200:
        raise MarineDiscoveryUnavailableError(
            f"The marine service returned HTTP {response.status_code} for {path}",
            status_code=response.status_code,
        )

    return body


def _fetch_upstream(
    state: CompanionProxyState, resolved_upstream: str, query_params: Any
) -> tuple[int, Any] | None:
    """GET {service_url}{resolved_upstream} with Bearer auth.

    Returns (status_code, parsed_json_body) on any response the marine
    service actually sent, or None on network failure / non-JSON body —
    the caller treats None the same as "unreachable" (state 1 of the
    three-state rule).
    """
    url = f"{state.service_url}{resolved_upstream}"
    try:
        with httpx.Client(timeout=_PROXY_REQUEST_TIMEOUT_S, verify=state.verify_tls) as client:
            response = client.get(url, params=dict(query_params), headers=_auth_headers())
    except httpx.HTTPError as exc:
        logger.warning("Companion proxy: request to %s failed: %s", url, exc)
        return None

    try:
        body = response.json()
    except ValueError:
        logger.warning(
            "Companion proxy: non-JSON response from %s (status %d)",
            url, response.status_code,
        )
        return None

    return response.status_code, body


def _tide_state_at(predictions: list[dict[str, Any]], at_time: datetime) -> str | None:
    """Classify a time-matched CO-OPS prediction interval without scoring it."""
    parsed: list[tuple[datetime, str]] = []
    for prediction in predictions:
        valid_time = _parse_utc_timestamp(prediction.get("time"))
        tide_type = prediction.get("type")
        if valid_time is not None and tide_type in {"high", "low"}:
            parsed.append((valid_time, tide_type))
    parsed.sort(key=lambda item: item[0])
    if len(parsed) < 2:
        return None

    before: tuple[datetime, str] | None = None
    after: tuple[datetime, str] | None = None
    for item in parsed:
        if item[0] <= at_time:
            before = item
        else:
            after = item
            break
    if before is None or after is None:
        return None
    if abs(at_time - before[0]) <= timedelta(minutes=30):
        return "slack_high" if before[1] == "high" else "slack_low"
    if abs(after[0] - at_time) <= timedelta(minutes=30):
        return "slack_high" if after[1] == "high" else "slack_low"
    midpoint = before[0] + (after[0] - before[0]) / 2
    if abs(at_time - midpoint) <= timedelta(minutes=30):
        return "peak_flow"
    return "incoming" if before[1] == "low" else "outgoing"


def _period_input_at_midpoint(
    candidates: list[dict[str, Any]], period_start: datetime, period_end: datetime
) -> dict[str, Any] | None:
    """Choose a source record only when its valid time is inside the period."""
    midpoint = period_start + (period_end - period_start) / 2
    timed = [
        (entry, _parse_utc_timestamp(entry.get("validTime")))
        for entry in candidates
        if isinstance(entry, dict)
    ]
    timed = [
        (entry, valid_time)
        for entry, valid_time in timed
        if valid_time is not None and period_start <= valid_time <= period_end
    ]
    if not timed:
        return None
    return min(timed, key=lambda item: abs(item[1] - midpoint))[0]


def _unavailable_field_provenance() -> dict[str, Any]:
    return {
        "available": False,
        "source": "unavailable",
        "sourceType": "unavailable",
        "validTime": None,
        "unit": None,
    }


def _fishing_periods(location_id: str) -> list[tuple[str, str, datetime]]:
    """Build the same three-day period midpoints the Fishing endpoint consumes.

    The API already owns astronomical data.  This only identifies time windows
    so source values can be matched before the request crosses to marine; it
    does not calculate a fishing score or any species treatment.
    """
    from weewx_clearskies_api.enrichment.solunar import compute_solunar  # noqa: PLC0415
    from weewx_clearskies_api.services.marine_enrichment import _find_location  # noqa: PLC0415

    location = _find_location(location_id)
    if location is None:
        return []
    timezone = _regular_forecast_timezone().key
    periods: list[tuple[str, str, datetime]] = []
    for day_offset in range(_FISHING_FORECAST_DAYS):
        solunar = compute_solunar(
            datetime.now(tz=UTC).date() + timedelta(days=day_offset),
            location.lat,
            location.lon,
            station_tz=timezone,
        )
        sunrise = _parse_utc_timestamp(solunar.sunrise)
        sunset = _parse_utc_timestamp(solunar.sunset)
        if sunrise is None or sunset is None or sunset <= sunrise:
            continue
        daylight_third = (sunset - sunrise) / 3
        for start, end in (
            (sunrise - timedelta(hours=1), sunrise + timedelta(hours=1)),
            (sunrise + timedelta(hours=1), sunrise + daylight_third),
            (sunrise + daylight_third, sunrise + 2 * daylight_third),
            (sunrise + 2 * daylight_third, sunset - timedelta(hours=1)),
            (sunset - timedelta(hours=1), sunset + timedelta(hours=1)),
            (sunset + timedelta(hours=1), sunset + timedelta(hours=7)),
        ):
            if end > start:
                periods.append((utc_isoformat(start), utc_isoformat(end), start + (end - start) / 2))
    return periods


def _fishing_depth_temperature_candidates(marine_body: Any) -> list[dict[str, Any]]:
    """Select only explicit depth-bearing marine temperature records.

    A selected-location surface observation without a depth is deliberately
    not eligible for Fishing's target-depth core input.  NDBC is never read
    here and cannot enter this transport path.
    """
    if not isinstance(marine_body, dict):
        return []
    candidates: list[dict[str, Any]] = []
    raw_entries = list(marine_body.get("forecast", []))
    observation = marine_body.get("observation")
    if isinstance(observation, dict):
        raw_entries.append(observation)
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        temperature = entry.get("waterTemp")
        provenance = entry.get("waterTempProvenance")
        if not isinstance(provenance, dict):
            provenance = (
                entry.get("provenance", {}).get("waterTemperature")
                if isinstance(entry.get("provenance"), dict)
                else None
            )
        depth_m = provenance.get("depthM") if isinstance(provenance, dict) else None
        valid_time = entry.get("time") or (provenance.get("validTime") if isinstance(provenance, dict) else None)
        coverage_tier = provenance.get("coverageTier") if isinstance(provenance, dict) else None
        source = provenance.get("source") if isinstance(provenance, dict) else None
        if (
            isinstance(temperature, int | float)
            and not isinstance(temperature, bool)
            and isinstance(depth_m, int | float)
            and not isinstance(depth_m, bool)
            and depth_m >= 0
            and _parse_utc_timestamp(valid_time) is not None
            and isinstance(provenance, dict)
            and provenance.get("available") is True
            and provenance.get("sourceType") in {"observed", "modeled", "forecast"}
            and isinstance(source, str)
            and not source.casefold().startswith(("ndbc", "coops"))
            and coverage_tier
            in {"local_sensor", "ofs", "regional_erddap", "rtofs", "mur_sst", "observed"}
        ):
            candidates.append(
                {
                    "validTime": valid_time,
                    "waterTemperatureC": float(temperature),
                    "provenance": {
                        "available": True,
                        "source": source,
                        "sourceType": provenance.get("sourceType"),
                        "validTime": provenance.get("validTime") or valid_time,
                        "coverageTier": coverage_tier,
                        "depthM": float(depth_m),
                        "unit": "degree_C",
                    },
                }
            )
    return candidates


def _build_fishing_scoring_payload(
    state: CompanionProxyState, location_id: str
) -> tuple[str | None, list[dict[str, Any]]]:
    """Assemble bounded scorer input and the separate public tide chart.

    The public Fishing response retains every CO-OPS prediction needed by the
    tide chart.  Those chart points are not scorer inputs: the scorer receives
    only the derived state for each of its bounded forecast periods.  Keeping
    the chart outside the compressed API-to-marine handoff prevents a dense
    prediction series from exceeding marine's defensive decoded-size limit.
    """
    try:
        from weewx_clearskies_api.services.marine_enrichment import (  # noqa: PLC0415
            build_fishing_weather_inputs,
        )

        weather_inputs = build_fishing_weather_inputs(location_id)
        periods = _fishing_periods(location_id)
    except Exception:
        logger.warning("Fishing input assembly failed for %s", location_id, exc_info=True)
        return None, []
    if not periods:
        return None, []

    tide_result = _fetch_upstream(state, f"/tides/{location_id}", {})
    tide_body = tide_result[1] if tide_result is not None and tide_result[0] == 200 else {}
    tide_predictions = tide_body.get("predictions", []) if isinstance(tide_body, dict) else []
    if not isinstance(tide_predictions, list):
        tide_predictions = []

    marine_result = _fetch_upstream(state, f"/marine/{location_id}", {})
    marine_body = marine_result[1] if marine_result is not None and marine_result[0] == 200 else {}
    temperature_candidates = _fishing_depth_temperature_candidates(marine_body)
    swell_candidates = [
        {
            "validTime": entry.get("time"),
            "swellHeight": entry.get("swellHeight"),
            "swellPeriod": entry.get("swellPeriod"),
            "swellProvenance": entry.get("swellProvenance", _unavailable_field_provenance()),
        }
        for entry in marine_body.get("forecast", [])
        if isinstance(entry, dict)
    ] if isinstance(marine_body, dict) else []

    points: list[dict[str, Any]] = []
    for period_start, period_end, midpoint in periods:
        start_time = _parse_utc_timestamp(period_start)
        end_time = _parse_utc_timestamp(period_end)
        if start_time is None or end_time is None:
            continue
        weather = _period_input_at_midpoint(weather_inputs, start_time, end_time)
        swell = _period_input_at_midpoint(swell_candidates, start_time, end_time)
        temperature_candidates_for_period = [
            candidate
            for candidate in temperature_candidates
            if (
                (candidate_time := _parse_utc_timestamp(candidate.get("validTime"))) is not None
                and start_time <= candidate_time <= end_time
            )
        ]
        tide_state = _tide_state_at(tide_predictions, midpoint)
        tide_provenance = (
            {
                "available": True,
                "source": "coops",
                "sourceType": "forecast",
                "validTime": utc_isoformat(midpoint),
                "unit": "meter",
            }
            if tide_state is not None
            else _unavailable_field_provenance()
        )
        points.append(
            {
                "periodStart": period_start,
                "periodEnd": period_end,
                "pressureTrendHpa3h": weather.get("pressureTrendHpa3h") if weather else None,
                "tideState": tide_state,
                "temperatureCandidates": temperature_candidates_for_period,
                "windSpeed": weather.get("windSpeed") if weather else None,
                "windDirection": weather.get("windDirection") if weather else None,
                "windGust": weather.get("windGust") if weather else None,
                "pressureProvenance": weather.get("pressureProvenance") if weather else _unavailable_field_provenance(),
                "tideCurrentProvenance": tide_provenance,
                "weatherProvenance": weather.get("weatherProvenance") if weather else _unavailable_field_provenance(),
                "swellHeight": swell.get("swellHeight") if swell else None,
                "swellPeriod": swell.get("swellPeriod") if swell else None,
                "swellProvenance": swell.get("swellProvenance") if swell else _unavailable_field_provenance(),
            }
        )

    payload = {
        "version": _FISHING_SCORING_INPUTS_VERSION,
        "locationId": location_id,
        "points": points,
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    transport = base64.urlsafe_b64encode(zlib.compress(encoded, level=9)).decode("ascii").rstrip("=")
    return transport, tide_predictions


def _cache_key(service_url: str, resolved_upstream: str, query_params: Any) -> str:
    sorted_query = "&".join(f"{k}={v}" for k, v in sorted(dict(query_params).items()))
    return f"companion_proxy:{service_url}:{resolved_upstream}?{sorted_query}"


def _proxy_request(
    request: Request, state: CompanionProxyState, manifest_entry: dict[str, Any]
) -> JSONResponse:
    """The three-state rule, implemented. See module docstring.

    Deliberately a sync ``def``, not ``async def``: the fetch below
    (``_fetch_upstream``) uses a blocking ``httpx.Client``, matching every
    other HTTP-calling module in this codebase (``ProviderHTTPClient`` is
    sync-only; ``providers/nearshore/swan.py``'s remote-mode calls are
    sync). FastAPI dispatches a sync route handler to its threadpool
    automatically, so this blocks a worker thread, not the asyncio event
    loop — an ``async def`` wrapping this same blocking call would instead
    stall every other in-flight request on this event loop for the
    duration of the marine service round-trip, which is the actual bug an
    earlier draft of this function had.
    """
    upstream_template = manifest_entry["upstream"]
    ttl_seconds = manifest_entry["cache_ttl"]

    try:
        resolved_upstream = upstream_template.format(**request.path_params)
    except KeyError as exc:
        # Manifest's own upstream template doesn't match its path's params —
        # a marine-service authoring bug, not a client error.
        raise HTTPException(
            status_code=500,
            detail=f"Companion proxy: manifest upstream template missing parameter {exc}",
        ) from exc

    upstream_query = dict(request.query_params)
    # This is authenticated API-to-marine transport only.  A browser-supplied
    # value is discarded; it must never be possible to inject arbitrary
    # scoring inputs through the public proxy route.
    upstream_query.pop(_FISHING_SCORING_INPUTS_PARAM, None)
    if manifest_entry["path"] == "/fishing/{location_id}":
        assembled_inputs, tide_predictions = _build_fishing_scoring_payload(
            state,
            str(request.path_params.get("location_id", "")),
        )
        if assembled_inputs is not None:
            upstream_query[_FISHING_SCORING_INPUTS_PARAM] = assembled_inputs

    cache = get_cache()
    cache_key = _cache_key(state.service_url, resolved_upstream, upstream_query)

    fetch_result = _fetch_upstream(state, resolved_upstream, upstream_query)

    if fetch_result is None:
        # State 1a: unreachable / non-JSON. Stale-preferred-to-none, else 503.
        cached = cache.get(cache_key)
        if cached is not None:
            logger.warning(
                "Companion proxy: %s unreachable, serving cached response for %s",
                state.service_url, resolved_upstream,
            )
            return JSONResponse(content=cached["body"], status_code=cached["status_code"])
        raise HTTPException(
            status_code=503,
            detail=(
                f"The marine service is unreachable and no cached response is "
                f"available for {resolved_upstream}."
            ),
        )

    status_code, body = fetch_result

    if status_code == 404:
        # State 3: unknown location / bad parameter. Passed through
        # untouched, never cached — nothing to serve stale for a resource
        # that does not exist.
        return JSONResponse(content=body, status_code=404)

    if status_code != 200:
        # State 1b: any other unexpected status (5xx, 401/403 from a
        # misconfigured secret, etc.) is not one of the three defined
        # states — treated the same as "unreachable": cache fallback, else
        # 503. The proxy's own 503 is never dressed up as anything else.
        logger.warning(
            "Companion proxy: %s returned unexpected HTTP %d for %s",
            state.service_url, status_code, resolved_upstream,
        )
        cached = cache.get(cache_key)
        if cached is not None:
            logger.warning(
                "Companion proxy: serving cached response for %s after upstream HTTP %d",
                resolved_upstream, status_code,
            )
            return JSONResponse(content=cached["body"], status_code=cached["status_code"])
        raise HTTPException(
            status_code=503,
            detail=(
                f"The marine service returned an unexpected response for "
                f"{resolved_upstream} and no cached response is available."
            ),
        )

    # State 2: HTTP 200 — including a null payload carrying
    # modelStatus: "unavailable". No modelStatus-specific branch exists for
    # caching/response purposes: a 200 is a successful proxied response and
    # is cached like any other. C-10: still scan the raw body for a model
    # gap and forward it (see _report_model_gaps_from_response() below) —
    # this does not affect what is cached or returned.
    if manifest_entry["path"] == "/fishing/{location_id}" and isinstance(body, dict):
        # The dense CO-OPS chart series belongs to the public API response,
        # rather than the bounded private scoring handoff.  It is attached
        # before the normal response conversion/envelope pipeline so its
        # tide-height units receive the same handling as every other marine
        # payload field.
        body = {**body, "tidePredictions": tide_predictions}
    _report_model_gaps_from_response(body, manifest_entry=manifest_entry)
    transformed = _apply_response_transform(body, manifest_entry=manifest_entry)
    cache.set(cache_key, {"body": transformed, "status_code": 200}, ttl_seconds)
    return JSONResponse(content=transformed, status_code=200)


# ---------------------------------------------------------------------------
# Dynamic route (re)registration
# ---------------------------------------------------------------------------


def _build_route(state: CompanionProxyState, path: str, manifest_entry: dict[str, Any]) -> APIRoute:
    full_path = f"{_API_PREFIX}{path}"

    def _handler(request: Request, _entry: dict[str, Any] = manifest_entry) -> JSONResponse:
        return _proxy_request(request, state, _entry)

    return APIRoute(
        full_path,
        _handler,
        methods=["GET"],
        name=f"{_ROUTE_NAME_PREFIX}{path}",
        # No static response_model for a dynamically-mounted, per-manifest
        # shape; also avoids publishing a misleading OpenAPI schema for a
        # route this process didn't define the shape of.
        include_in_schema=False,
    )


def _reconcile_routes(app: FastAPI, state: CompanionProxyState, manifest: dict[str, Any]) -> None:
    """Rebuild the companion-proxy portion of app.router.routes from a
    freshly-fetched manifest. Always replaces the routes list wholesale
    with a NEW list object (never mutates in place) so an in-flight
    request iterating the previous list object is unaffected — see module
    docstring "Middleware parity" and the class docstring above.
    """
    new_entries = _valid_manifest_entries(manifest)

    # T6.3: capabilities are refreshed on every successful fetch, independent
    # of whether the route set changed below — a manifest that adds/removes
    # a capability without touching endpoints must still be reflected.
    raw_capabilities = manifest.get("capabilities")
    new_capabilities = (
        [c for c in raw_capabilities if isinstance(c, str)]
        if isinstance(raw_capabilities, list)
        else []
    )
    with state.lock:
        state.capabilities = new_capabilities

        if new_entries == state.registered:
            return  # nothing changed — skip the route-list rebuild entirely

        old_paths = set(state.registered)
        new_paths = set(new_entries)
        removed = old_paths - new_paths
        added = new_paths - old_paths
        changed = {
            p for p in (old_paths & new_paths)
            if state.registered[p] != new_entries[p]
        }

        base_routes = [
            route for route in app.router.routes
            if not str(getattr(route, "name", "")).startswith(_ROUTE_NAME_PREFIX)
        ]
        new_route_list = list(base_routes)
        for path, entry in new_entries.items():
            new_route_list.append(_build_route(state, path, entry))
        app.router.routes = new_route_list

        state.registered = new_entries

    for path in sorted(removed):
        logger.info(
            "Companion proxy: de-registered %s%s (removed from manifest)", _API_PREFIX, path,
        )
    for path in sorted(added):
        entry = new_entries[path]
        logger.info(
            "Companion proxy: registered %s%s -> %s (cache_ttl=%ds)",
            _API_PREFIX, path, entry["upstream"], entry["cache_ttl"],
        )
    for path in sorted(changed):
        entry = new_entries[path]
        logger.info(
            "Companion proxy: re-registered %s%s -> %s (cache_ttl=%ds, manifest entry changed)",
            _API_PREFIX, path, entry["upstream"], entry["cache_ttl"],
        )


# ---------------------------------------------------------------------------
# Startup + periodic refresh
# ---------------------------------------------------------------------------


def _refresh_loop(app: FastAPI, state: CompanionProxyState) -> None:
    """Background daemon thread: re-fetch the manifest every 5 minutes and
    reconcile routes. Also the startup-retry mechanism (plan Do item 4) —
    a manifest fetch that fails here logs an ERROR and simply leaves the
    existing (possibly empty) route set in place until the next tick.
    """
    while True:
        time.sleep(_MANIFEST_REFRESH_INTERVAL_S)
        manifest = _fetch_manifest(state)
        if manifest is None:
            # T6.3 / API-MANUAL §19.4: routes stay mounted (stale cache
            # fallback per T6.1), but capabilities ARE removed on a failed
            # refresh — "the next manifest fetch will detect the absence
            # and remove marine capabilities from the response."
            with state.lock:
                state.capabilities = []
            logger.error(
                "Companion proxy: periodic manifest refresh from %s failed; "
                "retaining existing routes, clearing capabilities, retrying in %ds",
                state.service_url, _MANIFEST_REFRESH_INTERVAL_S,
            )
            continue
        _reconcile_routes(app, state, manifest)


def register_companion_proxy(app: FastAPI, settings: Settings) -> None:
    """Wire the companion proxy into `app` (call from create_app()).

    No-op when [providers] marine_service_url is not configured (plan Do
    item 3) — no manifest fetch, no marine routes, no background thread.
    """
    global _active_state  # noqa: PLW0603

    marine_url = settings.providers.marine_service_url
    if not marine_url:
        logger.debug(
            "Companion proxy: marine_service_url not configured — no marine routes mounted"
        )
        return

    verify_tls = settings.providers.marine_verify_tls
    if not verify_tls:
        # One unambiguous WARNING at startup, naming the host — not
        # per-request (rules/coding.md; API-MANUAL §19.2 / OPERATIONS-MANUAL
        # "Marine service TLS"). TLS encryption itself is unaffected; only
        # certificate verification is skipped.
        logger.warning(
            "Companion proxy: marine_verify_tls=false — TLS certificate "
            "verification is DISABLED for requests to %s (encryption stays "
            "active; only certificate verification is skipped)",
            marine_url,
        )

    state = CompanionProxyState(service_url=marine_url, verify_tls=verify_tls)
    _active_state = state

    manifest = _fetch_manifest(state)
    if manifest is None:
        logger.error(
            "Companion proxy: marine service at %s unreachable at startup — "
            "starting without marine routes; retrying every %ds",
            marine_url, _MANIFEST_REFRESH_INTERVAL_S,
        )
    else:
        _reconcile_routes(app, state, manifest)

    thread = threading.Thread(
        target=_refresh_loop,
        args=(app, state),
        daemon=True,
        name="companion-proxy-manifest-refresh",
    )
    thread.start()


def get_marine_capabilities() -> list[str]:
    """Return the marine service's currently-known capability id list (T6.3).

    Empty when the companion proxy is not configured, has never completed a
    successful manifest fetch, or the most recent periodic refresh failed
    (API-MANUAL §19.4 — capabilities are removed, not served stale, on a
    failed refresh; contrast with `registered`'s routes, which stay mounted
    against the stale cache). Consumed by endpoints/capabilities.py; that
    endpoint does not make a second call into the marine service — this is
    the manifest already fetched and cached by this module.
    """
    if _active_state is None:
        return []
    with _active_state.lock:
        return list(_active_state.capabilities)


def reset_companion_proxy_for_tests() -> None:
    """Reset module-level state. Used in tests only."""
    global _active_state  # noqa: PLW0603
    _active_state = None


# ---------------------------------------------------------------------------
# Gap reporting (C-10) — ported from providers/nearshore/swan.py's
# report_gap()/_gap_report_worker(). swan.py was deleted by T6.6; its call
# sites (endpoints/surf.py, endpoints/beach_profile.py — also deleted) are
# ported to _report_model_gaps_from_response() below. See module docstring
# "Gap reporting (C-10)".
# ---------------------------------------------------------------------------

#: Same bounds as the ported reference — neither a single large gap burst
#: nor a refresh loop hitting the same missing timestep repeatedly can grow
#: either structure without limit.
_GAP_REPORT_QUEUE_MAXSIZE = 256
_GAP_REPORT_DEDUP_MAXSIZE = 256
_GAP_REPORT_TIMEOUT_S = 2.0

_GapReportKey = tuple[str, str, str, str | None]

_gap_report_queue: _queue.Queue[_GapReportKey] = _queue.Queue(maxsize=_GAP_REPORT_QUEUE_MAXSIZE)
_gap_report_worker_thread: threading.Thread | None = None
_gap_report_worker_lock = threading.Lock()

_gap_report_seen: OrderedDict[_GapReportKey, None] = OrderedDict()
_gap_report_seen_lock = threading.Lock()


def _gap_report_worker() -> None:
    """Long-lived daemon thread: drain _gap_report_queue, POST each report."""
    while True:
        spot_id, valid_time, endpoint, run_time = _gap_report_queue.get()
        try:
            state = _active_state
            if state is not None:
                httpx.post(
                    f"{state.service_url}/report/gap",
                    json={
                        "spot_id": spot_id,
                        "valid_time": valid_time,
                        "endpoint": endpoint,
                        "run_time": run_time,
                    },
                    headers=_auth_headers(),
                    verify=state.verify_tls,
                    timeout=_GAP_REPORT_TIMEOUT_S,
                )
        except Exception:
            # Fire-and-forget per module docstring — a failure here must
            # never propagate anywhere; DEBUG-level, matching the ported
            # reference exactly (a broken gap reporter is silent by design,
            # not by accident).
            logger.debug(
                "Companion proxy: gap report failed for %r @ %s (%s)",
                spot_id, valid_time, endpoint, exc_info=True,
            )
        finally:
            _gap_report_queue.task_done()


def _ensure_gap_report_worker_started() -> None:
    global _gap_report_worker_thread  # noqa: PLW0603
    if _gap_report_worker_thread is not None and _gap_report_worker_thread.is_alive():
        return
    with _gap_report_worker_lock:
        if _gap_report_worker_thread is not None and _gap_report_worker_thread.is_alive():
            return
        _gap_report_worker_thread = threading.Thread(
            target=_gap_report_worker, daemon=True, name="companion-proxy-gap-report-worker",
        )
        _gap_report_worker_thread.start()


def report_gap(spot_id: str, valid_time: str, endpoint: str, run_time: str | None) -> None:
    """Fire-and-forget gap report to the marine service's POST /report/gap.

    No-op when the companion proxy is not configured (mirrors
    providers/nearshore/swan.py's report_gap() "if not _remote_url: return"
    contract). Deduplicated per (spot_id, valid_time, endpoint, run_time)
    with a bounded LRU, then handed to the single background worker via a
    non-blocking, bounded queue. A full queue drops the report and logs
    once at DEBUG; this function never blocks or raises into the caller's
    request path.
    """
    if _active_state is None:
        return

    key: _GapReportKey = (spot_id, valid_time, endpoint, run_time)
    with _gap_report_seen_lock:
        if key in _gap_report_seen:
            return
        _gap_report_seen[key] = None
        _gap_report_seen.move_to_end(key)
        while len(_gap_report_seen) > _GAP_REPORT_DEDUP_MAXSIZE:
            _gap_report_seen.popitem(last=False)

    _ensure_gap_report_worker_started()
    try:
        _gap_report_queue.put_nowait(key)
    except _queue.Full:
        logger.debug(
            "Companion proxy: gap report queue full (cap=%d) -- dropping report "
            "for %r @ %s (%s)",
            _GAP_REPORT_QUEUE_MAXSIZE, spot_id, valid_time, endpoint,
        )


def _report_model_gaps_from_response(body: Any, *, manifest_entry: dict[str, Any]) -> None:
    """C-10: the call site ``report_gap()`` above was missing until T6.6.

    Detects a model gap in a raw (pre-conversion) marine-service response
    and forwards it via ``report_gap()`` to the marine service's own
    ``POST /report/gap`` (``weewx-clearskies-marine``'s ``endpoints/gap.py``).
    A duplicate of what the marine service already logged in-process
    (``endpoints/surf.py``'s ``_report_forecast_gap()``, ``endpoints/
    beach_profile.py``'s equivalent) is harmless — both land in the same
    ``_record_gap_report()`` dedup keyed on (spot_id, valid_time, endpoint,
    run_time), so this never double-logs. What it does add: visibility for
    gaps the API observes on a proxied request that the marine service's
    own request handler already returned (e.g. a stale/cached 200 the API
    is re-serving would not re-trigger this, since only a fresh upstream
    200 reaches this function) and, more importantly, keeps the API-side
    ``report_gap()`` machinery ported in T6.1 from being permanently
    unwired dead code.

    Only two manifest paths ever carry a "modelStatus" signal — grep-
    verified against ``weewx_clearskies_marine/endpoints/*.py``; marine,
    tides, fishing, and beach-safety responses never set it:

      - ``/surf/{location_id}/profile``: a single dict with top-level
        ``modelStatus``/``locationId``/``timestep`` (mirrors the deleted
        ``endpoints/beach_profile.py``'s ``_unavailable_profile_response()``
        call site).
      - ``/surf/{location_id}``: a dict with a ``forecast`` list of
        entries, each carrying its own ``modelStatus``/``time`` (mirrors
        the deleted ``endpoints/surf.py``'s per-timestep
        ``swan.report_gap(endpoint="forecast", ...)`` call site).

    Deliberately not a generic JSON walk — this is a one-for-one port of
    the two specific call sites the old in-process endpoints had, not a
    new detection strategy.
    """
    if not isinstance(body, dict):
        return

    path = manifest_entry.get("path", "")

    if path.endswith("/profile"):
        if body.get("modelStatus") == "unavailable":
            report_gap(
                spot_id=body.get("locationId") or "",
                valid_time=body.get("timestep") or "",
                endpoint="profile",
                run_time=body.get("lastRunTime"),
            )
        return

    if path == "/surf/{location_id}":
        spot_id = body.get("locationId") or ""
        run_time = body.get("lastRunTime")
        for entry in body.get("forecast") or []:
            if isinstance(entry, dict) and entry.get("modelStatus") == "unavailable":
                report_gap(
                    spot_id=spot_id,
                    valid_time=entry.get("time") or "",
                    endpoint="forecast",
                    run_time=run_time,
                )
