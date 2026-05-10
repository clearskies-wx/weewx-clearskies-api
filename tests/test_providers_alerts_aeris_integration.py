"""Integration tests for the Aeris alerts provider (3b round 7).

All tests carry @pytest.mark.integration and run against the docker-compose
dev/test stack (MariaDB or SQLite backend per BACKEND env var).

Same integration suite runs twice per ADR-012 (once MariaDB, once SQLite)
to catch dialect drift. The alerts endpoint itself has no DB dependency, but
running in the real stack confirms the endpoint is DB-stack-agnostic and
wires correctly alongside the DB-backed endpoints.

Redis-backed integration tests carry both @pytest.mark.integration and
@pytest.mark.redis and are skipped unless `pytest -m "integration and redis"`.
Per brief §Process gates: the Redis tier MUST PASS, not skip. If Redis is
not reachable on weather-dev, this is a brief-gate failure that must be
surfaced to the lead before closeout.

End-to-end paths covered:
  - GET /api/v1/alerts  aeris configured + respx-mocked → 200 source="aeris"
  - Severity filter end-to-end: ?severity=warning → warning-only records
  - Empty response[]: → 200 alerts=[] source="aeris"
  - Credentials missing → 502 ProviderProblem
  - Unknown query param → 400/422 (extra="forbid" via Depends pattern)
  - dispatch table: ('alerts', 'aeris') in PROVIDER_MODULES
  - startup wiring: AlertsSettings with provider='aeris' passes validate()
  - memory cache: miss → fetch → hit (both backends)
  - Redis cache: miss → fetch → hit (redis mark; must pass on weather-dev)

Wire-shape notes:
  - The "valid" Aeris fixture used in happy-path integration tests OMITS
    the `emergency` field for clean test isolation. The real fixture
    (`alerts.json`) has `emergency=false` (boolean) and is safe to load
    via the post-2026-05-09-amendment `_AerisAlertDetails` model
    (`bool | str | None`). Boolean-emergency wire-shape coverage lives in
    the unit suite under `TestAerisWireShapePydantic`; integration
    fixtures are stripped only to make assertions on senderName etc.
    independent of that wire-shape behaviour.

ADR references: ADR-006, ADR-012, ADR-016, ADR-017, ADR-038.
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

# Station coordinates for integration tests — Seattle area
_LAT = 47.6062
_LON = -122.3321
_LOCATION = f"{round(_LAT, 4)},{round(_LON, 4)}"
_AERIS_ALERTS_URL = f"https://data.api.xweather.com/alerts/{_LOCATION}"

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
    alerts_provider: str | None = None,
    aeris_client_id: str | None = None,
    aeris_client_secret: str | None = None,
) -> tuple[Any, FastAPI]:
    """Wire the full integration stack for a test app with Aeris alerts.

    Returns (settings, app) with DB, station, units, cache, providers all wired.
    Handles both MariaDB and SQLite backends identically.
    """
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        AlertsSettings,
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP, ColumnInfo, ColumnRegistry  # noqa: PLC0415
    from weewx_clearskies_api.db.registry import wire_registry  # noqa: PLC0415
    from weewx_clearskies_api.db.session import wire_engine  # noqa: PLC0415
    from weewx_clearskies_api.endpoints.alerts import (  # noqa: PLC0415
        wire_alerts_settings,
        wire_aeris_credentials,
    )
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
    from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415
    import weewx_clearskies_api.providers.alerts.aeris as _aeris  # noqa: PLC0415

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

    # Wire station — Seattle coordinates match the Aeris test URLs.
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

    # Build capability list for alerts
    capabilities: list[ProviderCapability] = []
    if alerts_provider == "aeris":
        capabilities.append(_aeris.CAPABILITY)

    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        alerts=AlertsSettings({"provider": alerts_provider} if alerts_provider else {}),
    )

    # Wire settings first (reads NWS contact from settings.alerts), then
    # override Aeris credentials directly. Must come AFTER wire_alerts_settings
    # because wire_alerts_settings() calls wire_aeris_credentials() internally
    # with the env-var-loaded values (None in tests). The explicit call below
    # overwrites with test-supplied credentials. Original order (credentials
    # before settings) silently zeroed out the credentials on every test.
    wire_alerts_settings(settings)
    wire_aeris_credentials(aeris_client_id, aeris_client_secret)

    app = create_app(settings)
    return settings, app


def _make_valid_aeris_alerts_fixture() -> dict[str, Any]:
    """Build a valid Aeris alerts envelope with one watch + one advisory alert.

    Severity dispatch (canonical-data-model §4.3 amended 2026-05-09):
      - First alert `TO.A` → suffix `A` → 'watch' (Tornado Watch).
      - Second alert `WI.Y` → suffix `Y` → 'advisory' (Wind Advisory).
    """
    return {
        "success": True,
        "error": None,
        "response": [
            {
                "id": "integration-test-alert-001",
                "dataSource": "noaa_nws",
                "active": True,
                "details": {
                    "type": "TO.A",  # VTEC suffix .A → watch
                    "name": "TORNADO WATCH",
                    "loc": "WAZ001",
                    "priority": 2,  # NOAA hazard-map display priority (NOT severity)
                    "color": "FFFF00",
                    "body": "A Tornado Watch is in effect for portions of western Washington.",
                },
                "timestamps": {
                    "issued": 1778400000,
                    "issuedISO": "2026-05-09T12:00:00-07:00",
                    "begins": 1778400000,
                    "beginsISO": "2026-05-09T12:00:00-07:00",
                    "expires": 1778436000,
                    "expiresISO": "2026-05-09T22:00:00-07:00",
                },
                "place": {"name": "king", "state": "wa", "country": "us"},
            },
            {
                "id": "integration-test-alert-002",
                "dataSource": "noaa_nws",
                "active": True,
                "details": {
                    "type": "WI.Y",  # VTEC suffix .Y → advisory
                    "name": "WIND ADVISORY",
                    "loc": "WAZ002",
                    "priority": 4,
                    "color": "AAAA00",
                    "body": "Wind advisory in effect today.",
                },
                "timestamps": {
                    "issued": 1778400000,
                    "issuedISO": "2026-05-09T12:00:00-07:00",
                    "begins": 1778400000,
                    "beginsISO": "2026-05-09T12:00:00-07:00",
                    "expires": 1778436000,
                    "expiresISO": "2026-05-09T22:00:00-07:00",
                },
                "place": {"name": "snohomish", "state": "wa", "country": "us"},
            },
        ],
    }


def _make_warning_alert_fixture() -> dict[str, Any]:
    """Build a fixture with a single warning-severity alert (TO.W → 'warning')."""
    return {
        "success": True,
        "error": None,
        "response": [
            {
                "id": "integration-warning-alert-001",
                "dataSource": "noaa_nws",
                "active": True,
                "details": {
                    "type": "TO.W",  # VTEC suffix .W → warning
                    "name": "TORNADO WARNING",
                    "loc": "WAZ001",
                    "priority": 1,
                    "color": "FF0000",
                    "body": "A Tornado Warning is in effect for King County.",
                },
                "timestamps": {
                    "issued": 1778400000,
                    "issuedISO": "2026-05-09T12:00:00-07:00",
                    "expires": 1778436000,
                    "expiresISO": "2026-05-09T22:00:00-07:00",
                },
                "place": {"name": "king", "state": "wa", "country": "us"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Integration app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_app_aeris(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with Aeris alerts provider configured + test credentials."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(
        db_engine,
        alerts_provider="aeris",
        aeris_client_id=_TEST_CLIENT_ID,
        aeris_client_secret=_TEST_CLIENT_SECRET,
    )
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_app_aeris_no_credentials(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with Aeris provider but no credentials (tests 502 path)."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415

    _, app = _wire_integration_stack(
        db_engine,
        alerts_provider="aeris",
        aeris_client_id=None,  # Credentials missing
        aeris_client_secret=None,
    )
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_aeris(integration_app_aeris: FastAPI) -> TestClient:
    """TestClient for the Aeris alerts integration app."""
    return TestClient(integration_app_aeris, raise_server_exceptions=False)


@pytest.fixture
def integration_client_aeris_no_credentials(
    integration_app_aeris_no_credentials: FastAPI,
) -> TestClient:
    """TestClient for the Aeris alerts integration app with no credentials."""
    return TestClient(integration_app_aeris_no_credentials, raise_server_exceptions=False)


# ===========================================================================
# Integration: dispatch table
# ===========================================================================


class TestIntegrationAerisAlertsDispatchTable:
    """('alerts', 'aeris') is in the dispatch table."""

    def test_aeris_is_in_alerts_dispatch_table(self) -> None:
        """get_provider_module(domain='alerts', provider_id='aeris') returns aeris module."""
        from weewx_clearskies_api.providers._common.dispatch import get_provider_module  # noqa: PLC0415
        module = get_provider_module(domain="alerts", provider_id="aeris")
        assert module is not None
        assert hasattr(module, "CAPABILITY")
        assert hasattr(module, "fetch")
        assert module.CAPABILITY.provider_id == "aeris"
        assert module.CAPABILITY.domain == "alerts"


# ===========================================================================
# Integration: startup wiring
# ===========================================================================


class TestIntegrationAerisAlertsStartupWiring:
    """AlertsSettings with aeris provider wires and validates correctly."""

    def test_alerts_settings_with_aeris_passes_validate(self) -> None:
        """AlertsSettings({'provider': 'aeris'}).validate() does not raise."""
        from weewx_clearskies_api.config.settings import AlertsSettings  # noqa: PLC0415
        settings = AlertsSettings({"provider": "aeris"})
        settings.validate()  # Should not raise

    def test_wire_aeris_credentials_stores_test_values(self) -> None:
        """wire_aeris_credentials() stores the values accessible by the endpoint."""
        from weewx_clearskies_api.endpoints.alerts import (  # noqa: PLC0415
            wire_aeris_credentials,
            _aeris_client_id,
            _aeris_client_secret,
        )
        wire_aeris_credentials("TEST_ID", "TEST_SECRET")
        import weewx_clearskies_api.endpoints.alerts as alerts_mod  # noqa: PLC0415
        assert alerts_mod._aeris_client_id == "TEST_ID"
        assert alerts_mod._aeris_client_secret == "TEST_SECRET"
        # Restore None
        wire_aeris_credentials(None, None)

    def test_aeris_alerts_capability_wires_into_provider_registry(
        self, db_engine: Engine
    ) -> None:
        """wire_providers([aeris.CAPABILITY]) → registry has aeris alerts entry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])

        registry = get_provider_registry()
        aeris_entries = [p for p in registry if p.provider_id == "aeris" and p.domain == "alerts"]
        assert len(aeris_entries) == 1, (
            f"Expected 1 aeris alerts entry in registry, found {len(aeris_entries)}"
        )
        reset_provider_registry_for_tests()


# ===========================================================================
# Integration: end-to-end /alerts endpoint
# ===========================================================================


class TestIntegrationAerisAlertsEndpoint:
    """End-to-end GET /api/v1/alerts with Aeris provider via respx-mocked HTTP."""

    def test_aeris_alerts_returns_200_with_alerts_list_and_aeris_source(
        self, integration_client_aeris: TestClient
    ) -> None:
        """Aeris configured + respx-mocked valid response → 200 source='aeris'."""
        alerts_data = _make_valid_aeris_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_aeris.get("/api/v1/alerts")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )
        body = response.json()
        assert body["source"] == "aeris", (
            f"Expected source='aeris', got {body.get('source')!r}"
        )
        data = body["data"]
        assert data["source"] == "aeris"
        assert len(data["alerts"]) == 2, (
            f"Expected 2 alerts, got {len(data['alerts'])}"
        )

    def test_aeris_alerts_records_have_correct_canonical_shape(
        self, integration_client_aeris: TestClient
    ) -> None:
        """Each canonical AlertRecord has required fields: id, headline, severity, event, source."""
        alerts_data = _make_valid_aeris_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_aeris.get("/api/v1/alerts")

        body = response.json()
        alerts = body["data"]["alerts"]
        for alert in alerts:
            assert "id" in alert
            assert "headline" in alert
            assert "severity" in alert
            assert alert["severity"] in ("advisory", "watch", "warning"), (
                f"severity must be canonical enum, got {alert['severity']!r}"
            )
            assert "event" in alert
            assert alert["source"] == "aeris"

    def test_aeris_alerts_empty_response_returns_200_empty_list(
        self, integration_client_aeris: TestClient
    ) -> None:
        """Empty Aeris response[] → 200 + alerts=[] + source='aeris'."""
        empty_data = {"success": True, "error": None, "response": []}

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=empty_data)
            )
            response = integration_client_aeris.get("/api/v1/alerts")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["alerts"] == [], (
            f"Expected empty alerts list, got {body['data']['alerts']!r}"
        )
        assert body["data"]["source"] == "aeris"

    def test_aeris_alerts_severity_filter_warning_returns_warning_only(
        self, db_engine: Engine
    ) -> None:
        """?severity=warning returns only warning records (TO.W → 'warning').

        Uses a fresh app (not shared fixture) to control cache state.
        Severity dispatch via details.type VTEC suffix per canonical §4.3 amended 2026-05-09.
        """
        from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.capability import reset_provider_registry_for_tests  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

        # Mix of warning (TO.W → suffix .W) and advisory (WI.Y → suffix .Y) alerts.
        # Severity is now derived from details.type VTEC suffix (canonical §4.3 amended 2026-05-09).
        mixed_data = {
            "success": True,
            "error": None,
            "response": [
                {
                    "id": "warning-alert-001",
                    "dataSource": "noaa_nws",
                    "active": True,
                    "details": {
                        "type": "TO.W",  # VTEC suffix .W → warning
                        "name": "TORNADO WARNING",
                        "priority": 1,
                        "body": "Tornado warning in effect.",
                    },
                    "timestamps": {
                        "issuedISO": "2026-05-09T12:00:00-07:00",
                        "expiresISO": "2026-05-09T14:00:00-07:00",
                    },
                    "place": {"name": "king", "state": "wa", "country": "us"},
                },
                {
                    "id": "advisory-alert-001",
                    "dataSource": "noaa_nws",
                    "active": True,
                    "details": {
                        "type": "WI.Y",  # VTEC suffix .Y → advisory
                        "name": "WIND ADVISORY",
                        "priority": 4,
                        "body": "Wind advisory in effect.",
                    },
                    "timestamps": {
                        "issuedISO": "2026-05-09T12:00:00-07:00",
                        "expiresISO": "2026-05-09T22:00:00-07:00",
                    },
                    "place": {"name": "snohomish", "state": "wa", "country": "us"},
                },
            ],
        }

        _, app = _wire_integration_stack(
            db_engine,
            alerts_provider="aeris",
            aeris_client_id=_TEST_CLIENT_ID,
            aeris_client_secret=_TEST_CLIENT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=mixed_data)
            )
            response = client.get("/api/v1/alerts?severity=warning")

        assert response.status_code == 200
        body = response.json()
        alerts = body["data"]["alerts"]
        assert len(alerts) == 1, (
            f"severity=warning should return only warning alerts, got {len(alerts)}"
        )
        assert alerts[0]["severity"] == "warning"
        assert alerts[0]["id"] == "warning-alert-001"

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

    def test_aeris_alerts_credentials_missing_returns_502(
        self, integration_client_aeris_no_credentials: TestClient
    ) -> None:
        """Credentials missing (None) → 502 ProviderProblem (KeyInvalid → endpoint 502)."""
        with respx.mock(assert_all_called=False):
            response = integration_client_aeris_no_credentials.get("/api/v1/alerts")

        assert response.status_code == 502, (
            f"Expected 502 for missing Aeris credentials, got {response.status_code}: "
            f"{response.text[:300]}"
        )

    def test_aeris_alerts_unknown_query_param_returns_400_or_422(
        self, integration_client_aeris: TestClient
    ) -> None:
        """Unknown query parameter rejected by extra='forbid' via Depends pattern."""
        with respx.mock(assert_all_called=False):
            response = integration_client_aeris.get(
                "/api/v1/alerts?severity=advisory&unknown_param=bad"
            )

        assert response.status_code in (400, 422), (
            f"Expected 400 or 422 for unknown query param, got {response.status_code}"
        )

    def test_aeris_alerts_effective_field_is_utc_z_format(
        self, integration_client_aeris: TestClient
    ) -> None:
        """canonical effective field is UTC ISO-8601 with Z suffix (datetime conversion)."""
        alerts_data = _make_valid_aeris_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_aeris.get("/api/v1/alerts")

        body = response.json()
        alerts = body["data"]["alerts"]
        for alert in alerts:
            assert alert["effective"].endswith("Z"), (
                f"effective should be UTC Z format, got {alert['effective']!r}"
            )


# ===========================================================================
# Integration: memory cache end-to-end
# ===========================================================================


class TestIntegrationAerisAlertsMemoryCache:
    """Aeris alerts provider: memory cache miss → fetch → cache hit (both backends)."""

    def test_cache_miss_fetches_from_aeris_and_caches_result(
        self, db_engine: Engine
    ) -> None:
        """Memory cache miss → one Aeris HTTP call; result cached."""
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
            get_cache,
        )
        from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()
        wire_cache_from_env()

        alerts_data = _make_valid_aeris_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records = aeris.fetch(
                lat=_LAT,
                lon=_LON,
                client_id=_TEST_CLIENT_ID,
                client_secret=_TEST_CLIENT_SECRET,
            )
            call_count = mock.calls.call_count

        assert call_count == 1, f"Expected 1 Aeris call on cache miss, got {call_count}"
        assert len(records) == 2
        assert all(r.source == "aeris" for r in records)

        # Cache populated
        from weewx_clearskies_api.providers.alerts.aeris import _build_cache_key  # noqa: PLC0415
        cached = get_cache().get(_build_cache_key(_LAT, _LON))
        assert cached is not None

        reset_cache_for_tests()
        _reset_http_client_for_tests()

    def test_cache_hit_skips_aeris_call_and_returns_same_records(
        self, db_engine: Engine
    ) -> None:
        """Memory cache hit → zero Aeris HTTP calls; records match first fetch."""
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()
        wire_cache_from_env()

        alerts_data = _make_valid_aeris_alerts_fixture()

        # First fetch — fills memory cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records1 = aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )

        # Second fetch — should come from cache (zero calls)
        with respx.mock(assert_all_called=False) as mock2:
            records2 = aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )
            cache_hit_calls = mock2.calls.call_count

        assert cache_hit_calls == 0, (
            f"Expected 0 Aeris calls on cache hit, got {cache_hit_calls}"
        )
        assert len(records2) == len(records1)
        assert all(r.source == "aeris" for r in records2)

        reset_cache_for_tests()
        _reset_http_client_for_tests()


# ===========================================================================
# Integration: Redis cache (optional, redis mark — MUST PASS per brief)
# ===========================================================================


@pytest.mark.redis
class TestIntegrationAerisAlertsRedisCache:
    """Real Redis from the docker-compose redis profile.

    Per brief §Process gates: Redis tier MUST PASS, not skip.
    If Redis is not reachable on weather-dev, this is a brief-gate failure
    that must be surfaced to the lead via SendMessage BEFORE closeout.
    """

    def test_aeris_alerts_redis_cache_miss_stores_records(
        self, db_engine: Engine
    ) -> None:
        """Redis cache miss → one Aeris HTTP call → records stored in Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        alerts_data = _make_valid_aeris_alerts_fixture()

        try:
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_AERIS_ALERTS_URL).mock(
                    return_value=httpx.Response(200, json=alerts_data)
                )
                records = aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
                call_count = mock.calls.call_count

            assert call_count == 1, (
                f"Expected 1 Aeris call on Redis cache miss, got {call_count}"
            )
            assert len(records) == 2
            assert all(r.source == "aeris" for r in records)

            # Verify records in Redis
            from weewx_clearskies_api.providers.alerts.aeris import _build_cache_key  # noqa: PLC0415
            cache_key = _build_cache_key(_LAT, _LON)
            cached = cache_mod._cache.get(cache_key)
            assert cached is not None, "Records should be stored in Redis after cache miss"
            assert len(cached) == 2

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()

    def test_aeris_alerts_redis_cache_hit_skips_aeris_call(
        self, db_engine: Engine
    ) -> None:
        """Redis cache hit → zero Aeris HTTP calls; records returned from Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.aeris import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        aeris._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        alerts_data = _make_valid_aeris_alerts_fixture()

        try:
            # First fetch — fills Redis cache
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_AERIS_ALERTS_URL).mock(
                    return_value=httpx.Response(200, json=alerts_data)
                )
                records1 = aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
                assert mock.calls.call_count > 0

            # Second fetch — should hit Redis; zero calls
            with respx.mock(assert_all_called=False) as mock2:
                records2 = aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
                second_call_count = mock2.calls.call_count

            assert second_call_count == 0, (
                f"Expected 0 Aeris calls on Redis cache hit, got {second_call_count}"
            )
            assert len(records2) == len(records1)
            assert all(r.source == "aeris" for r in records2)

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()
