"""Tests for OpenAPI doc endpoint availability per ADR-018.

Verifies:
  - /api/v1/docs returns 200.
  - /api/v1/openapi.json returns 200 and is valid JSON.
  - /api/v1/redoc returns 200.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_swagger_ui_available(client: TestClient) -> None:
    """/api/v1/docs returns 200 (Swagger UI)."""
    response = client.get("/api/v1/docs")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_openapi_json_available(client: TestClient) -> None:
    """/api/v1/openapi.json returns 200 with valid JSON."""
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    # Must be parseable JSON.
    doc = json.loads(response.text)
    assert "openapi" in doc
    assert "paths" in doc


def test_openapi_json_contains_station_route(client: TestClient) -> None:
    """The OpenAPI spec includes the /api/v1/station smoke route."""
    response = client.get("/api/v1/openapi.json")
    doc = json.loads(response.text)
    paths = doc.get("paths", {})
    assert "/api/v1/station" in paths


def test_redoc_available(client: TestClient) -> None:
    """/api/v1/redoc returns 200."""
    response = client.get("/api/v1/redoc")
    assert response.status_code == 200
