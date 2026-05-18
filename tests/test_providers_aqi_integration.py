"""Integration tests for the Open-Meteo AQI provider (3b-9).

All tests carry @pytest.mark.integration and run against the docker-compose
dev/test stack (MariaDB or SQLite backend per BACKEND env var).

Same integration suite runs twice per ADR-012 (once MariaDB, once SQLite)
to catch dialect drift. The AQI endpoint itself has no DB dependency, but
running in the real stack confirms the endpoint is DB-stack-agnostic and
wires correctly alongside the DB-backed endpoints.

Redis-backed integration tests carry both @pytest.mark.integration and
@pytest.mark.redis and are skipped unless `pytest -m "integration and redis"`.
Per brief §Process gates: the Redis tier MUST PASS, not skip. If Redis is
not reachable on weather-dev, this is a brief-gate failure that must be
surfaced to the lead before closeout.

End-to-end paths covered:
  - Full startup with [aqi] provider = openmeteo in config.
  - GET /api/v1/aqi/current openmeteo configured + respx-mocked → 200 source="openmeteo".
  - Canonical AQIReading shape: aqi, aqiCategory, aqiMainPollutant, observedAt, source.
  - observedAt is UTC ISO-8601 Z format (LC4 + ADR-020).
  - PARTIAL-DOMAIN field aqiLocation is null in response.
  - GET /api/v1/aqi/current no provider configured → 200 data:null source="none".
  - GET /api/v1/aqi/history → 501 RFC 9457 (always).
  - Provider 5xx → 502 RFC 9457 problem+json.
  - Provider 429 → 503 RFC 9457 + Retry-After.
  - Unknown query param → 422 (extra="forbid" via Depends pattern).
  - AQIResponse envelope validates against OpenAPI AQIResponse schema shape.
  - Memory cache: miss → fetch → hit (both backends).
  - Redis cache: miss → fetch → hit (redis mark; must pass on weather-dev).
  - AQISettings with provider='openmeteo' validates without error.
  - wire_providers([openmeteo.CAPABILITY]) registers in capability registry.

ADR references: ADR-012, ADR-013, ADR-017, ADR-018, ADR-020, ADR-038.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "aqi"
_OPENMETEO_AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
_LAT = 47.6062
_LON = -122.3321


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/aqi/."""
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
    except Exception as exc:  # noqa: BLE001 — narrow redis-py errors below
        import redis as _redis_lib  # noqa: PLC0415
        if isinstance(exc, _redis_lib.exceptions.RedisError):
            pytest.skip(
                f"Redis not reachable at {_REDIS_URL} ({type(exc).__name__}); "
                "start redis compose profile"
            )
        raise


# ---------------------------------------------------------------------------
# Engine fixtures (module-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mariadb_ro_engine() -> Generator[Engine, None, None]:
    """Module-scoped read-only MariaDB engine."""
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
    aqi_provider: str | None = None,
) -> tuple[Any, FastAPI]:
    """Wire the full integration stack for a test app with Open-Meteo AQI.

    Returns (settings, app) with DB, station, units, cache, providers wired.
    Handles both MariaDB and SQLite backends identically.
    """
    import weewx_clearskies_api.providers.aqi.openmeteo as _om_aqi  # noqa: PLC0415
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.db.reflection import (  # noqa: PLC0415
        STOCK_COLUMN_MAP,
        ColumnInfo,
        ColumnRegistry,
    )
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
    from weewx_clearskies_api.providers.aqi.openmeteo import (  # noqa: PLC0415
        _reset_http_client_for_tests,
    )
    from weewx_clearskies_api.services import station as station_mod  # noqa: PLC0415
    from weewx_clearskies_api.services import units as units_mod  # noqa: PLC0415
    from weewx_clearskies_api.services.station import StationInfo, reset_cache  # noqa: PLC0415
    from weewx_clearskies_api.services.units import (  # noqa: PLC0415
        _GROUP_MEMBERS,
        _SYSTEM_PRESETS,
    )
    from weewx_clearskies_api.services.units import (
        reset_cache as reset_units_cache,
    )

    # Reset state
    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _om_aqi._rate_limiter._calls.clear()

    # Wire DB
    wire_engine(engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)

    # Wire station — Seattle coordinates match the AQI test URL mocks.
    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="integration-aqi-station",
        name="Integration AQI Test Station",
        latitude=_LAT,
        longitude=_LON,
        altitude=59.0,
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

    # Build capability list for AQI
    capabilities: list[ProviderCapability] = []
    if aqi_provider == "openmeteo":
        capabilities.append(_om_aqi.CAPABILITY)

    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
    )

    app = create_app(settings)
    return settings, app


# ---------------------------------------------------------------------------
# Integration app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_app_openmeteo(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with Open-Meteo AQI provider configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (
        reset_provider_registry_for_tests,  # noqa: PLC0415
    )
    from weewx_clearskies_api.providers.aqi.openmeteo import (
        _reset_http_client_for_tests,  # noqa: PLC0415
    )

    _, app = _wire_integration_stack(db_engine, aqi_provider="openmeteo")
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_app_no_aqi(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with no AQI provider configured."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (
        reset_provider_registry_for_tests,  # noqa: PLC0415
    )

    _, app = _wire_integration_stack(db_engine, aqi_provider=None)
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()


@pytest.fixture
def integration_client_openmeteo(integration_app_openmeteo: FastAPI) -> TestClient:
    """TestClient for the Open-Meteo AQI integration app."""
    return TestClient(integration_app_openmeteo, raise_server_exceptions=False)


@pytest.fixture
def integration_client_no_aqi(integration_app_no_aqi: FastAPI) -> TestClient:
    """TestClient for the integration app with no AQI provider."""
    return TestClient(integration_app_no_aqi, raise_server_exceptions=False)


# ===========================================================================
# Integration: startup wiring
# ===========================================================================


class TestIntegrationAqiStartupWiring:
    """AQI provider startup wiring + capability registry."""

    def test_aqi_settings_with_openmeteo_validates_without_error(self) -> None:
        """AQISettings({'provider': 'openmeteo'}).validate() does not raise."""
        from weewx_clearskies_api.config.settings import AQISettings  # noqa: PLC0415
        settings = AQISettings({"provider": "openmeteo"})
        settings.validate()  # Should not raise

    def test_openmeteo_capability_wires_into_provider_registry(
        self, db_engine: Engine
    ) -> None:
        """wire_providers([openmeteo.CAPABILITY]) → registry has openmeteo aqi entry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.aqi.openmeteo import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])

        registry = get_provider_registry()
        aqi_entries = [
            p for p in registry
            if p.provider_id == "openmeteo" and p.domain == "aqi"
        ]
        assert len(aqi_entries) == 1, (
            f"Expected 1 openmeteo aqi entry in registry, found {len(aqi_entries)}"
        )
        reset_provider_registry_for_tests()

    def test_openmeteo_capability_domain_is_aqi(self) -> None:
        """CAPABILITY.domain = 'aqi' (not 'alerts', not 'forecast')."""
        from weewx_clearskies_api.providers.aqi.openmeteo import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.domain == "aqi"

    def test_openmeteo_capability_is_keyless(self) -> None:
        """CAPABILITY.auth_required = () — Open-Meteo needs no credentials."""
        from weewx_clearskies_api.providers.aqi.openmeteo import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.auth_required == ()


# ===========================================================================
# Integration: /aqi/current — no provider
# ===========================================================================


class TestIntegrationAqiCurrentNoProvider:
    """GET /api/v1/aqi/current with no AQI provider configured."""

    def test_no_provider_returns_200_with_null_data(
        self, integration_client_no_aqi: TestClient
    ) -> None:
        """No provider → 200 + data:null + source:'none' (LC19 decision tree)."""
        response = integration_client_no_aqi.get("/api/v1/aqi/current")
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )
        body = response.json()
        assert body["data"] is None, f"Expected data=null, got {body.get('data')!r}"
        assert body["source"] == "none", f"Expected source='none', got {body.get('source')!r}"


# ===========================================================================
# Integration: /aqi/current — openmeteo end-to-end
# ===========================================================================


class TestIntegrationAqiCurrentOpenMeteo:
    """End-to-end GET /api/v1/aqi/current with Open-Meteo provider via respx."""

    def test_openmeteo_returns_200_with_openmeteo_source(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """Open-Meteo configured + respx-mocked valid response → 200 source='openmeteo'."""
        data = _load_fixture("openmeteo_current.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )
        body = response.json()
        assert body["source"] == "openmeteo"
        assert body["data"] is not None

    def test_openmeteo_response_matches_aqi_response_envelope_shape(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """AQIResponse envelope: data, units, source, generatedAt all present (OpenAPI schema)."""
        data = _load_fixture("openmeteo_current.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        body = response.json()
        for field in ("data", "units", "source", "generatedAt"):
            assert field in body, f"AQIResponse envelope missing required field {field!r}"

    def test_openmeteo_reading_has_required_canonical_fields(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """AQIReading has observedAt and source (required per OpenAPI schema)."""
        data = _load_fixture("openmeteo_current.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        reading = response.json()["data"]
        assert "observedAt" in reading, "AQIReading must have observedAt (required)"
        assert "source" in reading, "AQIReading must have source (required)"
        assert reading["source"] == "openmeteo"

    def test_openmeteo_observed_at_is_utc_z_format(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """observedAt is UTC ISO-8601 with Z suffix (LC4 + ADR-020)."""
        data = _load_fixture("openmeteo_current.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        reading = response.json()["data"]
        assert reading["observedAt"].endswith("Z"), (
            f"observedAt must end with Z (UTC), got {reading['observedAt']!r}"
        )

    def test_openmeteo_aqi_value_and_category_correct(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """AQI = 73, aqiCategory = 'Moderate' from fixture (51–100 band)."""
        data = _load_fixture("openmeteo_current.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        reading = response.json()["data"]
        assert reading["aqi"] == 73
        assert reading["aqiCategory"] == "Moderate"

    def test_openmeteo_aqi_main_pollutant_is_pm25(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """aqiMainPollutant = 'PM2.5' (argmax of sub-AQIs; PM2.5=73 is highest)."""
        data = _load_fixture("openmeteo_current.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        reading = response.json()["data"]
        assert reading["aqiMainPollutant"] == "PM2.5", (
            f"Expected aqiMainPollutant='PM2.5', got {reading.get('aqiMainPollutant')!r}"
        )

    def test_openmeteo_aqi_location_is_null_partial_domain(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """aqiLocation = null (PARTIAL-DOMAIN — Open-Meteo has no location label field)."""
        data = _load_fixture("openmeteo_current.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        reading = response.json()["data"]
        assert reading.get("aqiLocation") is None, (
            f"Expected aqiLocation=null (PARTIAL-DOMAIN), got {reading.get('aqiLocation')!r}"
        )

    def test_openmeteo_unknown_query_param_returns_422(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """Unknown query param → 422 (extra='forbid' via Depends pattern)."""
        with respx.mock(assert_all_called=False):
            response = integration_client_openmeteo.get(
                "/api/v1/aqi/current?unknown_param=bad"
            )
        assert response.status_code == 422


# ===========================================================================
# Integration: error propagation
# ===========================================================================


class TestIntegrationAqiErrorPropagation:
    """Provider errors → canonical error envelopes."""

    def test_provider_5xx_returns_502_problem_json(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """Open-Meteo 5xx → 502 application/problem+json."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(
                return_value=httpx.Response(500, json={"reason": "server error"})
            )
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        assert response.status_code == 502, (
            f"Expected 502 from 5xx provider, got {response.status_code}: {response.text[:300]}"
        )
        assert "application/problem+json" in response.headers.get("content-type", "")

    def test_provider_429_returns_503_problem_json(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """Open-Meteo 429 → 503 application/problem+json + Retry-After."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"reason": "rate limit"},
                    headers={"Retry-After": "60"},
                )
            )
            response = integration_client_openmeteo.get("/api/v1/aqi/current")

        assert response.status_code == 503, (
            f"Expected 503 from 429 provider, got {response.status_code}: {response.text[:300]}"
        )
        assert "application/problem+json" in response.headers.get("content-type", "")


# ===========================================================================
# Integration: /aqi/history — 501 stub
# ===========================================================================


class TestIntegrationAqiHistory:
    """/api/v1/aqi/history always returns 501 (stub; LC21)."""

    def test_aqi_history_returns_501_with_no_provider(
        self, integration_client_no_aqi: TestClient
    ) -> None:
        """No AQI provider → /aqi/history still returns 501."""
        response = integration_client_no_aqi.get("/api/v1/aqi/history")
        assert response.status_code == 501, (
            f"Expected 501 from /aqi/history, got {response.status_code}"
        )

    def test_aqi_history_returns_501_with_openmeteo_provider(
        self, integration_client_openmeteo: TestClient
    ) -> None:
        """Open-Meteo configured → /aqi/history still returns 501 (stub is unconditional)."""
        response = integration_client_openmeteo.get("/api/v1/aqi/history")
        assert response.status_code == 501, (
            f"Expected 501, got {response.status_code}"
        )

    def test_aqi_history_content_type_is_problem_json(
        self, integration_client_no_aqi: TestClient
    ) -> None:
        """/aqi/history 501 body is application/problem+json (RFC 9457, ADR-018)."""
        response = integration_client_no_aqi.get("/api/v1/aqi/history")
        assert "application/problem+json" in response.headers.get("content-type", "")


# ===========================================================================
# Integration: memory cache — miss → fetch → hit
# ===========================================================================


class TestIntegrationAqiMemoryCache:
    """Open-Meteo AQI provider: memory cache miss → fetch → hit (both backends)."""

    def test_cache_miss_fetches_from_provider_and_caches_result(
        self, db_engine: Engine
    ) -> None:
        """Memory cache miss → one HTTP call; result cached for next poll."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            get_cache,
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.aqi import openmeteo  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openmeteo import (  # noqa: PLC0415
            _build_cache_key,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        openmeteo._rate_limiter._calls.clear()
        wire_cache_from_env()

        data = _load_fixture("openmeteo_current.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            reading = openmeteo.fetch(lat=_LAT, lon=_LON)
            call_count = mock.calls.call_count

        assert call_count == 1, f"Expected 1 HTTP call on cache miss, got {call_count}"
        assert reading is not None
        assert reading.source == "openmeteo"
        assert reading.aqi == 73

        # Cache was populated
        cached = get_cache().get(_build_cache_key(_LAT, _LON))
        assert cached is not None, "Reading must be cached after cache miss"

        reset_cache_for_tests()
        _reset_http_client_for_tests()

    def test_cache_hit_skips_provider_call(self, db_engine: Engine) -> None:
        """Memory cache hit → zero HTTP calls; cached reading returned."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.aqi import openmeteo  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openmeteo import (
            _reset_http_client_for_tests,  # noqa: PLC0415
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        openmeteo._rate_limiter._calls.clear()
        wire_cache_from_env()

        data = _load_fixture("openmeteo_current.json")

        # First fetch — fills memory cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENMETEO_AQ_URL).mock(return_value=httpx.Response(200, json=data))
            reading1 = openmeteo.fetch(lat=_LAT, lon=_LON)

        # Second fetch — should come from cache (zero calls)
        with respx.mock(assert_all_called=False) as mock2:
            reading2 = openmeteo.fetch(lat=_LAT, lon=_LON)
            cache_hit_calls = mock2.calls.call_count

        assert cache_hit_calls == 0, (
            f"Expected 0 HTTP calls on cache hit, got {cache_hit_calls}"
        )
        assert reading1 is not None and reading2 is not None
        assert reading1.aqi == reading2.aqi
        assert reading1.source == reading2.source

        reset_cache_for_tests()
        _reset_http_client_for_tests()


# ===========================================================================
# Integration: Redis cache (optional, redis mark — MUST PASS per brief)
# ===========================================================================


@pytest.mark.redis
class TestIntegrationAqiRedisCache:
    """Real Redis from the docker-compose redis profile.

    Per brief §Process gates: Redis tier MUST PASS, not skip.
    If Redis is not reachable on weather-dev, this is a brief-gate failure
    that must be surfaced to the lead via SendMessage BEFORE closeout.
    """

    def test_aqi_redis_cache_miss_stores_reading(self, db_engine: Engine) -> None:
        """Redis cache miss → one HTTP call → reading stored in Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.aqi import openmeteo  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openmeteo import (  # noqa: PLC0415
            _build_cache_key,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        openmeteo._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        data = _load_fixture("openmeteo_current.json")

        try:
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_OPENMETEO_AQ_URL).mock(
                    return_value=httpx.Response(200, json=data)
                )
                reading = openmeteo.fetch(lat=_LAT, lon=_LON)
                call_count = mock.calls.call_count

            assert call_count == 1, (
                f"Expected 1 HTTP call on Redis cache miss, got {call_count}"
            )
            assert reading is not None
            assert reading.source == "openmeteo"

            # Verify reading is in Redis
            cache_key = _build_cache_key(_LAT, _LON)
            cached = cache_mod._cache.get(cache_key)
            assert cached is not None, "Reading should be stored in Redis after cache miss"

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()

    def test_aqi_redis_cache_hit_skips_provider_call(self, db_engine: Engine) -> None:
        """Redis cache hit → zero HTTP calls; reading returned from Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.aqi import openmeteo  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openmeteo import (
            _reset_http_client_for_tests,  # noqa: PLC0415
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        openmeteo._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        data = _load_fixture("openmeteo_current.json")

        try:
            # First fetch — fills Redis cache
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_OPENMETEO_AQ_URL).mock(
                    return_value=httpx.Response(200, json=data)
                )
                reading1 = openmeteo.fetch(lat=_LAT, lon=_LON)
                assert mock.calls.call_count > 0

            # Second fetch — should hit Redis; zero calls
            with respx.mock(assert_all_called=False) as mock2:
                reading2 = openmeteo.fetch(lat=_LAT, lon=_LON)
                second_call_count = mock2.calls.call_count

            assert second_call_count == 0, (
                f"Expected 0 HTTP calls on Redis cache hit, got {second_call_count}"
            )
            assert reading1 is not None and reading2 is not None
            assert reading1.aqi == reading2.aqi
            assert reading1.source == reading2.source

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()
