"""Tests for the smoke endpoint GET /api/v1/station.

Verifies the placeholder endpoint returns the expected shape
and that the full middleware chain works end-to-end.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_smoke_endpoint_returns_200(client: TestClient) -> None:
    """/api/v1/station returns 200."""
    response = client.get("/api/v1/station")
    assert response.status_code == 200


def test_smoke_endpoint_returns_expected_shape(client: TestClient) -> None:
    """Response matches StationResponse envelope from openapi-v1.yaml.

    openapi-v1.yaml StationResponse (lines 1502-1508):
      required: [data, units, generatedAt]

    openapi-v1.yaml StationMetadata (lines 1090-1117):
      required: [stationId, name, latitude, longitude, altitude, timezone, unitSystem]

    openapi-v1.yaml UnitsBlock (lines 794-803):
      additionalProperties: { type: string }  — flat field→unit-string map.
    """
    response = client.get("/api/v1/station")
    body = response.json()

    # Envelope required fields.
    assert "data" in body
    assert "units" in body
    assert "generatedAt" in body

    # StationMetadata required fields inside data.
    data = body["data"]
    assert "stationId" in data
    assert "name" in data
    assert "latitude" in data
    assert "longitude" in data
    # altitude must be a number (not a nested object) per spec line 1101.
    assert isinstance(data["altitude"], (int, float))
    assert "timezone" in data
    # unitSystem must be one of the enum values per spec line 1113.
    assert data["unitSystem"] in ("US", "METRIC", "METRICWX")

    # UnitsBlock must be a flat dict of string values.
    units = body["units"]
    assert isinstance(units, dict)
    assert all(isinstance(v, str) for v in units.values())

    # Explicit placeholder marker in data.
    assert data.get("_placeholder") is True


def test_smoke_endpoint_has_request_id_header(client: TestClient) -> None:
    """Response carries an X-Request-ID header (from RequestIdMiddleware)."""
    response = client.get("/api/v1/station")
    assert "x-request-id" in response.headers


def test_smoke_endpoint_has_security_headers(client: TestClient) -> None:
    """Response carries the required security headers."""
    response = client.get("/api/v1/station")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("referrer-policy") == "no-referrer"
