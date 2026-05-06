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
      1. RequestIdMiddleware     — outermost; establishes request_id for all logs
      2. BodySizeLimitMiddleware — reject oversized bodies early (before auth/rate)
      3. ProxyAuthMiddleware     — mark request as trusted BEFORE rate-limit check
      4. RateLimitMiddleware     — bypass when proxy_trusted is True
      5. CORSMiddleware          — standard Starlette CORS
      6. SecurityHeadersMiddleware — inject security headers on every response

    The innermost layer is FastAPI's own router. Error handlers registered
    via register_error_handlers() wrap the router.

    Registration order in the code below is the REVERSE of execution order
    (SecurityHeaders registered first = innermost; RequestId registered last =
    outermost). See the block comment in create_app() for detail.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from weewx_clearskies_api.config.settings import Settings
from weewx_clearskies_api.endpoints.station import router as station_router
from weewx_clearskies_api.errors import register_error_handlers
from weewx_clearskies_api.middleware.body_size_limit import BodySizeLimitMiddleware
from weewx_clearskies_api.middleware.proxy_auth import ProxyAuthMiddleware
from weewx_clearskies_api.middleware.rate_limit import RateLimitMiddleware
from weewx_clearskies_api.middleware.request_id import RequestIdMiddleware
from weewx_clearskies_api.middleware.security_headers import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    """Create and configure the public API FastAPI application.

    Args:
        settings: Validated Settings instance from config/settings.py.

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
    )

    # Register RFC 9457 error handlers (ADR-018).
    register_error_handlers(app)

    # Register API routers.
    # All endpoints at /api/v1/... per ADR-018.
    app.include_router(station_router, prefix="/api/v1")

    # ---------------------------------------------------------------------------
    # Middleware registration order.
    #
    # Starlette's add_middleware() PREPENDS to user_middleware (insert at index 0).
    # Therefore the LAST add_middleware() call becomes user_middleware[0] and
    # executes as the OUTERMOST wrapper (first to handle the incoming request).
    #
    # We want execution order: 1=RequestId (outer) → ... → 6=SecurityHeaders (inner)
    # So we register in REVERSE: SecurityHeaders first, RequestId last.
    #
    #   1st add_middleware call → SecurityHeadersMiddleware → executes innermost (step 6)
    #   2nd add_middleware call → CORSMiddleware            → executes step 5
    #   3rd add_middleware call → RateLimitMiddleware       → executes step 4
    #   4th add_middleware call → ProxyAuthMiddleware       → executes step 3
    #   5th add_middleware call → BodySizeLimitMiddleware   → executes step 2
    #   6th add_middleware call → RequestIdMiddleware       → executes outermost (step 1)
    # ---------------------------------------------------------------------------

    # 1st add_middleware call → executes innermost (step 6)
    app.add_middleware(SecurityHeadersMiddleware)

    # 2nd add_middleware call → executes step 5 (CORS)
    # Default: same-origin only (no allow_origins).
    # Operator extends via [api] cors_origins in api.conf.
    cors_origins = settings.api.cors_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,  # API has no cookies/credentials per ADR-008.
            allow_methods=["GET", "HEAD", "OPTIONS"],
            allow_headers=["*"],
        )
    else:
        # Same-origin only: add CORS middleware with empty origins so it
        # processes OPTIONS but rejects cross-origin requests.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[],
            allow_credentials=False,
            allow_methods=["GET", "HEAD", "OPTIONS"],
            allow_headers=["*"],
        )

    # 3rd add_middleware call → executes step 4 (rate limit; bypass when proxy_trusted is True)
    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=settings.ratelimit.requests_per_minute,
        window_seconds=settings.ratelimit.window_seconds,
    )

    # 4th add_middleware call → executes step 3 (proxy auth; sets proxy_trusted before rate-limit)
    app.add_middleware(ProxyAuthMiddleware)

    # 5th add_middleware call → executes step 2 (body size limit)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.api.max_request_bytes,
    )

    # 6th add_middleware call → executes outermost (step 1; establishes request_id for all logs)
    app.add_middleware(RequestIdMiddleware)

    logger.info(
        "Public API app created",
        extra={
            "bind_host": settings.api.bind_host,
            "bind_port": settings.api.bind_port,
            "cors_origins": cors_origins,
        },
    )

    return app
