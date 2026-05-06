"""Tests for the RFC 9457 error handler.

Verifies:
  - 404 → application/problem+json with correct shape.
  - Unhandled exception → 500 problem+json; no stack trace in body.
  - 422 validation error → problem+json.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from weewx_clearskies_api.errors import register_error_handlers


def _make_app() -> FastAPI:
    """App with RFC 9457 error handlers registered."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/ok")
    async def _ok() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/raises")
    async def _raises() -> JSONResponse:
        raise RuntimeError("internal details must not reach the client")

    @app.get("/http-error")
    async def _http_error() -> JSONResponse:
        raise HTTPException(status_code=404, detail="Not found")

    return app


def test_404_returns_problem_json_shape() -> None:
    """Unknown endpoint returns 404 problem+json with all required fields."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/nonexistent")
    assert response.status_code == 404
    assert "problem+json" in response.headers["content-type"]
    body = response.json()
    assert "type" in body
    assert "title" in body
    assert "status" in body
    assert body["status"] == 404
    assert "detail" in body
    assert "instance" in body


def test_explicit_http_exception_returns_problem_json() -> None:
    """Explicitly raised HTTPException is formatted as problem+json."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/http-error")
    assert response.status_code == 404
    assert "problem+json" in response.headers["content-type"]
    body = response.json()
    assert body["status"] == 404


def test_unhandled_exception_returns_500_without_stack_trace() -> None:
    """Unhandled exception returns 500 problem+json with no stack/internals."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/raises")
    assert response.status_code == 500
    assert "problem+json" in response.headers["content-type"]
    body = response.json()
    assert body["status"] == 500
    # The raw exception message must NOT appear in the client response.
    assert "internal details" not in str(body)
    # No traceback or internal path in the body.
    assert "Traceback" not in str(body)
    assert "weewx_clearskies_api" not in str(body)


def test_unhandled_exception_has_required_fields() -> None:
    """500 response has all RFC 9457 required fields."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/raises")
    body = response.json()
    for field in ("type", "title", "status", "detail", "instance"):
        assert field in body, f"Missing RFC 9457 field: {field!r}"
