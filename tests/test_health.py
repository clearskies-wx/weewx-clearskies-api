"""Tests for health endpoints per ADR-030.

Verifies:
  - /health/live returns 200 on the health app.
  - /health/ready returns 200 with correct shape (no probes registered).
  - /health/ready returns 503 when a probe reports unhealthy.
  - Health endpoints are NOT reachable on the public app.
  - Probe registration interface works.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from weewx_clearskies_api.health import (
    ProbeResult,
    _readiness_probes,  # noqa: PLC2701
    create_health_app,
    register_readiness_probe,
)


@pytest.fixture(autouse=True)
def _clear_probes() -> None:
    """Reset probe registry between tests."""
    _readiness_probes.clear()
    yield  # type: ignore[misc]  # noqa: PT022 — cleanup needed
    _readiness_probes.clear()


def test_liveness_returns_200() -> None:
    """/health/live always returns 200."""
    client = TestClient(create_health_app(), raise_server_exceptions=False)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_no_probes_returns_200() -> None:
    """/health/ready returns 200 with ok when no probes are registered."""
    client = TestClient(create_health_app(), raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "checks" in body


def test_readiness_ok_probe_returns_200() -> None:
    """A healthy probe yields 200 ok."""
    register_readiness_probe(lambda: ProbeResult(name="test_dep", status="ok"))
    client = TestClient(create_health_app(), raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["test_dep"]["status"] == "ok"


def test_readiness_warning_probe_returns_200_degraded() -> None:
    """A warning probe yields 200 with status degraded."""
    register_readiness_probe(
        lambda: ProbeResult(name="dep", status="warning", messages=["slow"])
    )
    client = TestClient(create_health_app(), raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"


def test_readiness_unhealthy_probe_returns_503() -> None:
    """An unhealthy probe yields 503."""
    register_readiness_probe(
        lambda: ProbeResult(name="db", status="unhealthy", messages=["connection refused"])
    )
    client = TestClient(create_health_app(), raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 503


def test_health_endpoints_not_on_public_app() -> None:
    """Health routes are NOT present on the public API app."""
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.app import create_app

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
    )
    public_client = TestClient(create_app(settings), raise_server_exceptions=False)

    # These should 404 on the public app.
    assert public_client.get("/health/live").status_code == 404
    assert public_client.get("/health/ready").status_code == 404


def test_probe_exception_handled_gracefully() -> None:
    """A probe that raises an exception doesn't crash /health/ready."""

    def _bad_probe() -> ProbeResult:
        raise ValueError("unexpected failure")

    register_readiness_probe(_bad_probe)
    client = TestClient(create_health_app(), raise_server_exceptions=False)
    # Should return 503 (unhealthy) but not 500.
    response = client.get("/health/ready")
    assert response.status_code == 503
