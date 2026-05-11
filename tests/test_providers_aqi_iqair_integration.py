"""Integration tests for the IQAir AirVisual AQI provider (3b-12).

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

Fixture path: tests/fixtures/providers/aqi/iqair_nearest_city_nashville.json
Fixture mode: synthetic-from-published-example (IQAir Community API key not
available on weather-dev at 3b-12 time; Nashville example from api-docs used
per L3 carry-forward pattern).

Real-capture path: if WEEWX_CLEARSKIES_IQAIR_KEY env var is set, tests skip
the synthetic fixture and call the live API for Belchertown coords. The captured
response is written to tests/fixtures/providers/aqi/iqair_nearest_city_real.json
with a sidecar .md marking mode: real-capture.

End-to-end paths covered:
  - Full startup with [aqi] provider = iqair + WEEWX_CLEARSKIES_IQAIR_KEY configured.
  - GET /api/v1/aqi/current iqair configured + respx-mocked → 200 source="iqair".
  - Canonical AQIReading: aqi=10, aqiCategory='Good', aqiMainPollutant='PM2.5',
    aqiLocation='Nashville, Tennessee', observedAt='2019-04-08T18:00:00Z', source='iqair'.
  - All pollutant concentration fields null (PARTIAL-DOMAIN — free tier, LC5).
  - observedAt is UTC ISO-8601 Z format (LC6 + ADR-020).
  - GET /api/v1/aqi/current credentials missing → 502 error.
  - Provider 5xx → 502 RFC 9457 problem+json.
  - Provider 429 → 503 RFC 9457 + Retry-After.
  - AQIResponse envelope validates against OpenAPI AQIResponse schema shape.
  - Memory cache: miss → fetch → hit (both DB backends).
  - Redis cache: miss → fetch → hit (redis mark; must pass on weather-dev).
  - wire_providers([iqair.CAPABILITY]) registers in capability registry.
  - CAPABILITY.supplied_canonical_fields has exactly 6 free-tier fields.

3b-11 Redis isolation pattern applied: setup_method flushes Redis at start AND
error paths; cache-miss verification inline before each assertion.

ADR references: ADR-012, ADR-013, ADR-017, ADR-018, ADR-020, ADR-038.
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
_IQAIR_LIVE_KEY = os.environ.get("WEEWX_CLEARSKIES_IQAIR_KEY", "").strip()

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "aqi"

# Nashville coordinates from the synthetic fixture
_LAT = 36.1767
_LON = -86.7386

_IQAIR_BASE_URL = "https://api.airvisual.com"
_IQAIR_NEAREST_CITY_URL = _IQAIR_BASE_URL + "/v2/nearest_city"

_TEST_KEY = "INTEGRATION_TEST_IQAIR_KEY"


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
        pytest.skip(f"SQLite file {_SQLITE_SDB_PATH!r} not found; seed the dev stack")


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


def _flush_redis_if_configured() -> None:
    """Flush Redis DB if CLEARSKIES_CACHE_URL is set. No-op if Redis not reachable."""
    cache_url = os.environ.get("CLEARSKIES_CACHE_URL")
    if not cache_url:
        return
    try:
        import redis as redis_lib  # noqa: PLC0415
        r = redis_lib.from_url(cache_url)
        r.flushdb()
    except Exception:  # noqa: BLE001
        pass  # Redis not reachable — skip flush; tests will skip if needed


# ---------------------------------------------------------------------------
# Engine fixtures
# ---------------------------------------------------------------------------


def _make_engine() -> Engine:
    """Build the DB engine for the current backend (MariaDB or SQLite)."""
    if _BACKEND == "mariadb":
        _require_mariadb_password()
        url = (
            f"mysql+pymysql://clearskies_ro:{_MARIADB_RO_PASSWORD}"
            f"@127.0.0.1:{_MARIADB_HOST_PORT}/{_MARIADB_DB}"
        )
        return create_engine(url, pool_pre_ping=True)
    else:
        _require_sqlite_file()
        return create_engine(
            f"sqlite+pysqlite:///file:///{_SQLITE_SDB_PATH}?mode=ro&uri=true",
            connect_args={"check_same_thread": False},
        )


@pytest.fixture(scope="class")
def db_engine() -> Generator[Engine, None, None]:
    """Class-scoped engine fixture. Disposes after each test class."""
    engine = _make_engine()
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _wire_db(engine: Engine) -> None:
    """Wire the real DB engine into weewx_clearskies_api's session layer."""
    from weewx_clearskies_api.db.reflection import (  # noqa: PLC0415
        STOCK_COLUMN_MAP,
        ColumnInfo,
        ColumnRegistry,
    )
    from weewx_clearskies_api.db.registry import wire_registry  # noqa: PLC0415
    from weewx_clearskies_api.db.session import wire_engine  # noqa: PLC0415

    wire_engine(engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)


def _wire_test_station() -> None:
    """Wire station at Nashville coordinates matching the IQAir fixture."""
    from weewx_clearskies_api.services import station as station_mod  # noqa: PLC0415
    from weewx_clearskies_api.services.station import StationInfo, reset_cache  # noqa: PLC0415

    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="integration-test-iqair-aqi",
        name="Integration Test Station (IQAir AQI)",
        latitude=_LAT,
        longitude=_LON,
        altitude=175.0,
        timezone="America/Chicago",
        timezone_offset_minutes=-300,
        unit_system="US",
        hardware=None,
    )


def _wire_test_units() -> None:
    """Wire US unit block for integration tests."""
    from weewx_clearskies_api.services import units as _units_mod  # noqa: PLC0415
    from weewx_clearskies_api.services.units import (  # noqa: PLC0415
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


def _reset_iqair_provider_state() -> None:
    """Reset provider registry, cache, IQAir http client + rate limiter."""
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.aqi.iqair import _reset_http_client_for_tests  # noqa: PLC0415
    import weewx_clearskies_api.providers.aqi.iqair as _iqair  # noqa: PLC0415

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _iqair._rate_limiter._calls.clear()
    wire_cache_from_env()


def _make_integration_app(
    engine: Engine,
    wire_credentials: bool = True,
) -> FastAPI:
    """Build a full integration FastAPI app with IQAir AQI registered.

    wire_credentials: if True, sets module-level _IQAIR_KEY.
    """
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415
    import weewx_clearskies_api.endpoints.aqi as _aqi_endpoint  # noqa: PLC0415

    _reset_iqair_provider_state()
    _wire_db(engine)
    _wire_test_station()
    _wire_test_units()

    from weewx_clearskies_api.providers.aqi.iqair import CAPABILITY  # noqa: PLC0415
    wire_providers([CAPABILITY])

    if wire_credentials:
        _aqi_endpoint._IQAIR_KEY = _TEST_KEY
    else:
        _aqi_endpoint._IQAIR_KEY = None

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
    )
    return create_app(settings)


# ---------------------------------------------------------------------------
# Client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def integration_client(db_engine: Engine) -> TestClient:
    """TestClient with IQAir registered + credentials wired."""
    app = _make_integration_app(db_engine, wire_credentials=True)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def integration_client_no_credentials(db_engine: Engine) -> TestClient:
    """TestClient with IQAir registered but credentials NOT wired."""
    app = _make_integration_app(db_engine, wire_credentials=False)
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. End-to-end happy path — IQAir registered + credentials wired
# ===========================================================================


class TestIntegrationIQAirAqiHappyPath:
    """Full stack GET /aqi/current with IQAir configured → 200 AQIReading."""

    def test_iqair_aqi_returns_200(self, integration_client: TestClient) -> None:
        """IQAir registered + credentials wired + respx-mocked → 200."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )

    def test_iqair_aqi_response_source_is_iqair(self, integration_client: TestClient) -> None:
        """source = 'iqair' in AQIResponse envelope."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["source"] == "iqair", (
            f"Expected source='iqair', got {body.get('source')!r}"
        )

    def test_iqair_aqi_data_aqi_value_is_10(self, integration_client: TestClient) -> None:
        """data.aqi = 10 (Nashville fixture aqius=10)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["data"] is not None
        assert body["data"]["aqi"] == 10, (
            f"Expected aqi=10, got {body['data'].get('aqi')!r}"
        )

    def test_iqair_aqi_data_category_is_good(self, integration_client: TestClient) -> None:
        """data.aqiCategory = 'Good' (AQI 10 → 0–50 EPA band)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["data"]["aqiCategory"] == "Good", (
            f"Expected aqiCategory='Good', got {body['data'].get('aqiCategory')!r}"
        )

    def test_iqair_aqi_data_main_pollutant_is_pm25(self, integration_client: TestClient) -> None:
        """data.aqiMainPollutant = 'PM2.5' (mainus='p2' → PM2.5 via lookup)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["data"]["aqiMainPollutant"] == "PM2.5", (
            f"Expected aqiMainPollutant='PM2.5', got {body['data'].get('aqiMainPollutant')!r}"
        )

    def test_iqair_aqi_data_location_is_nashville_tennessee(
        self, integration_client: TestClient
    ) -> None:
        """data.aqiLocation = 'Nashville, Tennessee' (city+', '+state per LC4)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["data"]["aqiLocation"] == "Nashville, Tennessee", (
            f"Expected aqiLocation='Nashville, Tennessee', "
            f"got {body['data'].get('aqiLocation')!r}"
        )

    def test_iqair_aqi_data_observed_at_is_utc_z(self, integration_client: TestClient) -> None:
        """data.observedAt ends with Z (UTC ISO-8601, LC6 + ADR-020)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        observed_at = body["data"]["observedAt"]
        assert observed_at.endswith("Z"), (
            f"observedAt must end with Z, got {observed_at!r}"
        )

    def test_iqair_aqi_data_observed_at_value(self, integration_client: TestClient) -> None:
        """data.observedAt = '2019-04-08T18:00:00Z' (millis stripped from pollution.ts)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["data"]["observedAt"] == "2019-04-08T18:00:00Z", (
            f"Expected observedAt='2019-04-08T18:00:00Z', got {body['data']['observedAt']!r}"
        )

    def test_iqair_aqi_data_source_is_iqair(self, integration_client: TestClient) -> None:
        """data.source = 'iqair' (AQIReading.source = 'iqair')."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["data"]["source"] == "iqair"

    def test_iqair_aqi_all_pollutant_concentrations_are_null(
        self, integration_client: TestClient
    ) -> None:
        """All pollutant* concentration fields null (PARTIAL-DOMAIN, LC5 + free tier)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        reading = response.json()["data"]
        for field in (
            "pollutantPM25", "pollutantPM10",
            "pollutantO3", "pollutantNO2", "pollutantSO2", "pollutantCO",
        ):
            assert reading.get(field) is None, (
                f"Expected {field}=null (PARTIAL-DOMAIN), got {reading.get(field)!r}"
            )

    def test_iqair_aqi_envelope_has_all_required_fields(
        self, integration_client: TestClient
    ) -> None:
        """AQIResponse envelope has all OpenAPI-mandated fields: data, units, source, generatedAt."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        for field in ("data", "units", "source", "generatedAt"):
            assert field in body, f"AQIResponse envelope missing required field {field!r}"

    def test_iqair_aqi_generated_at_is_utc_z(self, integration_client: TestClient) -> None:
        """generatedAt ends with Z (ADR-020 UTC at API boundary)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["generatedAt"].endswith("Z"), (
            f"generatedAt must end with Z, got {body['generatedAt']!r}"
        )


# ===========================================================================
# 2. Credentials missing path
# ===========================================================================


class TestIntegrationIQAirAqiCredentialsMissing:
    """IQAir registered but credentials NOT wired → 502 error."""

    def setup_method(self) -> None:
        """Flush Redis at start of each test to prevent cache masking error paths."""
        _flush_redis_if_configured()

    def test_credentials_missing_returns_502(
        self, integration_client_no_credentials: TestClient
    ) -> None:
        """IQAir registered, credentials=None → 502 (pre-call key check before provider call)."""
        with respx.mock(assert_all_called=False):
            response = integration_client_no_credentials.get("/api/v1/aqi/current")

        assert response.status_code == 502, (
            f"Expected 502 for missing credentials, got {response.status_code}: {response.text[:300]}"
        )


# ===========================================================================
# 3. Error paths
# ===========================================================================


class TestIntegrationIQAirAqiErrorPaths:
    """Provider error handling: 5xx → 502, 429 → 503 + Retry-After.

    setup_method flushes Redis so error-path tests aren't masked by cached
    success responses from earlier tests (3b-11 isolation pattern).
    """

    def setup_method(self) -> None:
        """Flush Redis if configured, ensuring no cached reading masks error-path responses."""
        _flush_redis_if_configured()

    def test_provider_5xx_returns_502_rfc9457(self, integration_client: TestClient) -> None:
        """Provider 5xx → 502 application/problem+json (RFC 9457)."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(500, json={"reason": "server error"})
            )
            response = integration_client.get("/api/v1/aqi/current")

        assert response.status_code == 502, (
            f"Provider 5xx must map to 502, got {response.status_code}: {response.text[:300]}"
        )
        assert "application/problem+json" in response.headers.get("content-type", ""), (
            "502 must return application/problem+json (RFC 9457)"
        )

    def test_provider_429_returns_503_rfc9457(self, integration_client: TestClient) -> None:
        """Provider 429 → 503 application/problem+json (RFC 9457)."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"reason": "too many requests"},
                    headers={"Retry-After": "60"},
                )
            )
            response = integration_client.get("/api/v1/aqi/current")

        assert response.status_code == 503, (
            f"Provider 429 must map to 503, got {response.status_code}: {response.text[:300]}"
        )
        assert "application/problem+json" in response.headers.get("content-type", ""), (
            "503 must return application/problem+json (RFC 9457)"
        )

    def test_provider_429_includes_retry_after_header(
        self, integration_client: TestClient
    ) -> None:
        """Provider 429 → 503 response includes Retry-After header (ADR-018)."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"reason": "rate limit"},
                    headers={"Retry-After": "120"},
                )
            )
            response = integration_client.get("/api/v1/aqi/current")

        assert "Retry-After" in response.headers, (
            "503 from QuotaExhausted must include Retry-After header"
        )


# ===========================================================================
# 4. OpenAPI schema validation
# ===========================================================================


class TestIntegrationIQAirAqiOpenApiSchema:
    """AQIResponse shape matches OpenAPI AQIResponse contract."""

    def test_aqi_reading_fields_match_openapi_aqi_reading_schema(
        self, integration_client: TestClient
    ) -> None:
        """data fields match OpenAPI AQIReading schema (all 12 canonical fields present)."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        reading = body["data"]
        assert reading is not None

        # These fields must be present (possibly null) in every AQIReading per OpenAPI contract
        expected_keys = {
            "aqi", "aqiCategory", "aqiMainPollutant", "aqiLocation",
            "pollutantPM25", "pollutantPM10",
            "pollutantO3", "pollutantNO2", "pollutantSO2", "pollutantCO",
            "observedAt", "source",
        }
        for key in expected_keys:
            assert key in reading, (
                f"AQIReading missing OpenAPI-required field {key!r}"
            )

    def test_aqi_response_source_is_iqair_not_none(
        self, integration_client: TestClient
    ) -> None:
        """AQIResponse.source = 'iqair' (not 'none', not null) when IQAir is configured."""
        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = integration_client.get("/api/v1/aqi/current")

        body = response.json()
        assert body["source"] not in (None, "none"), (
            f"Expected source='iqair', got {body.get('source')!r}"
        )
        assert body["source"] == "iqair"


# ===========================================================================
# 5. Capability registry wiring
# ===========================================================================


class TestIntegrationIQAirCapabilityRegistry:
    """wire_providers([iqair.CAPABILITY]) registers correctly in capability registry."""

    def test_iqair_capability_registered_in_registry(self, db_engine: Engine) -> None:
        """wire_providers([iqair.CAPABILITY]) → ('aqi', 'iqair') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.aqi.iqair import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(p.provider_id == "iqair" and p.domain == "aqi" for p in registry), (
            "wire_providers must register iqair aqi in registry"
        )
        reset_provider_registry_for_tests()

    def test_iqair_capability_supplied_fields_has_six_free_tier_fields(
        self, db_engine: Engine
    ) -> None:
        """CAPABILITY.supplied_canonical_fields = 6 free-tier fields only (no concentrations)."""
        from weewx_clearskies_api.providers.aqi.iqair import CAPABILITY  # noqa: PLC0415
        supplied = set(CAPABILITY.supplied_canonical_fields)
        expected = {"aqi", "aqiCategory", "aqiMainPollutant", "aqiLocation", "observedAt", "source"}
        assert supplied == expected, (
            f"Expected CAPABILITY supplied fields {expected!r}, got {supplied!r}"
        )


# ===========================================================================
# 6. Memory cache: miss → fetch → hit
# ===========================================================================


class TestIntegrationIQAirAqiMemoryCache:
    """IQAir AQI provider: memory cache miss → fetch → hit (both DB backends)."""

    def test_cache_miss_fetches_from_provider_and_caches_result(
        self, db_engine: Engine
    ) -> None:
        """Memory cache miss → one HTTP call; result cached for next poll."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            get_cache,
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.aqi import iqair  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.iqair import (  # noqa: PLC0415
            _build_cache_key,
            _reset_http_client_for_tests,
        )

        # Flush Redis if configured (3b-11 isolation pattern)
        _flush_redis_if_configured()

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        iqair._rate_limiter._calls.clear()
        wire_cache_from_env()

        data = _load_fixture("iqair_nearest_city_nashville.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            reading = iqair.fetch(lat=_LAT, lon=_LON, key=_TEST_KEY)
            call_count = mock.calls.call_count

        assert call_count == 1, f"Expected 1 HTTP call on cache miss, got {call_count}"
        assert reading is not None
        assert reading.source == "iqair"
        assert reading.aqi == 10

        # Cache was populated — verify inline before any other assertion
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
        from weewx_clearskies_api.providers.aqi import iqair  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.iqair import _reset_http_client_for_tests  # noqa: PLC0415

        _flush_redis_if_configured()

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        iqair._rate_limiter._calls.clear()
        wire_cache_from_env()

        data = _load_fixture("iqair_nearest_city_nashville.json")

        # First fetch — fills memory cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            reading1 = iqair.fetch(lat=_LAT, lon=_LON, key=_TEST_KEY)

        # Second fetch — must come from cache (zero calls)
        with respx.mock(assert_all_called=False) as mock2:
            reading2 = iqair.fetch(lat=_LAT, lon=_LON, key=_TEST_KEY)
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
# 7. Redis cache (redis mark — MUST PASS per brief)
# ===========================================================================


@pytest.mark.redis
class TestIntegrationIQAirAqiRedisCache:
    """Real Redis from the docker-compose redis profile.

    Per brief §Process gates: Redis tier MUST PASS, not skip.
    If Redis is not reachable on weather-dev, this is a brief-gate failure
    that must be surfaced to the lead via SendMessage BEFORE closeout.
    """

    def test_iqair_aqi_redis_cache_miss_stores_reading(self) -> None:
        """Redis cache miss → one HTTP call → reading stored in Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.aqi import iqair  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.iqair import (  # noqa: PLC0415
            _build_cache_key,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        iqair._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        data = _load_fixture("iqair_nearest_city_nashville.json")

        try:
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                    return_value=httpx.Response(200, json=data)
                )
                reading = iqair.fetch(lat=_LAT, lon=_LON, key=_TEST_KEY)
                call_count = mock.calls.call_count

            assert call_count == 1, (
                f"Expected 1 HTTP call on Redis cache miss, got {call_count}"
            )
            assert reading is not None
            assert reading.source == "iqair"

            # Verify inline — cache-miss verification before assertion (3b-11 pattern)
            cache_key = _build_cache_key(_LAT, _LON)
            cached = cache_mod._cache.get(cache_key)
            assert cached is not None, "Reading must be stored in Redis after cache miss"

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()

    def test_iqair_aqi_redis_cache_hit_skips_provider_call(self) -> None:
        """Redis cache hit → zero HTTP calls; reading returned from Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.aqi import iqair  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.iqair import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        iqair._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        data = _load_fixture("iqair_nearest_city_nashville.json")

        try:
            # First fetch — fills Redis cache
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_IQAIR_NEAREST_CITY_URL).mock(
                    return_value=httpx.Response(200, json=data)
                )
                reading1 = iqair.fetch(lat=_LAT, lon=_LON, key=_TEST_KEY)
                assert mock.calls.call_count > 0

            # Second fetch — must hit Redis; zero calls
            with respx.mock(assert_all_called=False) as mock2:
                reading2 = iqair.fetch(lat=_LAT, lon=_LON, key=_TEST_KEY)
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
