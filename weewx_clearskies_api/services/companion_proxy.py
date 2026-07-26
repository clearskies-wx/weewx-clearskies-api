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

**Envelope wrapping and unit conversion are T6.2, NOT this module.** Every
proxied response passes through ``_apply_response_transform()`` — currently
an identity function — before being returned to the caller. This is the
seam T6.2 replaces with envelope wrapping (``data``/``stationClock``/
``freshness``/``units``) and SI→display-unit conversion (API-MANUAL §19.3).
Do not add conversion/envelope logic anywhere else in this file; the single
call site is the contract for where it plugs in.

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

**Gap reporting (C-10).** ``POST /report/gap`` is intentionally NOT in the
marine service's manifest (see ``weewx-clearskies-marine``'s
``endpoints/gap.py`` docstring — it is a fire-and-forget POST with no TTL
and no cacheable resource, so the manifest schema has nothing to describe).
The API calls it directly via ``report_gap()`` below, which is a straight
port of ``providers/nearshore/swan.py``'s ``report_gap()`` /
``_gap_report_worker()`` (same dedup LRU bound, same bounded queue, same
single background worker, same fire-and-forget contract — a broken client
here would fail silently, exactly like the original). ``swan.py`` itself is
left untouched; T6.6 deletes it later in Phase 6, at which point its call
sites move to this module's ``report_gap()``. No caller wires this function
yet — that wiring is T6.6's job, not this task's.

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

import logging
import os
import queue as _queue
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from weewx_clearskies_api.config.settings import Settings
from weewx_clearskies_api.providers._common.cache import get_cache

logger = logging.getLogger(__name__)

#: Per ADR-027 §3 / API-MANUAL §19.2 — never stored in Settings or api.conf,
#: read fresh from the environment at every call site.
MARINE_SERVICE_SECRET_ENV_VAR = "MARINE_SERVICE_SECRET"  # noqa: S105 — env var name, not a secret value

#: Manifest refresh cadence AND the startup-retry cadence when the marine
#: service is unreachable at startup (plan Do items 4 and 6 share one
#: clock — see register_companion_proxy()).
_MANIFEST_REFRESH_INTERVAL_S = 300

_MANIFEST_FETCH_TIMEOUT_S = 5.0
_PROXY_REQUEST_TIMEOUT_S = 15.0
_API_PREFIX = "/api/v1"
#: Marker prefix on every route this module registers, so route
#: reconciliation can rebuild "everything except ours" without touching
#: native routers' entries in app.router.routes.
_ROUTE_NAME_PREFIX = "companion_proxy:"


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

    def __init__(self, *, service_url: str) -> None:
        self.service_url = service_url.rstrip("/")
        #: path (manifest "path", e.g. "/surf/{location_id}") -> manifest entry.
        #: Guarded by _lock; read under lock, replaced wholesale on each
        #: successful reconciliation (never mutated in place).
        self.registered: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()


#: Set by register_companion_proxy() when marine_service_url is configured;
#: None otherwise. report_gap() below is a no-op when this is None, mirroring
#: providers/nearshore/swan.py's "if not _remote_url: return" contract.
_active_state: CompanionProxyState | None = None


# ---------------------------------------------------------------------------
# Response transform seam (T6.2 plugs in here — identity for now)
# ---------------------------------------------------------------------------


def _apply_response_transform(body: Any, *, manifest_entry: dict[str, Any]) -> Any:
    """Identity passthrough. T6.2 replaces this body with envelope wrapping
    + SI->display-unit conversion (API-MANUAL §19.3). Every proxied 200
    response flows through this single call site — do not add
    conversion/envelope logic anywhere else in this module.
    """
    return body


# ---------------------------------------------------------------------------
# Manifest fetch
# ---------------------------------------------------------------------------


def _fetch_manifest(state: CompanionProxyState) -> dict[str, Any] | None:
    """GET {service_url}/manifest (no auth). Returns None on any failure —
    network error, non-2xx, or a body that isn't valid JSON — logging the
    reason. Never raises.
    """
    try:
        with httpx.Client(timeout=_MANIFEST_FETCH_TIMEOUT_S, verify=True) as client:
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
        with httpx.Client(timeout=_PROXY_REQUEST_TIMEOUT_S, verify=True) as client:
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


def _cache_key(service_url: str, resolved_upstream: str, query_params: Any) -> str:
    sorted_query = "&".join(f"{k}={v}" for k, v in sorted(dict(query_params).items()))
    return f"companion_proxy:{service_url}:{resolved_upstream}?{sorted_query}"


async def _proxy_request(
    request: Request, state: CompanionProxyState, manifest_entry: dict[str, Any]
) -> JSONResponse:
    """The three-state rule, implemented. See module docstring."""
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

    cache = get_cache()
    cache_key = _cache_key(state.service_url, resolved_upstream, request.query_params)

    fetch_result = _fetch_upstream(state, resolved_upstream, request.query_params)

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
    # modelStatus: "unavailable". No modelStatus-specific branch exists:
    # a 200 is a successful proxied response and is cached like any other.
    transformed = _apply_response_transform(body, manifest_entry=manifest_entry)
    cache.set(cache_key, {"body": transformed, "status_code": 200}, ttl_seconds)
    return JSONResponse(content=transformed, status_code=200)


# ---------------------------------------------------------------------------
# Dynamic route (re)registration
# ---------------------------------------------------------------------------


def _build_route(state: CompanionProxyState, path: str, manifest_entry: dict[str, Any]) -> APIRoute:
    full_path = f"{_API_PREFIX}{path}"

    async def _handler(request: Request, _entry: dict[str, Any] = manifest_entry) -> JSONResponse:
        return await _proxy_request(request, state, _entry)

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

    with state.lock:
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
            logger.error(
                "Companion proxy: periodic manifest refresh from %s failed; "
                "retaining existing routes, retrying in %ds",
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

    state = CompanionProxyState(service_url=marine_url)
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


def reset_companion_proxy_for_tests() -> None:
    """Reset module-level state. Used in tests only."""
    global _active_state  # noqa: PLW0603
    _active_state = None


# ---------------------------------------------------------------------------
# Gap reporting (C-10) — ported from providers/nearshore/swan.py's
# report_gap()/_gap_report_worker() (~line 1277). swan.py is untouched;
# T6.6 deletes it later in Phase 6 and moves its call sites here. See
# module docstring "Gap reporting (C-10)".
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
                    verify=True,
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
