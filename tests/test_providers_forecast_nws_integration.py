"""Integration tests for the NWS forecast provider (3b round 3).

All tests carry @pytest.mark.integration and run against the docker-compose
dev/test stack (MariaDB or SQLite backend per BACKEND env var).

Same integration suite runs twice per ADR-012 (once MariaDB, once SQLite)
to catch dialect drift. The forecast endpoint itself has no DB dependency, but
running in the real stack confirms the endpoint is DB-stack-agnostic and
wires correctly alongside the DB-backed endpoints.

Redis-backed integration tests carry both @pytest.mark.integration and
@pytest.mark.redis and are skipped unless `pytest -m "integration and redis"`
is used.  Per the brief: the Redis tier MUST PASS, not skip.  If Redis is
not available on weather-dev, this is a brief-gate failure that must be
surfaced to the lead before closeout.

Endpoints covered:
  - GET /api/v1/forecast  no-provider → 200 source="none" (regression from 3b-2)
  - GET /api/v1/forecast  nws configured + respx-mocked → 200 source="nws"
  - GET /api/v1/forecast  nws + AFD soft-failure → 200 discussion=None
  - GET /api/v1/capabilities  nws forecast + nws alerts both configured → both providers
  - Startup: [forecast] provider = nws → wiring succeeds cleanly
  - Startup: [forecast] provider = nws + no nws_user_agent_contact → starts OK, WARN on /forecast
  - Startup: [forecast] provider = aeris → now in dispatch (3b-4 wired; test updated)
  - Redis backend: /forecast end-to-end against real Redis

ADR references: ADR-006, ADR-007, ADR-012, ADR-017, ADR-018, ADR-019, ADR-038.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Generator

import httpx
import pytest
import respx
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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "nws"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/nws/."""
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
    """Skip if REDIS_URL is not reachable.

    Per brief §Brief-gate honesty: if Redis is not reachable, tests that depend
    on it will skip.  The test-author must SendMessage the lead if the Redis tier
    is not passing before submitting the closeout.
    """
    try:
        import redis as redis_lib  # noqa: PLC0415
        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.ping()
    except (ImportError, ConnectionError, OSError) as exc:
        pytest.skip(
            f"Redis not reachable at {_REDIS_URL} ({type(exc).__name__}); "
            "start redis compose profile"
        )
    except Exception as exc:  # noqa: BLE001 — narrow to redis-py errors below if not caught above
        # redis_lib.exceptions.RedisError is a separate hierarchy from
        # ConnectionError; catch its base class here.  Import-after-use to
        # keep the test skippable when redis-py itself is missing (caught above).
        import redis as _redis_lib  # noqa: PLC0415
        if isinstance(exc, _redis_lib.exceptions.RedisError):
            pytest.skip(
                f"Redis not reachable at {_REDIS_URL} ({type(exc).__name__}); "
                "start redis compose profile"
            )
        raise


# ---------------------------------------------------------------------------
# Engine fixtures (module-scoped — same DB for all integration tests in module)
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
    forecast_provider: str | None = None,
    alerts_provider: str | None = None,
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
    from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415
    import weewx_clearskies_api.providers.forecast.nws as _nws  # noqa: PLC0415

    # Reset state
    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _nws._rate_limiter._calls.clear()

    # Wire DB
    wire_engine(engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)

    # Wire station — Seattle coordinates match the NWS fixtures.
    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="integration-test-station",
        name="Integration Test Station",
        latitude=47.6062,
        longitude=-122.3321,
        altitude=100.0,
        timezone="America/Los_Angeles",
        timezone_offset_minutes=-420,
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
        from weewx_clearskies_api.providers.alerts import nws as alerts_nws  # noqa: PLC0415
        capabilities.append(alerts_nws.CAPABILITY)

    if forecast_provider == "nws":
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        capabilities.append(forecast_nws.CAPABILITY)
    elif forecast_provider == "openmeteo":
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
        forecast=ForecastSettings({"provider": forecast_provider} if forecast_provider else {}),
    )
    app = create_app(settings)
    return settings, app


def _mock_all_nws(mock: Any) -> None:
    """Wire respx mock for all 5 NWS URLs with real fixtures."""
    mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
        return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
    )
    mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
        return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
    )
    mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
        return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
    )
    mock.get("https://api.weather.gov/products").mock(
        return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
    )
    mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
        return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
    )


# ---------------------------------------------------------------------------
# Integration app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_app_no_provider(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with no forecast provider configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(db_engine, forecast_provider=None)
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
def integration_app_nws(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with NWS forecast provider configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(db_engine, forecast_provider="nws")
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_nws(integration_app_nws: FastAPI) -> TestClient:
    return TestClient(integration_app_nws, raise_server_exceptions=False)


@pytest.fixture
def integration_app_both_nws(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with NWS alerts + NWS forecast both configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(
        db_engine, forecast_provider="nws", alerts_provider="nws"
    )
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_both_nws(integration_app_both_nws: FastAPI) -> TestClient:
    return TestClient(integration_app_both_nws, raise_server_exceptions=False)


# ===========================================================================
# Integration test: /forecast no provider — regression from 3b-2
# ===========================================================================


class TestIntegrationForecastNoProviderRegression:
    """/forecast with no provider still returns 200 source='none' (regression check)."""

    def test_no_provider_returns_200_empty_bundle(
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

    def test_no_provider_generated_at_ends_with_z(
        self, integration_client_no_provider: TestClient
    ) -> None:
        response = integration_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["generatedAt"].endswith("Z")


# ===========================================================================
# Integration test: /forecast with NWS configured + respx-mocked
# ===========================================================================


class TestIntegrationForecastNws:
    """/forecast with NWS configured + respx-mocked → 200 source='nws'."""

    def test_nws_returns_200_with_bundle(
        self, integration_client_nws: TestClient
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = integration_client_nws.get("/api/v1/forecast")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["source"] == "nws"

    def test_nws_default_returns_48_hourly_and_7_daily(
        self, integration_client_nws: TestClient
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = integration_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert len(body["data"]["hourly"]) == 48
        assert len(body["data"]["daily"]) == 7

    def test_nws_slice_params_respected(
        self, integration_client_nws: TestClient
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = integration_client_nws.get(
                "/api/v1/forecast", params={"hours": 12, "days": 2}
            )
        body = response.json()
        assert len(body["data"]["hourly"]) == 12
        assert len(body["data"]["daily"]) == 2

    def test_nws_discussion_is_populated(
        self, integration_client_nws: TestClient
    ) -> None:
        """Full AFD available → discussion is not null."""
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = integration_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["discussion"] is not None
        # Per canonical-data-model §4.1.4 NWS column: senderName is the
        # "NWS [Location]" composite parsed from the AFD body's
        # "National Weather Service [Location]" line.
        assert body["data"]["discussion"]["senderName"] == "NWS Seattle WA"

    def test_nws_units_block_is_dict(
        self, integration_client_nws: TestClient
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = integration_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert isinstance(body["units"], dict)

    def test_nws_generated_at_ends_with_z(
        self, integration_client_nws: TestClient
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = integration_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["generatedAt"].endswith("Z")

    def test_nws_hourly_first_valid_time_is_utc(
        self, integration_client_nws: TestClient
    ) -> None:
        """First hourly point validTime ends with Z (UTC)."""
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = integration_client_nws.get(
                "/api/v1/forecast", params={"hours": 1, "days": 1}
            )
        body = response.json()
        assert body["data"]["hourly"][0]["validTime"].endswith("Z")


# ===========================================================================
# Integration test: /forecast NWS + AFD soft-failure
# ===========================================================================


class TestIntegrationForecastNwsAfdSoftFailure:
    """/forecast with NWS + AFD soft-failure → 200 bundle, discussion=None."""

    def test_afd_soft_failure_returns_200_with_null_discussion(
        self, integration_client_nws: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.forecast.nws"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
                )
                mock.get("https://api.weather.gov/products").mock(
                    return_value=httpx.Response(200, json=_load_fixture("products_afd_list_empty.json"))
                )
                response = integration_client_nws.get("/api/v1/forecast")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["source"] == "nws"
        assert body["data"]["discussion"] is None

    def test_afd_soft_failure_hourly_still_populated(
        self, integration_client_nws: TestClient
    ) -> None:
        """AFD failure does not affect hourly/daily forecast data."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list_empty.json"))
            )
            response = integration_client_nws.get("/api/v1/forecast")

        body = response.json()
        assert len(body["data"]["hourly"]) == 48
        assert len(body["data"]["daily"]) == 7


# ===========================================================================
# Integration test: /capabilities with both NWS alerts + NWS forecast
# ===========================================================================


class TestIntegrationCapabilitiesBothNws:
    """/capabilities with both NWS alerts + NWS forecast → both providers in list."""

    def test_capabilities_includes_two_nws_entries_for_different_domains(
        self, integration_client_both_nws: TestClient
    ) -> None:
        """Two NWS entries: one for alerts, one for forecast (different domains)."""
        response = integration_client_both_nws.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        providers = body["data"]["providers"]
        provider_ids = {p["providerId"] for p in providers}
        assert "nws" in provider_ids, f"Expected NWS in providers; got {provider_ids}"
        # Both alerts and forecast nws entries should be present.
        domains = {p["domain"] for p in providers if p["providerId"] == "nws"}
        assert "alerts" in domains, f"Expected alerts domain for NWS; got domains={domains}"
        assert "forecast" in domains, f"Expected forecast domain for NWS; got domains={domains}"

    def test_capabilities_canonical_fields_includes_discussion_fields(
        self, integration_client_both_nws: TestClient
    ) -> None:
        """canonicalFieldsAvailable includes ForecastDiscussion fields from NWS forecast."""
        response = integration_client_both_nws.get("/api/v1/capabilities")
        body = response.json()
        available = set(body["data"]["canonicalFieldsAvailable"])
        for field in ("headline", "body", "issuedAt", "senderName"):
            assert field in available, (
                f"Discussion field {field!r} missing from canonicalFieldsAvailable"
            )

    def test_capabilities_canonical_fields_includes_alerts_fields(
        self, integration_client_both_nws: TestClient
    ) -> None:
        """canonicalFieldsAvailable includes NWS alerts fields."""
        response = integration_client_both_nws.get("/api/v1/capabilities")
        body = response.json()
        available = set(body["data"]["canonicalFieldsAvailable"])
        # These are from the NWS alerts CAPABILITY (added in 3b-1).
        for field in ("id", "severity", "event"):
            assert field in available, (
                f"Alert field {field!r} missing from canonicalFieldsAvailable"
            )

    def test_capabilities_canonical_fields_includes_hourly_forecast_fields(
        self, integration_client_both_nws: TestClient
    ) -> None:
        """canonicalFieldsAvailable includes NWS forecast hourly fields."""
        response = integration_client_both_nws.get("/api/v1/capabilities")
        body = response.json()
        available = set(body["data"]["canonicalFieldsAvailable"])
        for field in ("validTime", "outTemp", "windSpeed", "precipType", "weatherCode"):
            assert field in available, (
                f"Hourly forecast field {field!r} missing from canonicalFieldsAvailable"
            )


# ===========================================================================
# Integration test: Startup wiring
# ===========================================================================


class TestIntegrationStartupWiring:
    """Startup _wire_providers_from_config correctly handles NWS forecast settings."""

    def test_startup_with_nws_as_forecast_provider_succeeds(
        self, db_engine: Engine
    ) -> None:
        """[forecast] provider = nws → wiring succeeds cleanly without error."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        wire_cache_from_env()

        # Simulate what _wire_providers_from_config does for forecast nws.
        wire_providers([forecast_nws.CAPABILITY])

        registry = get_provider_registry()
        nws_entries = [p for p in registry if p.provider_id == "nws" and p.domain == "forecast"]
        assert len(nws_entries) == 1

        # Cleanup.
        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

    def test_startup_with_nws_missing_ua_contact_emits_warn_on_first_request(
        self, db_engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """[forecast] provider = nws + no nws_user_agent_contact → starts OK; WARN on /forecast."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        _, app = _wire_integration_stack(db_engine, forecast_provider="nws")
        client = TestClient(app, raise_server_exceptions=False)

        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.forecast.nws"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
                )
                mock.get("https://api.weather.gov/products").mock(
                    return_value=httpx.Response(200, json=_load_fixture("products_afd_list_empty.json"))
                )
                # No nws_user_agent_contact wired → should trigger WARN.
                response = client.get("/api/v1/forecast")

        # Process starts cleanly (200 returned).
        assert response.status_code == 200

        warn_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        # At least one WARN about missing UA contact.
        assert any(
            "contact" in m.lower() or "user_agent" in m.lower() or "nws_user_agent" in m.lower()
            for m in warn_messages
        ), f"Expected WARN about missing NWS UA contact; got: {warn_messages}"

        # Cleanup.
        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

    def test_aeris_now_in_dispatch_table(self, db_engine: Engine) -> None:
        """Verify ('forecast', 'aeris') is now in the dispatch table (3b-4 addition).

        This test replaces test_startup_with_aeris_as_forecast_provider_raises_key_error
        from 3b-3, which expected KeyError because aeris was not yet wired.
        3b-4 scope item 2 adds the ('forecast', 'aeris') row to dispatch.py;
        the old KeyError expectation is now stale and was updated here.
        """
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415
        module = get_provider_module(domain="forecast", provider_id="aeris")
        assert module is not None
        assert hasattr(module, "CAPABILITY")
        assert hasattr(module, "fetch")

    def test_startup_with_unknown_provider_id_raises_at_validate(
        self, db_engine: Engine
    ) -> None:
        """[forecast] provider = unknown_provider → ForecastSettings.validate() raises ValueError."""
        from weewx_clearskies_api.config.settings import ForecastSettings  # noqa: PLC0415
        settings = ForecastSettings({"provider": "unknown_provider_xyz"})
        with pytest.raises(ValueError) as exc_info:
            settings.validate()
        assert "unknown_provider_xyz" in str(exc_info.value)

    def test_nws_now_in_dispatch_table(self, db_engine: Engine) -> None:
        """Verify ('forecast', 'nws') is now in the dispatch table (3b-3 addition)."""
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415
        module = get_provider_module(domain="forecast", provider_id="nws")
        assert module is not None
        assert hasattr(module, "CAPABILITY")
        assert hasattr(module, "fetch")


# ===========================================================================
# Integration test: Redis backend (optional, redis mark)
# ===========================================================================


@pytest.mark.redis
class TestIntegrationForecastNwsRedisBackend:
    """Real Redis from the docker-compose redis profile (optional tier).

    Per brief §Brief-gate honesty: this tier MUST PASS, not skip.
    If Redis is not reachable on weather-dev, surface this via SendMessage
    to the lead BEFORE submitting the closeout.
    """

    def test_forecast_endpoint_with_real_redis_returns_200_source_nws(
        self, db_engine: Engine
    ) -> None:
        """End-to-end /forecast with real Redis cache + NWS mocked → 200 source='nws'."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

        # Wire real Redis and flush to clear any prior-test state.
        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        wire_providers([forecast_nws.CAPABILITY])
        _, app = _wire_integration_stack(db_engine, forecast_provider="nws")

        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            response = client.get("/api/v1/forecast")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["source"] == "nws"

        # Cleanup.
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

    def test_forecast_cache_hit_on_second_request_skips_nws_calls(
        self, db_engine: Engine
    ) -> None:
        """Second /forecast request with warm Redis cache makes no NWS outbound calls."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]  # ensure cold cache for first request
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        wire_providers([forecast_nws.CAPABILITY])
        _, app = _wire_integration_stack(db_engine, forecast_provider="nws")

        client = TestClient(app, raise_server_exceptions=False)

        # First request — fills the cache.
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_nws(mock)
            resp1 = client.get("/api/v1/forecast")
            first_call_count = mock.calls.call_count

        assert resp1.status_code == 200
        assert first_call_count > 0, "First request should have made NWS calls"

        # Second request — should hit the cache; no new outbound calls.
        with respx.mock(assert_all_called=False) as mock:
            # Nothing mocked; if any call fires, respx will raise.
            resp2 = client.get("/api/v1/forecast")
            second_call_count = mock.calls.call_count

        assert resp2.status_code == 200
        assert second_call_count == 0, (
            "Second request should have hit Redis cache; "
            f"got {second_call_count} unexpected NWS calls"
        )

        # Cleanup.
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
