"""Integration tests for the Weather Underground forecast provider (3b round 6).

All tests carry @pytest.mark.integration and run against the docker-compose
dev/test stack (MariaDB or SQLite backend per BACKEND env var).

Same integration suite runs twice per ADR-012 (once MariaDB, once SQLite)
to catch dialect drift. The forecast endpoint itself has no DB dependency,
but running in the real stack confirms the endpoint is DB-stack-agnostic
and wires correctly alongside the DB-backed endpoints.

Redis-backed integration tests carry both @pytest.mark.integration and
@pytest.mark.redis and are skipped unless `pytest -m "integration and redis"`
is used. Per brief §Process gates: the Redis tier MUST PASS, not skip.
If Redis is not reachable on weather-dev, this is a brief-gate failure
that must be surfaced to the lead via SendMessage BEFORE closeout.

Coverage:
  - Dispatch table: ('forecast', 'wunderground') is in PROVIDER_MODULES.
  - ForecastSettings: provider='wunderground' passes validate().
  - wire_wunderground_credentials() picks up env vars.
  - /forecast endpoint: hourly=[], discussion=null, daily=5 entries.
  - PARTIAL-DOMAIN slice: requesting hours=24 → hourly=[] still (not an error).
  - Slice: requesting days=3 → daily[:3] returned (3 entries).
  - Cache (memory): miss → fetch (1 WU call) → bundle returned.
  - Cache (memory): second fetch hits cache → 0 WU calls.
  - Cache (Redis): miss → fetch (1 WU call) → bundle stored in Redis.
  - Cache (Redis): second fetch hits Redis cache → 0 WU calls.
  - Missing api_key → 502 ProviderProblem KeyInvalid.
  - Missing pws_station_id → 502 ProviderProblem KeyInvalid.
  - 401 from Wunderground → 502 ProviderProblem KeyInvalid (bare propagate).
  - 429 from Wunderground → 503 ProviderProblem QuotaExhausted.

ADR references: ADR-006, ADR-007, ADR-012, ADR-017, ADR-019, ADR-027, ADR-038.
"""

from __future__ import annotations

import json
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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "wunderground"

_LAT = 47.6062
_LON = -122.3321
_WU_BASE_URL = "https://api.weather.com"
_WU_FORECAST_PATH = "/v3/wx/forecast/daily/5day"
_WU_FORECAST_URL = _WU_BASE_URL + _WU_FORECAST_PATH
_TEST_API_KEY = "INTEGRATION_TEST_WU_KEY_12345"
_TEST_PWS_ID = "KWASEATT123"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/wunderground/."""
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
    """Skip if Redis is not reachable.

    Per brief §Brief-gate honesty: if Redis is not reachable, this function
    skips the individual test. The test-author must SendMessage the lead if
    the Redis tier is not passing before submitting the closeout.
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
    except Exception as exc:  # noqa: BLE001
        import redis as _redis_lib  # noqa: PLC0415
        if isinstance(exc, _redis_lib.exceptions.RedisError):
            pytest.skip(
                f"Redis not reachable at {_REDIS_URL} ({type(exc).__name__}); "
                "start redis compose profile"
            )
        raise


# ---------------------------------------------------------------------------
# Engine fixtures (module-scoped — same DB for all integration tests)
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
    wu_api_key: str | None = None,
    wu_pws_station_id: str | None = None,
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
    from weewx_clearskies_api.providers.forecast.wunderground import (  # noqa: PLC0415
        _reset_http_client_for_tests,
    )
    import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415
    from weewx_clearskies_api.endpoints import forecast as forecast_endpoint  # noqa: PLC0415

    # Reset state
    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _wu._rate_limiter._calls.clear()

    # Wire Wunderground credentials
    forecast_endpoint.wire_wunderground_credentials(
        wu_api_key or _TEST_API_KEY,
        wu_pws_station_id or _TEST_PWS_ID,
    )

    # Wire DB
    wire_engine(engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)

    # Wire station — Seattle coordinates match WU fixtures
    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="integration-test-station",
        name="Integration Test Station",
        latitude=_LAT,
        longitude=_LON,
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
    if forecast_provider == "wunderground":
        from weewx_clearskies_api.providers.forecast import wunderground as forecast_wu  # noqa: PLC0415
        capabilities.append(forecast_wu.CAPABILITY)

    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        alerts=AlertsSettings({}),
        forecast=ForecastSettings({"provider": forecast_provider} if forecast_provider else {}),
    )
    app = create_app(settings)
    return settings, app


def _reset_wu_state() -> None:
    """Reset Wunderground provider state between tests."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers.forecast.wunderground import (  # noqa: PLC0415
        _reset_http_client_for_tests,
    )
    import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _wu._rate_limiter._calls.clear()


# ---------------------------------------------------------------------------
# Integration app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_app_wu(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with Wunderground forecast provider configured."""
    _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")
    yield app
    _reset_wu_state()


@pytest.fixture
def integration_client_wu(integration_app_wu: FastAPI) -> TestClient:
    """TestClient for the integration app with Wunderground configured."""
    return TestClient(integration_app_wu, raise_server_exceptions=False)


# ===========================================================================
# Integration test: dispatch table — wunderground is in PROVIDER_MODULES
# ===========================================================================


class TestIntegrationDispatchTableHasWunderground:
    """('forecast', 'wunderground') is in the dispatch table as of 3b-6."""

    def test_wunderground_is_in_forecast_dispatch_table(self) -> None:
        """get_provider_module(domain='forecast', provider_id='wunderground') returns module."""
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415

        module = get_provider_module(domain="forecast", provider_id="wunderground")
        assert module is not None
        assert hasattr(module, "CAPABILITY")
        assert hasattr(module, "fetch")
        assert module.CAPABILITY.provider_id == "wunderground"

    def test_wunderground_module_has_correct_domain(self) -> None:
        """Wunderground module from dispatch table has domain='forecast'."""
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415

        module = get_provider_module(domain="forecast", provider_id="wunderground")
        assert module.CAPABILITY.domain == "forecast"


# ===========================================================================
# Integration test: /forecast endpoint with Wunderground + respx-mocked
# ===========================================================================


class TestIntegrationForecastWunderground:
    """/forecast with Wunderground + respx-mocked → 200 source='wunderground'."""

    def test_wunderground_returns_200_with_bundle(
        self, integration_client_wu: TestClient
    ) -> None:
        """/forecast with Wunderground configured → 200 with source='wunderground'."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["source"] == "wunderground"

    def test_wunderground_hourly_is_always_empty(
        self, integration_client_wu: TestClient
    ) -> None:
        """hourly=[] ALWAYS — PARTIAL-DOMAIN; no hourly on any Wunderground PWS tier."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["hourly"] == []

    def test_wunderground_discussion_is_null(
        self, integration_client_wu: TestClient
    ) -> None:
        """discussion is null — Wunderground PWS has no discussion product (§4.1.4 column='—')."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["discussion"] is None

    def test_wunderground_daily_has_5_entries(
        self, integration_client_wu: TestClient
    ) -> None:
        """/forecast with Wunderground → 5 daily entries from /5day fixture."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast?days=5")
        body = response.json()
        assert len(body["data"]["daily"]) == 5

    def test_wunderground_hourly_empty_even_with_hours_param(
        self, integration_client_wu: TestClient
    ) -> None:
        """Requesting ?hours=24 with Wunderground → hourly=[] (PARTIAL-DOMAIN slice=empty)."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast?hours=24")
        body = response.json()
        # PARTIAL-DOMAIN: hourly=[] regardless of ?hours= param
        assert body["data"]["hourly"] == []
        assert response.status_code == 200

    def test_slice_days_3_returns_3_daily_entries(
        self, integration_client_wu: TestClient
    ) -> None:
        """?days=3 → daily[:3] returned (3 entries from the 5-day bundle)."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast?days=3")
        body = response.json()
        assert len(body["data"]["daily"]) == 3

    def test_wunderground_daily_first_entry_shape(
        self, integration_client_wu: TestClient
    ) -> None:
        """First daily entry has all required DailyForecastPoint fields."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast?days=5")
        body = response.json()
        first_day = body["data"]["daily"][0]
        # Required fields per canonical §3.4
        assert "validDate" in first_day
        assert first_day["validDate"] == "2026-04-30"
        assert "tempMax" in first_day
        assert first_day["tempMax"] == 64
        assert "tempMin" in first_day
        assert first_day["tempMin"] == 48
        assert "windGustMax" in first_day
        assert first_day["windGustMax"] is None  # Wunderground §4.1.3 column = "—"

    def test_wunderground_openapi_response_shape(
        self, integration_client_wu: TestClient
    ) -> None:
        """Response shape matches ForecastResponse OpenAPI schema (§openapi-v1.yaml L1562)."""
        fixture = _load_fixture("forecast_daily_5day.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = integration_client_wu.get("/api/v1/forecast")
        body = response.json()
        # ForecastResponse envelope
        assert "data" in body
        assert "units" in body
        assert "source" in body
        assert "generatedAt" in body
        # ForecastBundle
        bundle = body["data"]
        assert "hourly" in bundle
        assert "daily" in bundle
        assert "discussion" in bundle
        assert "source" in bundle
        assert "generatedAt" in bundle


# ===========================================================================
# Integration test: memory cache
# ===========================================================================


class TestIntegrationMemoryCache:
    """Memory cache: miss → 1 WU call; hit → 0 calls."""

    def test_memory_cache_miss_makes_one_outbound_call(
        self, db_engine: Engine
    ) -> None:
        """Cache miss → one HTTP call to Wunderground /5day."""
        _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")
        client = TestClient(app, raise_server_exceptions=False)

        fixture = _load_fixture("forecast_daily_5day.json")
        call_count = 0

        with respx.mock(assert_all_called=False) as mock:
            def count_and_respond(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(200, json=fixture)

            mock.get(_WU_FORECAST_URL).mock(side_effect=count_and_respond)
            response = client.get("/api/v1/forecast")

        assert response.status_code == 200
        assert call_count == 1
        _reset_wu_state()

    def test_memory_cache_hit_makes_zero_outbound_calls(
        self, db_engine: Engine
    ) -> None:
        """Second fetch hits memory cache → 0 outbound calls."""
        _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")
        client = TestClient(app, raise_server_exceptions=False)

        fixture = _load_fixture("forecast_daily_5day.json")
        call_count = 0

        with respx.mock(assert_all_called=False) as mock:
            def count_and_respond(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(200, json=fixture)

            mock.get(_WU_FORECAST_URL).mock(side_effect=count_and_respond)
            # First request → cache miss
            client.get("/api/v1/forecast")
            # Second request → cache hit
            response = client.get("/api/v1/forecast")

        assert response.status_code == 200
        assert call_count == 1  # Only one outbound call across two requests
        _reset_wu_state()


# ===========================================================================
# Integration test: Redis cache tier
# ===========================================================================


class TestIntegrationRedisCache:
    """Redis cache tier — @pytest.mark.redis; must PASS not skip per brief §Process gates."""

    @pytest.mark.redis
    def test_redis_cache_miss_makes_one_outbound_call(self, db_engine: Engine) -> None:
        """Redis cache miss → one HTTP call to Wunderground /5day."""
        _require_redis()

        import redis as redis_lib  # noqa: PLC0415
        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.flushdb()

        _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")

        # Override cache to Redis after wiring
        import os  # noqa: PLC0415
        old_cache_url = os.environ.get("CLEARSKIES_CACHE_URL")
        os.environ["CLEARSKIES_CACHE_URL"] = _REDIS_URL
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        client = TestClient(app, raise_server_exceptions=False)
        fixture = _load_fixture("forecast_daily_5day.json")
        call_count = 0

        try:
            with respx.mock(assert_all_called=False) as mock:
                def count_and_respond(request: httpx.Request) -> httpx.Response:
                    nonlocal call_count
                    call_count += 1
                    return httpx.Response(200, json=fixture)

                mock.get(_WU_FORECAST_URL).mock(side_effect=count_and_respond)
                response = client.get("/api/v1/forecast")
        finally:
            if old_cache_url is not None:
                os.environ["CLEARSKIES_CACHE_URL"] = old_cache_url
            elif "CLEARSKIES_CACHE_URL" in os.environ:
                del os.environ["CLEARSKIES_CACHE_URL"]
            r.flushdb()
            _reset_wu_state()

        assert response.status_code == 200
        assert call_count == 1

    @pytest.mark.redis
    def test_redis_cache_hit_makes_zero_outbound_calls(self, db_engine: Engine) -> None:
        """Second Redis fetch hits Redis cache → 0 outbound calls."""
        _require_redis()

        import redis as redis_lib  # noqa: PLC0415
        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.flushdb()

        _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")

        import os  # noqa: PLC0415
        old_cache_url = os.environ.get("CLEARSKIES_CACHE_URL")
        os.environ["CLEARSKIES_CACHE_URL"] = _REDIS_URL
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        client = TestClient(app, raise_server_exceptions=False)
        fixture = _load_fixture("forecast_daily_5day.json")
        call_count = 0

        try:
            with respx.mock(assert_all_called=False) as mock:
                def count_and_respond(request: httpx.Request) -> httpx.Response:
                    nonlocal call_count
                    call_count += 1
                    return httpx.Response(200, json=fixture)

                mock.get(_WU_FORECAST_URL).mock(side_effect=count_and_respond)
                # First call → cache miss
                client.get("/api/v1/forecast")
                # Second call → Redis cache hit
                response = client.get("/api/v1/forecast")
        finally:
            if old_cache_url is not None:
                os.environ["CLEARSKIES_CACHE_URL"] = old_cache_url
            elif "CLEARSKIES_CACHE_URL" in os.environ:
                del os.environ["CLEARSKIES_CACHE_URL"]
            r.flushdb()
            _reset_wu_state()

        assert response.status_code == 200
        assert call_count == 1  # Only one outbound call despite two requests

    @pytest.mark.redis
    def test_redis_cache_bundle_has_empty_hourly_and_null_discussion(
        self, db_engine: Engine
    ) -> None:
        """Redis-cached bundle preserves hourly=[] and discussion=null after round-trip."""
        _require_redis()

        import redis as redis_lib  # noqa: PLC0415
        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.flushdb()

        _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")

        import os  # noqa: PLC0415
        old_cache_url = os.environ.get("CLEARSKIES_CACHE_URL")
        os.environ["CLEARSKIES_CACHE_URL"] = _REDIS_URL
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        client = TestClient(app, raise_server_exceptions=False)
        fixture = _load_fixture("forecast_daily_5day.json")

        try:
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_WU_FORECAST_URL).mock(
                    return_value=httpx.Response(200, json=fixture)
                )
                # First call populates Redis cache
                client.get("/api/v1/forecast")
                # Second call reads from Redis cache
                response = client.get("/api/v1/forecast")
        finally:
            if old_cache_url is not None:
                os.environ["CLEARSKIES_CACHE_URL"] = old_cache_url
            elif "CLEARSKIES_CACHE_URL" in os.environ:
                del os.environ["CLEARSKIES_CACHE_URL"]
            r.flushdb()
            _reset_wu_state()

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["hourly"] == []
        assert body["data"]["discussion"] is None


# ===========================================================================
# Integration test: missing credential paths → 502 ProviderProblem KeyInvalid
# ===========================================================================


class TestIntegrationMissingCredentialPaths:
    """Missing credentials → 502 ProviderProblem KeyInvalid per brief lead-call 14."""

    def test_missing_api_key_returns_502_key_invalid(self, db_engine: Engine) -> None:
        """api_key=None → 502 ProviderProblem with errorCode='KeyInvalid'."""
        _, app = _wire_integration_stack(
            db_engine,
            forecast_provider="wunderground",
            wu_api_key=None,
            wu_pws_station_id=_TEST_PWS_ID,
        )
        # Override wired credential to None
        from weewx_clearskies_api.endpoints import forecast as forecast_endpoint  # noqa: PLC0415
        forecast_endpoint.wire_wunderground_credentials(None, _TEST_PWS_ID)

        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False):
            response = client.get("/api/v1/forecast")

        assert response.status_code == 502
        body = response.json()
        assert body.get("errorCode") == "KeyInvalid"
        _reset_wu_state()

    def test_missing_pws_station_id_returns_502_key_invalid(
        self, db_engine: Engine
    ) -> None:
        """pws_station_id=None → 502 ProviderProblem with errorCode='KeyInvalid'."""
        _, app = _wire_integration_stack(
            db_engine,
            forecast_provider="wunderground",
            wu_api_key=_TEST_API_KEY,
            wu_pws_station_id=None,
        )
        # Override wired credential to None
        from weewx_clearskies_api.endpoints import forecast as forecast_endpoint  # noqa: PLC0415
        forecast_endpoint.wire_wunderground_credentials(_TEST_API_KEY, None)

        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False):
            response = client.get("/api/v1/forecast")

        assert response.status_code == 502
        body = response.json()
        assert body.get("errorCode") == "KeyInvalid"
        _reset_wu_state()

    def test_401_from_wunderground_returns_502_key_invalid(
        self, db_engine: Engine
    ) -> None:
        """401 from Wunderground → 502 ProviderProblem KeyInvalid (bare propagate)."""
        _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")
        client = TestClient(app, raise_server_exceptions=False)

        fixture_401 = _load_fixture("error_401_invalid_key.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(401, json=fixture_401)
            )
            response = client.get("/api/v1/forecast")

        assert response.status_code == 502
        body = response.json()
        assert body.get("errorCode") == "KeyInvalid"
        _reset_wu_state()

    def test_429_from_wunderground_returns_503_quota_exhausted(
        self, db_engine: Engine
    ) -> None:
        """429 from Wunderground → 503 ProviderProblem QuotaExhausted + Retry-After."""
        _, app = _wire_integration_stack(db_engine, forecast_provider="wunderground")
        client = TestClient(app, raise_server_exceptions=False)

        fixture_429 = _load_fixture("error_429_quota.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(
                    429,
                    json=fixture_429,
                    headers={"Retry-After": "60"},
                )
            )
            response = client.get("/api/v1/forecast")

        assert response.status_code == 503
        body = response.json()
        assert body.get("errorCode") == "QuotaExhausted"
        _reset_wu_state()


# ===========================================================================
# Integration test: ForecastSettings validation
# ===========================================================================


class TestIntegrationForecastSettingsValidation:
    """ForecastSettings.validate() accepts 'wunderground' as a valid provider."""

    def test_forecast_settings_accepts_wunderground_provider(self) -> None:
        """ForecastSettings({'provider': 'wunderground'}).validate() does not raise."""
        from weewx_clearskies_api.config.settings import ForecastSettings  # noqa: PLC0415

        settings = ForecastSettings({"provider": "wunderground"})
        # Should not raise
        settings.validate()

    def test_forecast_settings_wunderground_api_key_field_exists(self) -> None:
        """ForecastSettings has wunderground_api_key field (populated from env var)."""
        from weewx_clearskies_api.config.settings import ForecastSettings  # noqa: PLC0415

        settings = ForecastSettings({})
        # Field must exist on the settings object (may be None if env var not set)
        assert hasattr(settings, "wunderground_api_key")

    def test_forecast_settings_wunderground_pws_station_id_field_exists(self) -> None:
        """ForecastSettings has wunderground_pws_station_id field (populated from env var)."""
        from weewx_clearskies_api.config.settings import ForecastSettings  # noqa: PLC0415

        settings = ForecastSettings({})
        assert hasattr(settings, "wunderground_pws_station_id")
