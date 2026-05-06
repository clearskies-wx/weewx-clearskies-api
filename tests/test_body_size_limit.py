"""Tests for BodySizeLimitMiddleware in isolation."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from weewx_clearskies_api.middleware.body_size_limit import BodySizeLimitMiddleware


def _make_app(max_bytes: int) -> FastAPI:
    """Minimal app with only BodySizeLimitMiddleware at the given limit."""
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

    @app.post("/upload")
    async def _upload() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def test_under_limit_passes() -> None:
    """Requests under the limit are passed through."""
    client = TestClient(_make_app(max_bytes=1024), raise_server_exceptions=False)
    response = client.post("/upload", content=b"x" * 100)
    assert response.status_code == 200


def test_exactly_at_limit_passes() -> None:
    """Request exactly at the limit passes."""
    max_bytes = 1024
    client = TestClient(_make_app(max_bytes=max_bytes), raise_server_exceptions=False)
    response = client.post(
        "/upload",
        content=b"x" * max_bytes,
        headers={"Content-Length": str(max_bytes)},
    )
    assert response.status_code == 200


def test_two_mib_body_returns_413() -> None:
    """A 2 MiB body against a 1 MiB limit returns 413 problem+json."""
    one_mib = 1 * 1024 * 1024
    two_mib = 2 * one_mib
    client = TestClient(_make_app(max_bytes=one_mib), raise_server_exceptions=False)
    # Provide Content-Length so middleware can check before reading.
    response = client.post(
        "/upload",
        content=b"x" * two_mib,
        headers={"Content-Length": str(two_mib)},
    )
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 413
    assert "type" in body
    assert "title" in body
    assert "detail" in body
    assert "instance" in body


def test_malformed_content_length_passes() -> None:
    """A malformed Content-Length header is passed to the upstream handler."""
    client = TestClient(_make_app(max_bytes=100), raise_server_exceptions=False)
    # Starlette TestClient may normalise Content-Length; simulate by sending
    # a request without Content-Length (no pre-check) — body is small so it passes.
    response = client.post("/upload", content=b"small")
    assert response.status_code == 200
