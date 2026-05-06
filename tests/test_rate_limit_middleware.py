"""Tests for RateLimitMiddleware in isolation and in the full stack.

Isolated tests verify RateLimitMiddleware mechanics (quota, Retry-After, bypass
flag) in a minimal 1-2-middleware app — fast and focused.

Full-stack test (test_proxy_bypass_fullstack) uses create_app() via the conftest
client fixture to verify the bypass works in the production middleware order.
This is the production guarantee; the isolated tests are unit coverage.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from weewx_clearskies_api.middleware.proxy_auth import ProxyAuthMiddleware
from weewx_clearskies_api.middleware.rate_limit import RateLimitMiddleware


def _make_app(rpm: int = 3, window: int = 60) -> FastAPI:
    """Minimal app with only RateLimitMiddleware at low quota for testing."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rpm, window_seconds=window)

    @app.get("/test")
    async def _test() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def _make_app_with_auth_and_rate_limit(secret: str, rpm: int = 1) -> FastAPI:
    """App with ProxyAuthMiddleware (outermost) + RateLimitMiddleware.

    Starlette processes add_middleware in reverse order, so we register
    rate-limit first (becomes outer) and then proxy-auth (becomes outermost).
    Wait — we need ProxyAuth to be outer so it sets proxy_trusted before
    rate-limit checks it. Register them in the right order:
      add_middleware(RateLimitMiddleware)   — registered first → executes second (inner)
      add_middleware(ProxyAuthMiddleware)   — registered last  → executes first (outer)
    """
    os.environ["WEEWX_CLEARSKIES_PROXY_SECRET"] = secret

    app = FastAPI()
    # Register rate-limit first → it becomes inner wrapper.
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rpm, window_seconds=60)
    # Register proxy-auth last → it becomes outer wrapper, sets proxy_trusted first.
    app.add_middleware(ProxyAuthMiddleware)

    @app.get("/test")
    async def _test() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def test_under_quota_passes() -> None:
    """Requests under the quota are passed through."""
    client = TestClient(_make_app(rpm=10), raise_server_exceptions=False)
    for _ in range(5):
        response = client.get("/test")
        assert response.status_code == 200


def test_exhausting_quota_returns_429() -> None:
    """After exhausting quota, next request returns 429 with Retry-After."""
    rpm = 3
    client = TestClient(_make_app(rpm=rpm), raise_server_exceptions=False)

    # Exhaust the quota.
    for _ in range(rpm):
        r = client.get("/test")
        assert r.status_code == 200

    # Next request should be rate-limited.
    r = client.get("/test")
    assert r.status_code == 429
    assert r.headers["content-type"] == "application/problem+json"
    assert "retry-after" in r.headers

    body = r.json()
    assert body["status"] == 429
    assert "type" in body
    assert "title" in body
    assert "detail" in body
    assert "instance" in body


def test_retry_after_is_positive_integer() -> None:
    """Retry-After header is a positive integer."""
    rpm = 1
    client = TestClient(_make_app(rpm=rpm), raise_server_exceptions=False)
    client.get("/test")  # exhaust
    r = client.get("/test")
    assert r.status_code == 429
    retry_after = int(r.headers["retry-after"])
    assert retry_after > 0


def test_trusted_request_bypasses_rate_limit() -> None:
    """Requests with a valid proxy secret bypass the rate limiter."""
    secret = "test-secret-for-rate-bypass"
    client = TestClient(
        _make_app_with_auth_and_rate_limit(secret=secret, rpm=1),
        raise_server_exceptions=False,
    )
    try:
        # First request without auth — exhausts quota (rpm=1).
        r1 = client.get("/test")
        assert r1.status_code == 200

        # Second request without auth — rate limited.
        r2 = client.get("/test")
        assert r2.status_code == 429

        # Third request with valid proxy auth — trusted, bypasses rate limit.
        r3 = client.get("/test", headers={"X-Clearskies-Proxy-Auth": secret})
        assert r3.status_code == 200
    finally:
        os.environ.pop("WEEWX_CLEARSKIES_PROXY_SECRET", None)


def test_proxy_bypass_fullstack() -> None:
    """Full-stack production guarantee: proxy bypass works through create_app().

    Unlike test_trusted_request_bypasses_rate_limit which exercises a
    hand-rolled 2-middleware mini-app, this test drives the real 6-middleware
    stack from create_app(). It proves that the production registration order
    in app.py keeps ProxyAuthMiddleware ahead of RateLimitMiddleware.

    Strategy: build a full app with rpm=2 so we exhaust quota in 2 anonymous
    requests without sending 61 requests, then confirm a 3rd request with
    a valid secret succeeds.
    """
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )

    secret = "fullstack-bypass-test-secret"
    os.environ["WEEWX_CLEARSKIES_PROXY_SECRET"] = secret
    try:
        settings = Settings(
            api=ApiSettings({}),
            health=HealthSettings({}),
            logging_settings=LoggingSettings({}),
            ratelimit=RateLimitSettings({"requests_per_minute": 2, "window_seconds": 60}),
            database=DatabaseSettings({}),
        )
        app = create_app(settings)
        full_client = TestClient(app, raise_server_exceptions=False)

        # Exhaust the 2-request quota anonymously.
        r1 = full_client.get("/api/v1/station")
        assert r1.status_code == 200
        r2 = full_client.get("/api/v1/station")
        assert r2.status_code == 200

        # Third anonymous request should be rate-limited.
        r3 = full_client.get("/api/v1/station")
        assert r3.status_code == 429

        # Fourth request with valid proxy secret must bypass the rate limit.
        r4 = full_client.get(
            "/api/v1/station",
            headers={"X-Clearskies-Proxy-Auth": secret},
        )
        assert r4.status_code == 200
    finally:
        os.environ.pop("WEEWX_CLEARSKIES_PROXY_SECRET", None)
