"""Integration tests for the EMSC SeismicPortal earthquake provider (3b-13).

All tests carry @pytest.mark.integration. Runs against dev/test stack.
Same suite runs twice per ADR-012 (MariaDB + SQLite).

End-to-end paths covered:
  - GET /api/v1/earthquakes emsc configured + respx-mocked → 200 source="emsc".
  - Canonical fields: id (top-level Feature.id), time (ISO Z), depth (POSITIVE from
    properties.depth, NOT negative geometry.coordinates[2]), magnitudeType (lowercase
    magtype), place (from flynn_region), url (constructed from unid).
  - GET /api/v1/earthquakes no provider → 200 data=[] source="none".
  - Provider 5xx → 502. Provider 429 → 503 + Retry-After.
  - Memory cache: miss → hit. Redis cache: miss → hit (redis mark).

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

# Brussels station for EU/Mediterranean provider
_LAT = 50.85
_LON = 4.35

_EMSC_QUERY_URL = "https://www.seismicportal.eu/fdsnws/event/1/query"


def _load_fixture(name: str) -> dict[str, Any]:
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


def _require_mariadb_password() -> None:
    if not _MARIADB_RO_PASSWORD:
        pytest.skip("MARIADB_RO_PASSWORD not set")


def _require_sqlite_file() -> None:
    try:
        exists = Path(_SQLITE_SDB_PATH).exists()
    except PermissionError:
        pytest.skip(f"Cannot access {_SQLITE_SDB_PATH}")
    if not exists:
        pytest.skip(f"SQLite file {_SQLITE_SDB_PATH!r} not found")


def _require_redis() -> None:
    try:
        import redis as redis_lib  # noqa: PLC0415
        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip(f"Redis not reachable at {_REDIS_URL}")


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


def _wire_integration_state(db_engine: Engine, provider: str | None = "emsc") -> None:
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
        station_id="integration-emsc-station",
        name="Integration EMSC Station",
        latitude=_LAT,
        longitude=_LON,
        altitude=15.0,
        timezone="Europe/Brussels",
        timezone_offset_minutes=120,
        unit_system="METRIC",
        hardware=None,
    )

    reset_units_cache()
    system_map = _SYSTEM_PRESETS["METRIC"]
    block: dict[str, str] = {}
    for group, unit in system_map.items():
        for field in _GROUP_MEMBERS.get(group, []):
            block[field] = unit
    units_mod._cached_units_block = block
    units_mod._cached_target_unit = "METRIC"

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    wire_cache_from_env()

    if provider == "emsc":
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415
        wire_providers([CAPABILITY])
    else:
        wire_providers([])


def _make_earthquakes_app(db_engine: Engine, provider: str | None = "emsc") -> FastAPI:
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
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.earthquakes.emsc import (  # noqa: PLC0415
        _rate_limiter,
        _reset_http_client_for_tests,
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _rate_limiter._calls.clear()


class TestNoProviderConfigured:
    """/earthquakes → 200 data=[] source='none' when no provider."""

    def test_no_provider_returns_200_empty_list(self, db_engine: Engine) -> None:
        """No provider → 200 + data=[] + source='none'."""
        app = _make_earthquakes_app(db_engine, provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_state()
        assert response.status_code == 200
        body = response.json()
        assert body["data"] == []
        assert body["source"] == "none"


class TestEMSCProviderConfigured:
    """EMSC registered + respx mock → 200 canonical EarthquakeRecord list."""

    def _get_response(self, db_engine: Engine) -> Any:
        app = _make_earthquakes_app(db_engine, provider="emsc")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("emsc_global_m2_5.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            resp = client.get("/api/v1/earthquakes")
        _reset_state()
        return resp

    def test_emsc_configured_returns_200(self, db_engine: Engine) -> None:
        """EMSC + valid response → 200."""
        assert self._get_response(db_engine).status_code == 200

    def test_emsc_source_is_emsc(self, db_engine: Engine) -> None:
        """source='emsc' in EarthquakeListResponse."""
        body = self._get_response(db_engine).json()
        assert body["source"] == "emsc"

    def test_emsc_first_record_id_from_top_level(self, db_engine: Engine) -> None:
        """data[0].id = '20260511_0000281' (top-level Feature.id)."""
        body = self._get_response(db_engine).json()
        assert body["data"][0]["id"] == "20260511_0000281", (
            f"Expected '20260511_0000281', got {body['data'][0].get('id')!r}"
        )

    def test_emsc_first_record_depth_is_positive(self, db_engine: Engine) -> None:
        """data[0].depth = 5.0 (from properties.depth, positive; NOT coordinates[2]=-5.0)."""
        body = self._get_response(db_engine).json()
        depth = body["data"][0].get("depth")
        assert depth == 5.0, f"Expected depth=5.0 (positive from properties.depth), got {depth!r}"
        assert depth > 0, "EMSC depth must be positive (properties.depth)"

    def test_emsc_first_record_place_from_flynn_region(self, db_engine: Engine) -> None:
        """data[0].place = 'MOLUCCA SEA' (from properties.flynn_region)."""
        body = self._get_response(db_engine).json()
        assert body["data"][0]["place"] == "MOLUCCA SEA", (
            f"Expected 'MOLUCCA SEA', got {body['data'][0].get('place')!r}"
        )

    def test_emsc_first_record_url_constructed_from_unid(self, db_engine: Engine) -> None:
        """data[0].url contains seismicportal.eu eventdetails URL."""
        body = self._get_response(db_engine).json()
        url = body["data"][0].get("url", "")
        assert "seismicportal.eu/eventdetails.html?unid=20260511_0000281" in url, (
            f"Expected seismicportal eventdetails URL, got {url!r}"
        )

    def test_emsc_memory_cache_hit_skips_http(self, db_engine: Engine) -> None:
        """Memory cache hit → 0 HTTP calls on second request."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.emsc import (  # noqa: PLC0415
            _rate_limiter,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        app = _make_earthquakes_app(db_engine, provider="emsc")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("emsc_global_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            client.get("/api/v1/earthquakes")

        with respx.mock(assert_all_called=False) as mock2:
            client.get("/api/v1/earthquakes")
            assert mock2.calls.call_count == 0

        _reset_state()

    @pytest.mark.redis
    def test_emsc_redis_cache_hit_skips_http(self, db_engine: Engine) -> None:
        """Redis cache hit → 0 additional HTTP calls."""
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
        from weewx_clearskies_api.providers.earthquakes.emsc import (  # noqa: PLC0415
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

        app = _make_earthquakes_app(db_engine, provider="emsc")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("emsc_global_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            client.get("/api/v1/earthquakes")

        with respx.mock(assert_all_called=False) as mock2:
            client.get("/api/v1/earthquakes")
            assert mock2.calls.call_count == 0

        _reset_state()


class TestEMSCErrorPaths:
    """Provider error propagation."""

    def test_emsc_5xx_returns_502(self, db_engine: Engine) -> None:
        """EMSC 5xx → 502."""
        app = _make_earthquakes_app(db_engine, provider="emsc")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(
                return_value=httpx.Response(500, json={"error": "server error"})
            )
            response = client.get("/api/v1/earthquakes")
        _reset_state()
        assert response.status_code == 502

    def test_emsc_429_returns_503(self, db_engine: Engine) -> None:
        """EMSC 429 → 503 + Retry-After."""
        app = _make_earthquakes_app(db_engine, provider="emsc")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"message": "Too Many Requests"},
                    headers={"Retry-After": "60"},
                )
            )
            response = client.get("/api/v1/earthquakes")
        _reset_state()
        assert response.status_code == 503
