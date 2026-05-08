"""Integration tests for the forecast provider domain (3b round 2).

All tests carry @pytest.mark.integration and run against the docker-compose
dev/test stack (MariaDB or SQLite backend per BACKEND env var).

Same integration suite runs twice per ADR-012 (once MariaDB, once SQLite)
to catch dialect drift. The forecast endpoint itself has no DB dependency, but
running in the real stack confirms the endpoint is DB-stack-agnostic and
wires correctly alongside the DB-backed endpoints.

Redis-backed integration tests carry both @pytest.mark.integration and
@pytest.mark.redis and are skipped unless `pytest -m "integration and redis"`
is used.

Endpoints covered:
  - GET /api/v1/forecast  no-provider → 200 source="none"
  - GET /api/v1/forecast  openmeteo configured + respx-mocked → 200 source="openmeteo"
  - GET /api/v1/capabilities  openmeteo+nws configured → both providers in list
  - Startup: [forecast] provider = openmeteo → _wire_providers_from_config succeeds
  - Startup: [forecast] provider = unknown_provider → ForecastSettings.validate() raises
  - Startup: [forecast] provider = nws → KeyError at dispatch (not yet wired)
  - Redis integration (optional, redis mark): /forecast end-to-end with real Redis

ADR references: ADR-007, ADR-012, ADR-016, ADR-017, ADR-018, ADR-038.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Generator

import pytest
import respx
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Backend configuration
# ---------------------------------------------------------------------------

_BACKEND = os.environ.get("BACKEND", "mariadb").lower()
_MARIADB_HOST_PORT = os.environ.get("MARIADB_HOST_PORT", "3307")
_MARIADB_DB = os.environ.get("MARIADB_DATABASE", "weewx")
_MARIADB_RO_PASSWORD = os.environ.get("MARIADB_RO_PASSWORD", "clearskies_ro_test")
_SQLITE_SDB_PATH = os.environ.get(
    "SQLITE_SDB_PATH",
    os.path.join(os.environ.get("SQLITE_DATA_PATH", "/tmp"), "weewx.sdb"),
)
_REDIS_URL = os.environ.get("CLEARSKIES_CACHE_URL", "redis://localhost:6379/0")

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "openmeteo"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/openmeteo/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


def _require_mariadb_password() -> None:
    if not _MARIADB_RO_PASSWORD:
        pytest.skip("MARIADB_RO_PASSWORD not set; start dev stack and set env var")


def _require_sqlite_file() -> None:
    try:
        exists = Path(_SQLITE_SDB_PATH).exists()
    except PermissionError:
        pytest.skip(f"Cannot access {_SQLITE_SDB_PATH} (PermissionError)")
    if not exists:
        pytest.skip(f"SQLite file not found: {_SQLITE_SDB_PATH}")


def _require_redis() -> None:
    """Skip if REDIS_URL is not reachable."""
    try:
        import redis as redis_lib  # noqa: PLC0415
        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip(f"Redis not reachable at {_REDIS_URL}; start redis compose profile")


# ---------------------------------------------------------------------------
# Engine fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mariadb_ro_engine() -> Generator[Engine, None, None]:
    """Module-scoped read-only MariaDB engine (clearskies_ro user)."""
    _require_mariadb_password()
    engine = create_engine(
        f"mysql+pymysql://clearskies_ro:{_MARIADB_RO_PASSWORD}"
        f"@127.0.0.1:{_MARIADB_HOST_PORT}/{_MARIADB_DB}?charset=utf8mb4",
        future=True,
        pool_pre_ping=True,
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def sqlite_ro_engine() -> Generator[Engine, None, None]:
    """Module-scoped read-only SQLite engine."""
    _require_sqlite_file()
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    engine = create_engine(
        f"sqlite+pysqlite:///file:///{_SQLITE_SDB_PATH}?mode=ro&uri=true",
        poolclass=NullPool,
        future=True,
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def db_engine(
    mariadb_ro_engine: Engine, sqlite_ro_engine: Engine
) -> Generator[Engine, None, None]:
    """Yield the appropriate engine based on BACKEND env var."""
    if _BACKEND == "sqlite":
        yield sqlite_ro_engine
    else:
        yield mariadb_ro_engine


# ---------------------------------------------------------------------------
# Shared wiring helper
# ---------------------------------------------------------------------------


def _wire_integration_stack(
    engine: Engine,
    provider: str | None = None,
    alerts_provider: str | None = None,
    also_wire_forecast: bool = True,
) -> tuple[Any, Any]:
    """Wire the full integration stack for a test app.

    Returns (settings, app) with DB, station, units, cache, providers all wired.
    Handles both MariaDB and SQLite backends identically.
    """
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        AlertsSettings,
        ApiSettings,
        DatabaseSettings,
        ForecastSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP, ColumnInfo, ColumnRegistry  # noqa: PLC0415
    from weewx_clearskies_api.db.registry import wire_registry  # noqa: PLC0415
    from weewx_clearskies_api.db.session import wire_engine  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        ProviderCapability,
        reset_provider_registry_for_tests,
        wire_providers,
    )
    from weewx_clearskies_api.services import station as station_mod  # noqa: PLC0415
    from weewx_clearskies_api.services import units as units_mod  # noqa: PLC0415
    from weewx_clearskies_api.services.station import StationInfo, reset_cache  # noqa: PLC0415
    from weewx_clearskies_api.services.units import (  # noqa: PLC0415
        _GROUP_MEMBERS,
        _SYSTEM_PRESETS,
        reset_cache as reset_units_cache,
    )
    from weewx_clearskies_api.providers.forecast.openmeteo import _reset_http_client_for_tests  # noqa: PLC0415
    import weewx_clearskies_api.providers.forecast.openmeteo as _om  # noqa: PLC0415

    # Reset state
    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    # Clear the rate limiter deque so consecutive tests don't trip each other.
    _om._rate_limiter._calls.clear()

    # Wire DB
    wire_engine(engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)

    # Wire station
    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="integration-test-station",
        name="Integration Test Station",
        latitude=42.375,
        longitude=-72.519,
        altitude=100.0,
        timezone="America/New_York",
        timezone_offset_minutes=-240,
        unit_system="US",
        hardware=None,
    )

    # Wire units
    reset_units_cache()
    system_map = _SYSTEM_PRESETS["US"]
    block: dict[str, str] = {}
    for group, unit in system_map.items():
        for field in _GROUP_MEMBERS.get(group, []):
            block[field] = unit
    units_mod._cached_units_block = block
    units_mod._cached_target_unit = "US"

    # Wire cache
    wire_cache_from_env()

    # Build capability list
    capabilities: list[ProviderCapability] = []

    if alerts_provider == "nws":
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415
        capabilities.append(nws.CAPABILITY)

    if also_wire_forecast and provider == "openmeteo":
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415
        capabilities.append(openmeteo.CAPABILITY)

    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        alerts=AlertsSettings({"provider": alerts_provider} if alerts_provider else {}),
        forecast=ForecastSettings({"provider": provider} if provider else {}),
    )
    app = create_app(settings)
    return settings, app


# ---------------------------------------------------------------------------
# Integration: /forecast with no provider configured
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_app_no_provider(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with no forecast provider configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.forecast.openmeteo import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(db_engine, provider=None)
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_no_provider(
    integration_app_no_provider: FastAPI,
) -> TestClient:
    return TestClient(integration_app_no_provider, raise_server_exceptions=False)


@pytest.fixture
def integration_app_openmeteo(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with Open-Meteo forecast provider configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.forecast.openmeteo import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(db_engine, provider="openmeteo")
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_openmeteo(
    integration_app_openmeteo: FastAPI,
) -> TestClient:
    return TestClient(integration_app_openmeteo, raise_server_exceptions=False)


@pytest.fixture
def integration_app_both_providers(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with both NWS alerts and Open-Meteo forecast configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.forecast.openmeteo import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(
        db_engine,
        provider="openmeteo",
        alerts_provider="nws",
    )
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_both_providers(
    integration_app_both_providers: FastAPI,
) -> TestClient:
    return TestClient(integration_app_both_providers, raise_server_exceptions=False)


# ===========================================================================
# Integration test: /forecast no provider configured
# ===========================================================================


class TestIntegrationForecastNoProvider:
    """/forecast with no provider → 200 source='none' (both DB backends)."""

    def test_no_provider_returns_200_with_empty_bundle(
        self, integration_client_no_provider: TestClient
    ) -> None:
        response = integration_client_no_provider.get("/api/v1/forecast")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["source"] == "none"
        assert body["data"]["hourly"] == []
        assert body["data"]["daily"] == []
        assert body["data"]["discussion"] is None

    def test_no_provider_envelope_source_is_none(
        self, integration_client_no_provider: TestClient
    ) -> None:
        response = integration_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert body["source"] == "none"

    def test_no_provider_units_block_present(
        self, integration_client_no_provider: TestClient
    ) -> None:
        response = integration_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert "units" in body
        assert isinstance(body["units"], dict)

    def test_no_provider_generated_at_is_set(
        self, integration_client_no_provider: TestClient
    ) -> None:
        response = integration_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["generatedAt"].endswith("Z")
        assert body["generatedAt"].endswith("Z")


# ===========================================================================
# Integration test: /forecast with Open-Meteo configured + respx-mocked
# ===========================================================================


class TestIntegrationForecastOpenMeteo:
    """/forecast with Open-Meteo + respx-mocked → 200 source='openmeteo'."""

    def test_openmeteo_returns_200_with_bundle(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_openmeteo.get("/api/v1/forecast")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["source"] == "openmeteo"

    def test_openmeteo_default_returns_48_hourly_and_7_daily(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert len(body["data"]["hourly"]) == 48
        assert len(body["data"]["daily"]) == 7

    def test_openmeteo_slice_params_respected(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_openmeteo.get(
                "/api/v1/forecast", params={"hours": 12, "days": 2}
            )
        body = response.json()
        assert len(body["data"]["hourly"]) == 12
        assert len(body["data"]["daily"]) == 2

    def test_openmeteo_discussion_is_null(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["discussion"] is None

    def test_openmeteo_units_block_is_dict(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert isinstance(body["units"], dict)


# ===========================================================================
# Integration test: /capabilities with both alerts+forecast configured
# ===========================================================================


class TestIntegrationCapabilitiesBothProviders:
    """/capabilities with NWS alerts + Open-Meteo forecast → both in providers list."""

    def test_capabilities_includes_both_nws_and_openmeteo(
        self, integration_client_both_providers: TestClient
    ) -> None:
        response = integration_client_both_providers.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        providers = body["data"]["providers"]
        provider_ids = {p["providerId"] for p in providers}
        assert "nws" in provider_ids, f"Expected NWS in providers; got {provider_ids}"
        assert "openmeteo" in provider_ids, f"Expected openmeteo in providers; got {provider_ids}"

    def test_capabilities_canonical_fields_includes_union_of_both(
        self, integration_client_both_providers: TestClient
    ) -> None:
        """canonicalFieldsAvailable is the union of stock + NWS + Open-Meteo fields."""
        response = integration_client_both_providers.get("/api/v1/capabilities")
        body = response.json()
        available = set(body["data"]["canonicalFieldsAvailable"])

        # Alerts-specific fields (from NWS CAPABILITY)
        for field in ("id", "headline", "severity", "event"):
            assert field in available, (
                f"NWS field {field!r} should be in canonicalFieldsAvailable"
            )

        # Forecast-specific fields (from Open-Meteo CAPABILITY)
        for field in ("validTime", "outTemp", "precipProbability", "weatherCode"):
            assert field in available, (
                f"Forecast field {field!r} should be in canonicalFieldsAvailable"
            )


# ===========================================================================
# Integration test: Startup wiring
# ===========================================================================


class TestIntegrationStartupWiring:
    """Startup _wire_providers_from_config correctly handles forecast settings."""

    def test_startup_with_openmeteo_provider_succeeds(
        self, db_engine: Engine
    ) -> None:
        """[forecast] provider = openmeteo → wiring succeeds without error."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.openmeteo import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        wire_cache_from_env()

        # Simulate what _wire_providers_from_config does for forecast
        wire_providers([openmeteo.CAPABILITY])

        registry = get_provider_registry()
        forecast_providers = [p for p in registry if p.domain == "forecast"]
        assert len(forecast_providers) == 1
        assert forecast_providers[0].provider_id == "openmeteo"

        # Cleanup
        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

    def test_startup_with_unknown_provider_id_raises_at_validate(
        self, db_engine: Engine
    ) -> None:
        """[forecast] provider = unknown_provider → ForecastSettings.validate() raises ValueError."""
        from weewx_clearskies_api.config.settings import ForecastSettings  # noqa: PLC0415
        settings = ForecastSettings({"provider": "unknown_provider_xyz"})
        with pytest.raises(ValueError) as exc_info:
            settings.validate()
        assert "unknown_provider_xyz" in str(exc_info.value)

    def test_startup_with_nws_as_forecast_provider_raises_key_error(
        self, db_engine: Engine
    ) -> None:
        """[forecast] provider = nws → KeyError at dispatch lookup (nws not in forecast dispatch).

        Per brief §Failure modes: ForecastSettings accepts all ADR-007 day-1 providers,
        but dispatch lookup raises KeyError for providers not yet wired.
        """
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415
        with pytest.raises(KeyError):
            get_provider_module(domain="forecast", provider_id="nws")


# ===========================================================================
# Integration test: Redis backend (optional, redis mark)
# ===========================================================================


@pytest.mark.redis
class TestIntegrationForecastRedisBackend:
    """Real Redis from the docker-compose redis profile (optional tier)."""

    def test_forecast_endpoint_with_real_redis_returns_200(
        self, db_engine: Engine
    ) -> None:
        """End-to-end /forecast with real Redis cache returns 200."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.openmeteo import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

        # Wire real Redis
        redis_cache = RedisCache(url=_REDIS_URL)
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        wire_providers([openmeteo.CAPABILITY])

        _, app = _wire_integration_stack(db_engine, provider="openmeteo")

        client = TestClient(app, raise_server_exceptions=False)
        fixture = _load_fixture("forecast.json")

        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = client.get("/api/v1/forecast")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["source"] == "openmeteo"

        # Cleanup
        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
