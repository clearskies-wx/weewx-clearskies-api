"""Tests for CORS middleware configuration.

Same-origin requests must always succeed.
Cross-origin requests are rejected when no cors_origins are configured.
Cross-origin requests are allowed when the origin matches cors_origins.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def _make_app_no_cors() -> FastAPI:
    """App with same-origin-only CORS (empty allow_origins)."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/test")
    async def _test() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def _make_app_with_cors(origins: list[str]) -> FastAPI:
    """App with specific allowed CORS origins."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/test")
    async def _test() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def test_no_origin_header_passes() -> None:
    """Requests without an Origin header (same-origin) pass always."""
    client = TestClient(_make_app_no_cors(), raise_server_exceptions=False)
    response = client.get("/test")
    assert response.status_code == 200


def test_same_origin_passes() -> None:
    """Same-origin request (Origin matches the server) passes."""
    client = TestClient(_make_app_no_cors(), raise_server_exceptions=False)
    response = client.get("/test", headers={"Origin": "http://testserver"})
    assert response.status_code == 200


def test_foreign_origin_rejected_when_not_configured() -> None:
    """An unconfigured foreign origin does not get an Access-Control-Allow-Origin header."""
    client = TestClient(_make_app_no_cors(), raise_server_exceptions=False)
    response = client.get("/test", headers={"Origin": "https://evil.example.com"})
    # With empty allow_origins, CORS middleware does NOT add ACAO header.
    assert "access-control-allow-origin" not in response.headers


def test_configured_origin_gets_allow_header() -> None:
    """A configured origin receives the Access-Control-Allow-Origin header."""
    allowed = "https://dashboard.example.com"
    client = TestClient(
        _make_app_with_cors([allowed]), raise_server_exceptions=False
    )
    response = client.get("/test", headers={"Origin": allowed})
    assert response.headers.get("access-control-allow-origin") == allowed


def test_unconfigured_origin_rejected_even_with_cors_list() -> None:
    """An origin not in the list is rejected even when cors_origins is set."""
    client = TestClient(
        _make_app_with_cors(["https://dashboard.example.com"]),
        raise_server_exceptions=False,
    )
    response = client.get("/test", headers={"Origin": "https://attacker.example.com"})
    assert "access-control-allow-origin" not in response.headers
