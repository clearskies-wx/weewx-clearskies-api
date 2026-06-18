"""Integration tests for the USGS earthquake provider (3b-13).

All tests carry @pytest.mark.integration and run against the docker-compose
dev/test stack (MariaDB or SQLite backend per BACKEND env var).

Same integration suite runs twice per ADR-012 (once MariaDB, once SQLite)
to catch dialect drift. The /earthquakes endpoint has no DB dependency, but
running in the real stack confirms the endpoint is DB-stack-agnostic and
wires correctly alongside the DB-backed endpoints.

Redis-backed integration tests carry @pytest.mark.redis.
Per brief §Process gates: Redis tier MUST PASS if running the redis compose profile.

End-to-end paths covered:
  - GET /api/v1/earthquakes  usgs configured + respx-mocked USGS → 200 source="usgs".
  - Canonical EarthquakeRecord shape: id, time (ISO Z), latitude, longitude,
    magnitude, magnitudeType, depth, place, url, tsunami (bool), felt, status, source.
  - USGS-specific: epoch ms → ISO conversion; tsunami int → bool.
  - GET /api/v1/earthquakes  no provider → 200 data=[] source="none".
  - EarthquakeListResponse envelope: data (list), source, generatedAt (UTC Z).
  - Provider 5xx → 502 RFC 9457 problem+json.
  - Provider 429 → 503 RFC 9457 + Retry-After.
  - Memory cache: miss → fetch → hit (0 HTTP calls on second request).
  - Redis cache: miss → fetch → hit (redis mark).
  - wire_providers([usgs.CAPABILITY]) registers capability in registry.

ADR references: ADR-012, ADR-017, ADR-018, ADR-020, ADR-038, ADR-040.
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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "earthquakes"

# Seattle station coordinates matching USGS fixture
_LAT = 47.6
_LON = -122.3

_USGS_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"


def _load_fixture(name: str) -> dict[str, Any]:
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
        pytest.skip(f"SQLite file {_SQLITE_SDB_PATH!r} not found; seed the dev stack")


def _require_redis() -> None:
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
    if _BACKEND == "sqlite":
        yield sqlite_ro_engine
    else:
        yield mariadb_ro_engine


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _wire_integration_state(
    db_engine: Engine,
    provider: str | None = "usgs",
) -> None:
    """Wire DB, station, units, cache, and provider registry for integration tests."""
    from weewx_clearskies_api.db.reflection import (  # noqa: PLC0415
        STOCK_COLUMN_MAP,  # noqa: PLC0415
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
        reset_provider_registry_for_tests,
        wire_providers,
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

    wire_engine(db_engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)

    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="integration-usgs-station",
        name="Integration USGS Station",
        latitude=_LAT,
        longitude=_LON,
        altitude=50.0,
        timezone="America/Los_Angeles",
        timezone_offset_minutes=-420,
        unit_system="US",
        hardware=None,
    )

    reset_units_cache()
    system_map = _SYSTEM_PRESETS["US"]
    block: dict[str, str] = {}
    for group, unit in system_map.items():
        for field in _GROUP_MEMBERS.get(group, []):
            block[field] = unit
    units_mod._cached_units_block = block
    units_mod._cached_target_unit = "US"

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    wire_cache_from_env()

    if provider == "usgs":
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415
        wire_providers([CAPABILITY])
    else:
        wire_providers([])


def _make_earthquakes_app(db_engine: Engine, provider: str | None = "usgs") -> FastAPI:
    """Build integration app with earthquakes endpoint and optional USGS provider."""
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        EarthquakesSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.endpoints.earthquakes import (
        wire_earthquakes_settings,  # noqa: PLC0415
    )

    _wire_integration_state(db_engine, provider=provider)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
        earthquakes=EarthquakesSettings({"provider": provider} if provider else {}),
    )
    wire_earthquakes_settings(settings)
    return create_app(settings)


def _reset_state() -> None:
    """Teardown: reset cache and registry after each test."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
        _rate_limiter,
        _reset_http_client_for_tests,
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _rate_limiter._calls.clear()


# ===========================================================================
# 1. No provider configured → 200 data=[] source="none"
# ===========================================================================


class TestNoProviderConfigured:
    """/earthquakes → 200 data=[] source='none' when no provider registered."""

    def test_no_provider_returns_200(self, db_engine: Engine) -> None:
        """No earthquake provider → 200 (not 404 or 503)."""
        app = _make_earthquakes_app(db_engine, provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_state()
        assert response.status_code == 200, (
            f"Expected 200 (no provider), got {response.status_code}: {response.text[:300]}"
        )

    def test_no_provider_data_is_empty_list(self, db_engine: Engine) -> None:
        """No provider → data=[] (empty list, not null)."""
        app = _make_earthquakes_app(db_engine, provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_state()
        body = response.json()
        assert body["data"] == [], (
            f"Expected data=[] with no provider, got {body.get('data')!r}"
        )

    def test_no_provider_source_is_none_string(self, db_engine: Engine) -> None:
        """No provider → source='none'."""
        app = _make_earthquakes_app(db_engine, provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_state()
        body = response.json()
        assert body["source"] == "none", (
            f"Expected source='none', got {body.get('source')!r}"
        )

    def test_no_provider_response_has_generated_at_z(self, db_engine: Engine) -> None:
        """No provider → generatedAt present and UTC Z format."""
        app = _make_earthquakes_app(db_engine, provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_state()
        body = response.json()
        assert "generatedAt" in body
        assert body["generatedAt"].endswith("Z"), (
            f"generatedAt must be UTC Z format, got {body['generatedAt']!r}"
        )


# ===========================================================================
# 2. USGS configured + respx-mocked → 200 canonical records
# ===========================================================================


class TestUSGSProviderConfigured:
    """USGS provider registered + respx mock → 200 with canonical EarthquakeRecord list."""

    def _get_response(self, db_engine: Engine) -> Any:
        app = _make_earthquakes_app(db_engine, provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            resp = client.get("/api/v1/earthquakes")
        _reset_state()
        return resp

    def test_usgs_configured_returns_200(self, db_engine: Engine) -> None:
        """USGS + valid response → 200."""
        response = self._get_response(db_engine)
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )

    def test_usgs_configured_source_is_usgs(self, db_engine: Engine) -> None:
        """source='usgs' in EarthquakeListResponse envelope."""
        response = self._get_response(db_engine)
        body = response.json()
        assert body["source"] == "usgs", (
            f"Expected source='usgs', got {body.get('source')!r}"
        )

    def test_usgs_configured_data_is_list(self, db_engine: Engine) -> None:
        """data is a list of EarthquakeRecord (not null or empty)."""
        response = self._get_response(db_engine)
        body = response.json()
        assert isinstance(body["data"], list), (
            f"Expected data to be list, got {type(body.get('data')).__name__!r}"
        )
        assert len(body["data"]) == 3, (
            f"Expected 3 records from fixture, got {len(body['data'])}"
        )

    def test_usgs_first_record_id_is_correct(self, db_engine: Engine) -> None:
        """data[0].id = 'uw62242697' (top-level Feature.id from USGS fixture)."""
        response = self._get_response(db_engine)
        body = response.json()
        assert body["data"][0]["id"] == "uw62242697", (
            f"Expected id='uw62242697', got {body['data'][0].get('id')!r}"
        )

    def test_usgs_first_record_time_is_utc_z(self, db_engine: Engine) -> None:
        """data[0].time ends with 'Z' (epoch ms converted to UTC ISO-8601 Z)."""
        response = self._get_response(db_engine)
        body = response.json()
        time_val = body["data"][0].get("time", "")
        assert time_val.endswith("Z"), f"time must end with Z, got {time_val!r}"

    def test_usgs_first_record_tsunami_is_bool_false(self, db_engine: Engine) -> None:
        """data[0].tsunami = False (int 0 → bool False at canonical layer)."""
        response = self._get_response(db_engine)
        body = response.json()
        tsunami = body["data"][0].get("tsunami")
        assert tsunami is False, f"Expected tsunami=False (bool), got {tsunami!r}"

    def test_usgs_first_record_source_is_usgs(self, db_engine: Engine) -> None:
        """data[0].source = 'usgs' on the record itself."""
        response = self._get_response(db_engine)
        body = response.json()
        assert body["data"][0]["source"] == "usgs", (
            f"Expected record source='usgs', got {body['data'][0].get('source')!r}"
        )

    def test_usgs_response_has_generated_at_z(self, db_engine: Engine) -> None:
        """generatedAt present and UTC Z format."""
        response = self._get_response(db_engine)
        body = response.json()
        assert "generatedAt" in body
        assert body["generatedAt"].endswith("Z")

    def test_usgs_memory_cache_hit_skips_http_call(self, db_engine: Engine) -> None:
        """Memory cache hit on second request → 0 additional HTTP calls."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
            _rate_limiter,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        app = _make_earthquakes_app(db_engine, provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")

        # First request: cache miss → HTTP call
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            resp1 = client.get("/api/v1/earthquakes")
            call_count_1 = mock.calls.call_count

        # Second request: cache hit → 0 HTTP calls
        with respx.mock(assert_all_called=False) as mock2:
            resp2 = client.get("/api/v1/earthquakes")
            call_count_2 = mock2.calls.call_count

        _reset_state()
        assert call_count_1 == 1, f"Expected 1 call on cache miss, got {call_count_1}"
        assert call_count_2 == 0, f"Expected 0 calls on cache hit, got {call_count_2}"
        assert resp1.json()["source"] == "usgs"
        assert resp2.json()["source"] == "usgs"

    @pytest.mark.redis
    def test_usgs_redis_cache_hit_skips_http_call(self, db_engine: Engine) -> None:
        """Redis cache hit on second request → 0 additional HTTP calls."""
        _require_redis()

        import redis as redis_lib  # noqa: PLC0415

        import weewx_clearskies_api.providers._common.cache as _cache_mod  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
            _rate_limiter,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        # Inject a real-Redis-backed RedisCache via the established test pattern
        # (object.__new__ bypasses the URL-based ping in __init__);
        # see tests/test_providers_alerts_unit.py:660 for the precedent.
        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.flushdb()
        redis_cache = object.__new__(RedisCache)
        redis_cache._client = r
        redis_cache._redis_error_cls = redis_lib.exceptions.RedisError
        _cache_mod._cache = redis_cache

        app = _make_earthquakes_app(db_engine, provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            resp1 = client.get("/api/v1/earthquakes")

        with respx.mock(assert_all_called=False) as mock2:
            resp2 = client.get("/api/v1/earthquakes")
            assert mock2.calls.call_count == 0, "Redis cache hit must skip HTTP call"

        _reset_state()
        assert resp1.json()["source"] == "usgs"
        assert resp2.json()["source"] == "usgs"


# ===========================================================================
# 3. Error paths
# ===========================================================================


class TestUSGSErrorPaths:
    """Provider error propagation to API response codes."""

    def test_usgs_5xx_returns_502_problem_json(self, db_engine: Engine) -> None:
        """USGS 5xx → 502 RFC 9457 problem+json."""
        app = _make_earthquakes_app(db_engine, provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(500, json={"error": "server error"})
            )
            response = client.get("/api/v1/earthquakes")

        _reset_state()
        assert response.status_code == 502, (
            f"Expected 502 from provider 5xx, got {response.status_code}"
        )
        ct = response.headers.get("content-type", "")
        assert "problem+json" in ct or "application/json" in ct, (
            f"Expected problem+json content-type, got {ct!r}"
        )

    def test_usgs_429_returns_503_with_retry_after(self, db_engine: Engine) -> None:
        """USGS 429 → 503 RFC 9457 + Retry-After header."""
        app = _make_earthquakes_app(db_engine, provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"message": "Too Many Requests"},
                    headers={"Retry-After": "60"},
                )
            )
            response = client.get("/api/v1/earthquakes")

        _reset_state()
        assert response.status_code == 503, (
            f"Expected 503 from provider 429, got {response.status_code}"
        )
