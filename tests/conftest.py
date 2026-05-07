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


def _wire_test_station() -> None:
    """Wire station metadata with test-friendly defaults.

    Called before creating the test app so that /station and other endpoints
    that call get_station_info() don't raise RuntimeError in unit tests.
    Integration tests that need real DB data handle their own wiring.
    """
    from weewx_clearskies_api.services import station as _station_mod
    from weewx_clearskies_api.services.station import StationInfo, reset_cache

    reset_cache()
    _station_mod._cached_station = StationInfo(
        station_id="test-station",
        name="Test Weather Station",
        latitude=42.375,
        longitude=-72.519,
        altitude=100.0,
        timezone="America/New_York",
        timezone_offset_minutes=-240,
        unit_system="US",
        hardware=None,
    )


def _wire_test_units() -> None:
    """Wire units block with US defaults for tests that don't need weewx.conf."""
    from weewx_clearskies_api.services import units as _units_mod
    from weewx_clearskies_api.services.units import (
        _GROUP_MEMBERS,
        _SYSTEM_PRESETS,
        reset_cache,
    )

    reset_cache()
    system_map = _SYSTEM_PRESETS["US"]
    block: dict[str, str] = {}
    for group, unit in system_map.items():
        for field in _GROUP_MEMBERS.get(group, []):
            block[field] = unit
    _units_mod._cached_units_block = block
    _units_mod._cached_target_unit = "US"


def _wire_test_db() -> None:
    """Wire an in-memory SQLite engine with a minimal archive table.

    Required so that /station can run its MIN/MAX dateTime query without a
    live weewx database.  The table uses the real production schema column set
    so NOT NULL constraints surface correctly per the 'real schemas' rule.
    """
    from sqlalchemy import Column, Integer, Float, MetaData, Table, create_engine

    from weewx_clearskies_api.db.registry import wire_registry
    from weewx_clearskies_api.db.reflection import ColumnRegistry, ColumnInfo, STOCK_COLUMN_MAP
    from weewx_clearskies_api.db.session import wire_engine

    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    # Minimal archive table: dateTime (required), usUnits, interval are NOT NULL
    # in the real weewx schema.  Add them so the query runs without error.
    Table(
        "archive",
        meta,
        Column("dateTime", Integer, primary_key=True),
        Column("usUnits", Integer, nullable=False),
        Column("interval", Integer, nullable=False),
        Column("outTemp", Float, nullable=True),
    )
    meta.create_all(engine)
    wire_engine(engine)

    # Wire a minimal ColumnRegistry (empty stock = no columns in archive for unit tests).
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
        if col in ("dateTime", "usUnits", "interval", "outTemp")
    }
    wire_registry(registry)


@pytest.fixture()
def default_settings() -> Settings:
    """Settings with all defaults — loopback bind, 60 rpm, 1 MiB body limit."""
    return _make_settings()


@pytest.fixture()
def test_app(default_settings: Settings) -> FastAPI:
    """Full application stack for integration tests.

    Wires station metadata, units block, and an in-memory SQLite DB so that
    /station and other service-dependent endpoints work without a live
    weewx.conf or DB in unit tests.
    """
    from weewx_clearskies_api.app import create_app

    _wire_test_units()
    _wire_test_station()
    _wire_test_db()
    return create_app(default_settings)


@pytest.fixture()
def client(test_app: FastAPI) -> TestClient:
    """TestClient for the full application stack."""
    return TestClient(test_app, raise_server_exceptions=False)
