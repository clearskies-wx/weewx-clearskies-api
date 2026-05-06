"""Shared fixtures for the weewx-clearskies-api test suite.

All tests use TestClient only — no live uvicorn processes per task brief.
"""

from __future__ import annotations

from typing import Any

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


# ---------------------------------------------------------------------------
# Settings fixture helpers
# ---------------------------------------------------------------------------


def _make_settings(
    api_overrides: dict[str, Any] | None = None,
    health_overrides: dict[str, Any] | None = None,
    log_overrides: dict[str, Any] | None = None,
    rl_overrides: dict[str, Any] | None = None,
    db_overrides: dict[str, Any] | None = None,
) -> Settings:
    """Build a Settings instance with test-friendly defaults."""
    return Settings(
        api=ApiSettings({**(api_overrides or {})}),
        health=HealthSettings({**(health_overrides or {})}),
        logging_settings=LoggingSettings({**(log_overrides or {})}),
        ratelimit=RateLimitSettings({**(rl_overrides or {})}),
        database=DatabaseSettings({**(db_overrides or {})}),
    )


@pytest.fixture()
def default_settings() -> Settings:
    """Settings with all defaults — loopback bind, 60 rpm, 1 MiB body limit."""
    return _make_settings()


@pytest.fixture()
def test_app(default_settings: Settings) -> FastAPI:
    """Full application stack for integration tests."""
    from weewx_clearskies_api.app import create_app

    return create_app(default_settings)


@pytest.fixture()
def client(test_app: FastAPI) -> TestClient:
    """TestClient for the full application stack."""
    return TestClient(test_app, raise_server_exceptions=False)
