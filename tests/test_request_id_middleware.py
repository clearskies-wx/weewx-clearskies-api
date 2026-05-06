"""Tests for RequestIdMiddleware in isolation."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from weewx_clearskies_api.middleware.request_id import RequestIdMiddleware
from weewx_clearskies_api.logging.redaction_filter import request_id_var


def _make_minimal_app() -> FastAPI:
    """Minimal app with only RequestIdMiddleware."""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/test")
    async def _test() -> JSONResponse:
        return JSONResponse({"request_id": request_id_var.get("")})

    return app


def test_request_id_generated() -> None:
    """Every response carries an X-Request-ID header."""
    client = TestClient(_make_minimal_app(), raise_server_exceptions=False)
    response = client.get("/test")
    assert "x-request-id" in response.headers
    # Must be a valid UUID4.
    rid = response.headers["x-request-id"]
    parsed = uuid.UUID(rid)
    assert parsed.version == 4


def test_request_id_injected_into_context_var() -> None:
    """The request_id_var ContextVar is set during request handling."""
    client = TestClient(_make_minimal_app(), raise_server_exceptions=False)
    response = client.get("/test")
    body = response.json()
    header_id = response.headers["x-request-id"]
    assert body["request_id"] == header_id


def test_incoming_x_request_id_reused() -> None:
    """An incoming X-Request-ID header is honoured (not replaced)."""
    client = TestClient(_make_minimal_app(), raise_server_exceptions=False)
    response = client.get("/test", headers={"X-Request-ID": "my-custom-id"})
    assert response.headers["x-request-id"] == "my-custom-id"


def test_unique_id_per_request() -> None:
    """Each request gets a distinct ID when none is provided."""
    client = TestClient(_make_minimal_app(), raise_server_exceptions=False)
    ids = {client.get("/test").headers["x-request-id"] for _ in range(5)}
    assert len(ids) == 5
