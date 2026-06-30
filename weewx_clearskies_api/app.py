"""FastAPI application factory.

Usage:
    from weewx_clearskies_api.app import create_app
    app = create_app(settings)

IPv4/IPv6 dual-stack listener (coding.md §1):
    Default bind = 127.0.0.1 per ADR-037. When operator overrides [api] bind
    to a non-loopback host, the caller (see __main__.py) resolves via
    socket.getaddrinfo and passes each (family, address) result to a separate
    uvicorn Server instance. The app itself is IP-agnostic — it's uvicorn's
    job to accept connections on both stacks.

Middleware execution order (outermost → innermost):
    Starlette's add_middleware() PREPENDS entries, so the LAST call becomes
    the outermost wrapper (first to process each incoming request).

    Execution order (comment must stay in sync with middleware/__init__.py):
      0. MetricsMiddleware       — outermost; times total request including request-id gen
      1. RequestIdMiddleware     — establishes request_id for all logs
      2. BodySizeLimitMiddleware — reject oversized bodies early (before auth)
      3. ProxyAuthMiddleware     — mark request as trusted via X-Clearskies-Proxy-Auth
      4. CORSMiddleware          — standard Starlette CORS
      5. SecurityHeadersMiddleware — inject security headers on every response

    The innermost layer is FastAPI's own router. Error handlers registered
    via register_error_handlers() wrap the router.

    Registration order in the code below is the REVERSE of execution order
    (SecurityHeaders registered first = innermost; MetricsMiddleware registered
    last = outermost). See the block comment in create_app() for detail.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from weewx_clearskies_api.config.settings import Settings
from weewx_clearskies_api.endpoints.alerts import router as alerts_router
from weewx_clearskies_api.endpoints.almanac import router as almanac_router
from weewx_clearskies_api.endpoints.aqi import router as aqi_router
from weewx_clearskies_api.endpoints.archive_grouped import router as archive_grouped_router
from weewx_clearskies_api.endpoints.branding import router as branding_router
from weewx_clearskies_api.endpoints.capabilities import router as capabilities_router
from weewx_clearskies_api.endpoints.charts import router as charts_router
from weewx_clearskies_api.endpoints.content import router as content_router
from weewx_clearskies_api.endpoints.custom_query import router as custom_query_router
from weewx_clearskies_api.endpoints.earthquakes import router as earthquakes_router
from weewx_clearskies_api.endpoints.forecast import router as forecast_router
from weewx_clearskies_api.endpoints.observations import router as observations_router
from weewx_clearskies_api.endpoints.pages import router as pages_router
from weewx_clearskies_api.endpoints.radar import router as radar_router
from weewx_clearskies_api.endpoints.records import router as records_router
from weewx_clearskies_api.endpoints.reports import router as reports_router
from weewx_clearskies_api.endpoints.seeing import router as seeing_router
from weewx_clearskies_api.endpoints.setup import router as setup_router
from weewx_clearskies_api.endpoints.sse import router as sse_router
from weewx_clearskies_api.endpoints.station import router as station_router
from weewx_clearskies_api.errors import register_error_handlers
from weewx_clearskies_api.metrics import current_endpoint
from weewx_clearskies_api.middleware.body_size_limit import BodySizeLimitMiddleware
from weewx_clearskies_api.middleware.metrics import MetricsMiddleware
from weewx_clearskies_api.middleware.proxy_auth import ProxyAuthMiddleware
from weewx_clearskies_api.middleware.request_id import RequestIdMiddleware
from weewx_clearskies_api.middleware.security_headers import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)


def _set_endpoint_context(request: Request) -> None:
    """Set the current_endpoint ContextVar for DB metrics labeling (ADR-031).

    Runs as a sync app-level dependency so it executes in the same thread as
    sync route handlers, ensuring the ContextVar is visible to SQLAlchemy
    event listeners when they fire during query execution.

    Uses route.path (the route template, e.g. "/api/v1/archive") rather than
    request.url.path (the concrete URL with substituted path params) to keep
    the endpoint label low-cardinality.
    """
    route = request.scope.get("route")
    if route and hasattr(route, "path"):
        current_endpoint.set(route.path)  # type: ignore[union-attr]


def create_app(settings: Settings) -> FastAPI:
    """Create and configure the public API FastAPI application.

    Args:
        settings: Validated Settings instance from config/settings.py.
            When settings.configured is False, only setup endpoints and a minimal
            status endpoint are mounted; all data routers are omitted.

    Returns:
        Configured FastAPI app ready for uvicorn to serve.
    """
    app = FastAPI(
        title="weewx-clearskies-api",
        version="1.0.0",
        description="HTTP/JSON API for the Clear Skies weather dashboard",
        license_info={
            "name": "GPL-3.0-or-later",
            "url": "https://www.gnu.org/licenses/gpl-3.0.html",
        },
        # Per ADR-018: OpenAPI at /api/v1/openapi.json, docs at /api/v1/docs.
        root_path="",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        # extra="forbid" on Pydantic models handles input validation at endpoints.
        # FastAPI's own validation layer is active by default.
        # ADR-031: set current_endpoint ContextVar so DB metrics carry the route
        # template label instead of "unknown".
        dependencies=[Depends(_set_endpoint_context)],
    )

    # Register RFC 9457 error handlers (ADR-018).
    register_error_handlers(app)

    # GET /api/v1/status works in both modes — lets the dashboard detect setup state.
    configured_flag = settings.configured

    @app.get("/api/v1/status")
    async def status() -> dict[str, bool]:
        return {"configured": configured_flag}

    if not settings.configured:
        # Setup mode: only the setup router and the catch-all 503 are mounted.
        # No DB, no providers, no data routers.
        app.include_router(setup_router)

        @app.api_route(
            "/api/v1/{path:path}",
            methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
        async def _not_configured(path: str, request: Request) -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "type": "urn:clearskies:not-configured",
                    "title": "Clear Skies is not configured",
                    "detail": "Run the setup wizard to configure this installation.",
                    "status": 503,
                },
                media_type="application/problem+json",
            )

        logger.info("Public API app created in setup mode (unconfigured)")
    else:
        # Configured mode: mount all data routers.
        # Route ordering: /reports/{year}/{month} BEFORE /reports/{year} so
        # FastAPI matches the more-specific path first (brief §6 note).
        app.include_router(station_router, prefix="/api/v1")
        app.include_router(observations_router, prefix="/api/v1")
        app.include_router(records_router, prefix="/api/v1")
        # reports_router declares monthly before yearly internally; both included here.
        app.include_router(reports_router, prefix="/api/v1")
        # 3a-2 new routers.
        app.include_router(almanac_router, prefix="/api/v1")
        app.include_router(capabilities_router, prefix="/api/v1")
        app.include_router(pages_router, prefix="/api/v1")
        app.include_router(charts_router, prefix="/api/v1")
        app.include_router(content_router, prefix="/api/v1")
        # 3b-1 new routers.
        app.include_router(alerts_router, prefix="/api/v1")
        # 3b-2 new routers.
        app.include_router(forecast_router, prefix="/api/v1")
        # 3b-9 new routers.
        app.include_router(aqi_router, prefix="/api/v1")
        # 3b-13 new routers.
        app.include_router(earthquakes_router, prefix="/api/v1")
        # 3b-14 new routers.
        app.include_router(radar_router, prefix="/api/v1")
        # Gap #10 (ADR-022): branding configuration endpoint.
        app.include_router(branding_router, prefix="/api/v1")
        # Archive grouped: general-purpose time-bucketed aggregation.
        app.include_router(archive_grouped_router, prefix="/api/v1")
        # Seeing forecast: 7Timer 72-hour astronomical seeing forecast.
        app.include_router(seeing_router, prefix="/api/v1")
        # Custom SQL query: operator-defined series from charts.conf.
        app.include_router(custom_query_router, prefix="/api/v1")

        # ADR-058: SSE stream — no /api/v1 prefix; endpoint lives at /sse.
        # Dashboard connects to /sse via Caddy proxy; prefix must match.
        app.include_router(sse_router)

        # Setup endpoints — no /api/v1 prefix (separate surface per ADR-038).
        app.include_router(setup_router)

        logger.info(
            "Public API app created",
            extra={
                "bind_host": settings.api.bind_host,
                "bind_port": settings.api.bind_port,
                "cors_origins": settings.api.cors_origins,
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------------------------------------------------------------------
    # Middleware registration order.
    #
    # Starlette's add_middleware() PREPENDS to user_middleware (insert at index 0).
    # Therefore the LAST add_middleware() call becomes user_middleware[0] and
    # executes as the OUTERMOST wrapper (first to handle the incoming request).
    #
    # We want execution order: 0=Metrics (outermost) → 1=RequestId → ...
    #   → 5=SecurityHeaders (innermost).
    # So we register in REVERSE: SecurityHeaders first, MetricsMiddleware last.
    #
    #   1st add_middleware call → SecurityHeadersMiddleware → executes innermost (step 5)
    #   2nd add_middleware call → CORSMiddleware            → executes step 4
    #   3rd add_middleware call → ProxyAuthMiddleware       → executes step 3
    #   4th add_middleware call → BodySizeLimitMiddleware   → executes step 2
    #   5th add_middleware call → RequestIdMiddleware       → executes step 1
    #   6th add_middleware call → MetricsMiddleware         → executes outermost (step 0)
    # ---------------------------------------------------------------------------

    # 1st add_middleware call → executes innermost (step 5)
    app.add_middleware(SecurityHeadersMiddleware)

    # 2nd add_middleware call → executes step 4 (CORS)
    # Default: same-origin only (no allow_origins).
    # Operator extends via [api] cors_origins in api.conf.
    cors_origins = settings.api.cors_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,  # API has no cookies/credentials per ADR-008.
            allow_methods=["GET", "HEAD", "OPTIONS", "POST"],
            allow_headers=["*"],
        )
    else:
        # Same-origin only: add CORS middleware with empty origins so it
        # processes OPTIONS but rejects cross-origin requests.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[],
            allow_credentials=False,
            allow_methods=["GET", "HEAD", "OPTIONS", "POST"],
            allow_headers=["*"],
        )

    # 3rd add_middleware call → executes step 3 (proxy auth; sets proxy_trusted flag)
    app.add_middleware(ProxyAuthMiddleware)

    # 4th add_middleware call → executes step 2 (body size limit)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.api.max_request_bytes,
    )

    # 5th add_middleware call → executes step 1 (request-id; establishes request_id for all logs)
    app.add_middleware(RequestIdMiddleware)

    # 6th add_middleware call → executes outermost (step 0; ADR-031 HTTP timing wraps everything)
    app.add_middleware(MetricsMiddleware)

    return app
