"""Fixtures for API endpoint benchmarks (ADR-033).

Benchmarks in this package are marked with @pytest.mark.benchmark and are
excluded from the default pytest run.  They are designed to run on weather-dev
with a real database to capture meaningful latency numbers.

The fixtures here mirror the pattern in tests/conftest.py:
  - test_app  — FastAPI app with test-friendly settings.
  - benchmark_client  — TestClient built from test_app, ready for benchmark().

The top-level autouse fixture _wire_minimal_services (tests/conftest.py) runs
for every test including benchmarks, so station metadata, units, and an
in-memory SQLite DB are already wired before each benchmark function executes.
On weather-dev, integration_client wiring replaces the SQLite stub; the
benchmark fixtures are intentionally thin so callers can swap in real DB wiring
without changing this file.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from weewx_clearskies_api.config.settings import (
    ApiSettings,
    DatabaseSettings,
    HealthSettings,
    LoggingSettings,
    RateLimitSettings,
    Settings,
)


def _make_benchmark_settings() -> Settings:
    """Return test-friendly Settings for benchmark fixtures.

    Identical to the test defaults in tests/conftest.py — loopback bind,
    60 rpm rate limit, 1 MiB body limit.  No DB URL is set so that the
    in-memory SQLite wired by the autouse fixture (or by the caller's own
    wire_engine call on weather-dev) is used.
    """
    return Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
    )


@pytest.fixture()
def test_app() -> FastAPI:
    """Full application stack for benchmark tests.

    Uses the same create_app() entry-point as the main test suite so that
    all middleware (rate-limit, request-ID, security headers, CORS) is
    included in the measured round-trip.
    """
    from weewx_clearskies_api.app import create_app

    return create_app(_make_benchmark_settings())


@pytest.fixture()
def benchmark_client(test_app: FastAPI) -> TestClient:
    """TestClient for benchmark use.

    raise_server_exceptions=False mirrors the main suite's ``client``
    fixture — we want to measure response time even when the handler raises,
    rather than have pytest-benchmark swallow the exception and report a
    misleading zero-latency result.
    """
    return TestClient(test_app, raise_server_exceptions=False)
