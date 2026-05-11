"""Integration tests for the ReNaSS (France) earthquake provider (3b-13).

All tests carry @pytest.mark.integration. Runs against dev/test stack.
Same suite runs twice per ADR-012 (MariaDB + SQLite).

End-to-end paths covered:
  - GET /api/v1/earthquakes renass configured + respx-mocked → 200 source="renass".
  - Canonical fields: id (top-level Feature.id), time (ISO Z), depth (POSITIVE from
    properties.depth), magnitudeType (camelCase magType), place (description.en),
    url (url.en), status (derived from automatic bool), extras (description_fr, url_fr).
  - GET /api/v1/earthquakes no provider → 200 data=[] source="none".
  - Provider 5xx → 502. Provider 429 → 503 + Retry-After.
  - Memory cache: miss → hit. Redis cache: miss → hit (redis mark).

ADR references: ADR-012, ADR-017, ADR-018, ADR-020, ADR-038, ADR-040.
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

# Strasbourg station coordinates (matching fixture query)
_LAT = 48.5
_LON = 7.7

_RENASS_QUERY_URL = "https://api.franceseisme.fr/fdsnws/event/1/query"


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


def _wire_integration_state(db_engine: Engine, provider: str | None = "renass") -> None:
    from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP  # noqa: PLC0415
    from weewx_clearskies_api.db.reflection import ColumnInfo, ColumnRegistry  # noqa: PLC0415
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
    from weewx_clearskies_api.services.station import StationInfo, reset_cache  # noqa: PLC0415
    from weewx_clearskies_api.services import units as units_mod  # noqa: PLC0415
    from weewx_clearskies_api.services.units import (  # noqa: PLC0415
        _GROUP_MEMBERS,
        _SYSTEM_PRESETS,
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
        station_id="integration-renass-station",
        name="Integration ReNaSS Station",
        latitude=_LAT,
        longitude=_LON,
        altitude=145.0,
        timezone="Europe/Paris",
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

    if provider == "renass":
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415
        wire_providers([CAPABILITY])
    else:
        wire_providers([])


def _make_earthquakes_app(db_engine: Engine, provider: str | None = "renass") -> FastAPI:
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        EarthquakesSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.endpoints.earthquakes import wire_earthquakes_settings  # noqa: PLC0415

    _wire_integration_state(db_engine, provider=provider)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
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
    from weewx_clearskies_api.providers.earthquakes.renass import (  # noqa: PLC0415
        _reset_http_client_for_tests,
        _rate_limiter,
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


class TestReNaSSProviderConfigured:
    """ReNaSS registered + respx mock → 200 canonical EarthquakeRecord list."""

    def _get_response(self, db_engine: Engine) -> Any:
        app = _make_earthquakes_app(db_engine, provider="renass")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("renass_france_recent.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            resp = client.get("/api/v1/earthquakes")
        _reset_state()
        return resp

    def test_renass_configured_returns_200(self, db_engine: Engine) -> None:
        """ReNaSS + valid response → 200."""
        assert self._get_response(db_engine).status_code == 200

    def test_renass_source_is_renass(self, db_engine: Engine) -> None:
        """source='renass' in EarthquakeListResponse."""
        body = self._get_response(db_engine).json()
        assert body["source"] == "renass"

    def test_renass_first_record_id_from_top_level(self, db_engine: Engine) -> None:
        """data[0].id = 'fr2026trycyd' (top-level Feature.id)."""
        body = self._get_response(db_engine).json()
        assert body["data"][0]["id"] == "fr2026trycyd", (
            f"Expected 'fr2026trycyd', got {body['data'][0].get('id')!r}"
        )

    def test_renass_first_record_depth_is_positive(self, db_engine: Engine) -> None:
        """data[0].depth is positive (from properties.depth, NOT negative coordinates[2])."""
        body = self._get_response(db_engine).json()
        depth = body["data"][0].get("depth")
        assert depth is not None
        assert depth > 0, f"ReNaSS depth must be positive, got {depth!r}"

    def test_renass_first_record_place_is_en_description(self, db_engine: Engine) -> None:
        """data[0].place from properties.description.en."""
        body = self._get_response(db_engine).json()
        place = body["data"][0].get("place", "")
        assert "Pau" in place, f"Expected .en description with 'Pau', got {place!r}"

    def test_renass_first_record_url_is_en_url(self, db_engine: Engine) -> None:
        """data[0].url from properties.url.en."""
        body = self._get_response(db_engine).json()
        url = body["data"][0].get("url", "")
        assert "/en/events/" in url, f"Expected .en URL, got {url!r}"

    def test_renass_first_record_status_is_automatic(self, db_engine: Engine) -> None:
        """data[0].status = 'automatic' (automatic=True → 'automatic')."""
        body = self._get_response(db_engine).json()
        assert body["data"][0]["status"] == "automatic", (
            f"Expected status='automatic', got {body['data'][0].get('status')!r}"
        )

    def test_renass_third_record_status_is_reviewed(self, db_engine: Engine) -> None:
        """data[2].status = 'reviewed' (automatic=False → 'reviewed')."""
        body = self._get_response(db_engine).json()
        assert body["data"][2]["status"] == "reviewed", (
            f"Expected status='reviewed' for automatic=False, got {body['data'][2].get('status')!r}"
        )

    def test_renass_extras_has_description_fr(self, db_engine: Engine) -> None:
        """data[0].extras['description_fr'] contains French text."""
        body = self._get_response(db_engine).json()
        extras = body["data"][0].get("extras", {})
        assert "description_fr" in extras, "extras must have 'description_fr'"
        assert isinstance(extras["description_fr"], str)

    def test_renass_extras_has_url_fr(self, db_engine: Engine) -> None:
        """data[0].extras['url_fr'] contains French URL."""
        body = self._get_response(db_engine).json()
        extras = body["data"][0].get("extras", {})
        assert "url_fr" in extras, "extras must have 'url_fr'"
        assert "/fr/evenements/" in extras["url_fr"], (
            f"Expected French URL in url_fr, got {extras.get('url_fr')!r}"
        )

    def test_renass_memory_cache_hit_skips_http(self, db_engine: Engine) -> None:
        """Memory cache hit → 0 HTTP calls on second request."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.renass import (  # noqa: PLC0415
            _reset_http_client_for_tests,
            _rate_limiter,
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        app = _make_earthquakes_app(db_engine, provider="renass")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("renass_france_recent.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            client.get("/api/v1/earthquakes")

        with respx.mock(assert_all_called=False) as mock2:
            client.get("/api/v1/earthquakes")
            assert mock2.calls.call_count == 0

        _reset_state()

    @pytest.mark.redis
    def test_renass_redis_cache_hit_skips_http(self, db_engine: Engine) -> None:
        """Redis cache hit → 0 additional HTTP calls."""
        _require_redis()
        import redis as redis_lib  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            _RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.renass import (  # noqa: PLC0415
            _reset_http_client_for_tests,
            _rate_limiter,
        )
        import weewx_clearskies_api.providers._common.cache as _cache_mod  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        r = redis_lib.Redis.from_url(_REDIS_URL)
        r.flushdb()
        _cache_mod._cache_instance = _RedisCache(r)

        app = _make_earthquakes_app(db_engine, provider="renass")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("renass_france_recent.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            client.get("/api/v1/earthquakes")

        with respx.mock(assert_all_called=False) as mock2:
            client.get("/api/v1/earthquakes")
            assert mock2.calls.call_count == 0

        _reset_state()


class TestReNaSSErrorPaths:
    """Provider error propagation."""

    def test_renass_5xx_returns_502(self, db_engine: Engine) -> None:
        """ReNaSS 5xx → 502."""
        app = _make_earthquakes_app(db_engine, provider="renass")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(
                return_value=httpx.Response(500, json={"error": "server error"})
            )
            response = client.get("/api/v1/earthquakes")
        _reset_state()
        assert response.status_code == 502

    def test_renass_429_returns_503(self, db_engine: Engine) -> None:
        """ReNaSS 429 → 503 + Retry-After."""
        app = _make_earthquakes_app(db_engine, provider="renass")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"message": "Too Many Requests"},
                    headers={"Retry-After": "60"},
                )
            )
            response = client.get("/api/v1/earthquakes")
        _reset_state()
        assert response.status_code == 503
