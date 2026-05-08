"""Integration tests for the Aeris forecast provider (3b round 4).

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
that must be surfaced to the lead before closeout.

Cache integration covered:
  - Memory cache: miss → fetch (2 Aeris calls) → bundle returned.
  - Memory cache: second fetch hits cache → 0 Aeris outbound calls.
  - Redis cache: miss → fetch (2 Aeris calls) → bundle stored in Redis.
  - Redis cache: second fetch hits Redis cache → 0 Aeris outbound calls.

Dispatch table:
  - ('forecast', 'aeris') is in PROVIDER_MODULES dispatch table.

Startup wiring:
  - ForecastSettings with provider='aeris' passes validate().
  - ForecastSettings credentials loaded from env vars.

ADR references: ADR-006, ADR-007, ADR-012, ADR-017, ADR-019, ADR-038.
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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "aeris"

_LAT = 47.6062
_LON = -122.3321
_LOCATION = f"{round(_LAT, 4)},{round(_LON, 4)}"
_AERIS_HOURLY_URL = f"https://data.api.xweather.com/forecasts/{_LOCATION}"
_AERIS_DAYNIGHT_URL = f"https://data.api.xweather.com/forecasts/{_LOCATION}"

_TEST_CLIENT_ID = "INTEGRATION_TEST_CLIENT_ID"
_TEST_CLIENT_SECRET = "INTEGRATION_TEST_CLIENT_SECRET"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/aeris/."""
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
    except Exception as exc:  # noqa: BLE001 — narrow to redis-py errors below
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
    from weewx_clearskies_api.providers.forecast.aeris import _reset_http_client_for_tests  # noqa: PLC0415
    import weewx_clearskies_api.providers.forecast.aeris as _aeris  # noqa: PLC0415

    # Reset state
    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _aeris._rate_limiter._calls.clear()

    # Wire DB
    wire_engine(engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)

    # Wire station — Seattle coordinates match the Aeris fixtures.
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
    if forecast_provider == "aeris":
        from weewx_clearskies_api.providers.forecast import aeris as forecast_aeris  # noqa: PLC0415
        capabilities.append(forecast_aeris.CAPABILITY)

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


def _mock_aeris_both_calls(
    mock: Any,
    hourly_data: dict[str, Any],
    daynight_data: dict[str, Any],
) -> None:
    """Register respx routes for both Aeris outbound calls.

    Both hourly and daynight share the same base URL; respx matches on
    the filter= query param to distinguish them.
    """
    mock.get(
        _AERIS_HOURLY_URL,
        params={"filter": "1hr"},
    ).mock(return_value=httpx.Response(200, json=hourly_data))

    mock.get(
        _AERIS_DAYNIGHT_URL,
        params={"filter": "daynight"},
    ).mock(return_value=httpx.Response(200, json=daynight_data))


# ---------------------------------------------------------------------------
# Integration app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_app_aeris(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with Aeris forecast provider configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers.forecast.aeris import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(db_engine, forecast_provider="aeris")
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_aeris(integration_app_aeris: FastAPI) -> TestClient:
    """TestClient for the integration app with Aeris configured."""
    return TestClient(integration_app_aeris, raise_server_exceptions=False)


# ===========================================================================
# Integration test: dispatch table — aeris is in PROVIDER_MODULES
# ===========================================================================


class TestIntegrationDispatchTableHasAeris:
    """('forecast', 'aeris') is in the dispatch table as of 3b-4."""

    def test_aeris_is_in_forecast_dispatch_table(self) -> None:
        """get_provider_module(domain='forecast', provider_id='aeris') returns aeris module."""
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415
        module = get_provider_module(domain="forecast", provider_id="aeris")
        assert module is not None
        assert hasattr(module, "CAPABILITY")
        assert hasattr(module, "fetch")
        assert module.CAPABILITY.provider_id == "aeris"

    def test_aeris_module_has_correct_domain(self) -> None:
        """Aeris module from dispatch table has domain='forecast'."""
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415
        module = get_provider_module(domain="forecast", provider_id="aeris")
        assert module.CAPABILITY.domain == "forecast"


# ===========================================================================
# Integration test: memory cache — miss → fetch → hit (both backends)
# ===========================================================================


class TestIntegrationMemoryCacheMissAndHit:
    """Aeris forecast provider: memory cache miss → fetch → cache hit flow."""

    def test_cache_miss_fetches_from_aeris_and_stores_bundle(
        self, db_engine: Engine
    ) -> None:
        """Memory cache miss → two Aeris HTTP calls → bundle stored."""
        from weewx_clearskies_api.providers.forecast import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
            get_cache,
        )
        from weewx_clearskies_api.providers.forecast.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()
        wire_cache_from_env()

        hourly_data = _load_fixture("forecasts_hourly.json")
        daynight_data = _load_fixture("forecasts_daynight.json")

        with respx.mock(assert_all_called=False) as mock:
            _mock_aeris_both_calls(mock, hourly_data, daynight_data)
            bundle = aeris.fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="US",
                client_id=_TEST_CLIENT_ID,
                client_secret=_TEST_CLIENT_SECRET,
            )
            call_count = mock.calls.call_count

        assert call_count == 2, f"Expected 2 Aeris calls on cache miss, got {call_count}"
        assert bundle.source == "aeris"
        assert len(bundle.hourly) == 24
        assert len(bundle.daily) == 7

        # Cache populated
        cache_key = aeris._build_cache_key(_LAT, _LON, "US")
        cached = get_cache().get(cache_key)
        assert cached is not None

        reset_cache_for_tests()
        _reset_http_client_for_tests()

    def test_cache_hit_skips_aeris_calls_and_returns_same_bundle(
        self, db_engine: Engine
    ) -> None:
        """Memory cache hit → zero Aeris HTTP calls; bundle matches cached."""
        from weewx_clearskies_api.providers.forecast import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.forecast.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()
        wire_cache_from_env()

        hourly_data = _load_fixture("forecasts_hourly.json")
        daynight_data = _load_fixture("forecasts_daynight.json")

        # First fetch — fills memory cache
        with respx.mock(assert_all_called=False) as mock:
            _mock_aeris_both_calls(mock, hourly_data, daynight_data)
            bundle1 = aeris.fetch(
                lat=_LAT, lon=_LON, target_unit="US",
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )

        # Second fetch — should come from cache (zero calls)
        with respx.mock(assert_all_called=False) as mock2:
            bundle2 = aeris.fetch(
                lat=_LAT, lon=_LON, target_unit="US",
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )
            cache_hit_calls = mock2.calls.call_count

        assert cache_hit_calls == 0, (
            f"Expected 0 Aeris calls on cache hit, got {cache_hit_calls}"
        )
        assert bundle2.source == "aeris"
        assert len(bundle2.hourly) == len(bundle1.hourly)
        assert len(bundle2.daily) == len(bundle1.daily)

        reset_cache_for_tests()
        _reset_http_client_for_tests()


# ===========================================================================
# Integration test: Redis cache (optional, redis mark — MUST PASS per brief)
# ===========================================================================


@pytest.mark.redis
class TestIntegrationAerisRedisBackend:
    """Real Redis from the docker-compose redis profile.

    Per brief §Process gates: Redis tier MUST PASS, not skip.
    If Redis is not reachable on weather-dev, this is a brief-gate failure
    that must be surfaced to the lead via SendMessage BEFORE closeout.
    """

    def test_aeris_forecast_with_real_redis_cache_miss_makes_two_calls(
        self, db_engine: Engine
    ) -> None:
        """Redis cache miss → two Aeris HTTP calls → bundle stored in Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.forecast import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        hourly_data = _load_fixture("forecasts_hourly.json")
        daynight_data = _load_fixture("forecasts_daynight.json")

        try:
            with respx.mock(assert_all_called=False) as mock:
                _mock_aeris_both_calls(mock, hourly_data, daynight_data)
                bundle = aeris.fetch(
                    lat=_LAT, lon=_LON, target_unit="US",
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
                call_count = mock.calls.call_count

            assert call_count == 2, (
                f"Expected 2 Aeris calls on Redis cache miss, got {call_count}"
            )
            assert bundle.source == "aeris"
            assert len(bundle.hourly) == 24
            assert len(bundle.daily) == 7

            # Verify bundle is in Redis
            cache_key = aeris._build_cache_key(_LAT, _LON, "US")
            import weewx_clearskies_api.providers._common.cache as cache_mod2  # noqa: PLC0415
            cached = cache_mod2._cache.get(cache_key)
            assert cached is not None, "Bundle should be stored in Redis after cache miss"

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()

    def test_aeris_forecast_with_real_redis_cache_hit_skips_aeris_calls(
        self, db_engine: Engine
    ) -> None:
        """Redis cache hit → zero Aeris HTTP calls; bundle returned from Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.forecast import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        hourly_data = _load_fixture("forecasts_hourly.json")
        daynight_data = _load_fixture("forecasts_daynight.json")

        try:
            # First fetch — fills Redis cache
            with respx.mock(assert_all_called=False) as mock:
                _mock_aeris_both_calls(mock, hourly_data, daynight_data)
                bundle1 = aeris.fetch(
                    lat=_LAT, lon=_LON, target_unit="US",
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
                first_call_count = mock.calls.call_count

            assert first_call_count > 0, "First request should have called Aeris API"
            assert bundle1.source == "aeris"

            # Second fetch — should hit Redis cache; zero calls
            with respx.mock(assert_all_called=False) as mock2:
                bundle2 = aeris.fetch(
                    lat=_LAT, lon=_LON, target_unit="US",
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
                second_call_count = mock2.calls.call_count

            assert second_call_count == 0, (
                f"Expected 0 Aeris calls on Redis cache hit, got {second_call_count}"
            )
            assert bundle2.source == "aeris"
            assert len(bundle2.hourly) == len(bundle1.hourly)
            assert len(bundle2.daily) == len(bundle1.daily)

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()


# ===========================================================================
# Integration test: startup wiring + validation
# ===========================================================================


class TestIntegrationStartupWiring:
    """ForecastSettings with aeris provider wires and validates correctly."""

    def test_forecast_settings_with_aeris_passes_validate(self) -> None:
        """ForecastSettings({'provider': 'aeris'}).validate() does not raise."""
        from weewx_clearskies_api.config.settings import ForecastSettings  # noqa: PLC0415
        settings = ForecastSettings({"provider": "aeris"})
        settings.validate()  # Should not raise

    def test_aeris_capability_wires_into_provider_registry(self, db_engine: Engine) -> None:
        """wire_providers([aeris.CAPABILITY]) → get_provider_registry() has aeris entry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast.aeris import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])

        registry = get_provider_registry()
        aeris_entries = [p for p in registry if p.provider_id == "aeris"]
        assert len(aeris_entries) == 1
        assert aeris_entries[0].domain == "forecast"

        reset_provider_registry_for_tests()

    def test_aeris_missing_credentials_raises_key_invalid_at_fetch_time(
        self, db_engine: Engine
    ) -> None:
        """Aeris configured but credentials missing → KeyInvalid at fetch (not startup)."""
        from weewx_clearskies_api.providers.forecast import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.forecast.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()
        wire_cache_from_env()

        # Missing both credentials — should raise KeyInvalid at call time
        with pytest.raises(KeyInvalid):
            aeris.fetch(
                lat=_LAT, lon=_LON, target_unit="US",
                client_id=None, client_secret=None,
            )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
