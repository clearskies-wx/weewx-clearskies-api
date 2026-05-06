"""Tests for SecurityHeadersMiddleware in isolation."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from weewx_clearskies_api.middleware.security_headers import SecurityHeadersMiddleware


def _make_app() -> FastAPI:
    """Minimal app with only SecurityHeadersMiddleware."""
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/test")
    async def _test() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def test_x_content_type_options_nosniff() -> None:
    """X-Content-Type-Options: nosniff must be present on every response."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/test")
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_referrer_policy_no_referrer() -> None:
    """Referrer-Policy: no-referrer must be present on every response."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/test")
    assert response.headers.get("referrer-policy") == "no-referrer"


def test_server_header_suppressed() -> None:
    """The Server: header must not be present (fingerprinting suppression)."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/test")
    # server header should be absent or empty.
    assert "server" not in response.headers or response.headers.get("server") == ""


def test_hsts_not_set_by_api() -> None:
    """HSTS is the proxy's responsibility (ADR-037) — must NOT be set here."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/test")
    assert "strict-transport-security" not in response.headers


def test_csp_not_set_by_api() -> None:
    """CSP is the proxy's responsibility (ADR-037) — must NOT be set here."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/test")
    assert "content-security-policy" not in response.headers
