"""Integration tests for the OpenWeatherMap alerts provider (3b round 8).

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
  - GET /api/v1/alerts  openweathermap configured + respx-mocked → 200 source="openweathermap"
  - Canonical AlertRecord shape: id, headline, severity, event, source all present.
  - effective field is UTC ISO-8601 Z format.
  - Severity filter end-to-end: ?severity=warning → warning-only records.
  - Empty alerts[] → 200 alerts=[] source="openweathermap".
  - Basic-tier 401 (Q1=A) → 200 alerts=[] source="openweathermap" (NOT 502).
  - Credentials missing → 502 ProviderProblem.
  - Unknown query param → 400/422 (extra="forbid" via Depends pattern).
  - dispatch table: ('alerts', 'openweathermap') in PROVIDER_MODULES.
  - startup wiring: AlertsSettings with provider='openweathermap' passes validate().
  - wire_openweathermap_credentials() stores credentials accessible to endpoint.
  - memory cache: miss → fetch → hit (both backends).
  - Redis cache: miss → fetch → hit (redis mark; must pass on weather-dev).

ADR references: ADR-006, ADR-012, ADR-016, ADR-017, ADR-038.
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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "openweathermap"

# Station coordinates — Seattle area (same as NWS/Aeris fixtures)
_LAT = 47.6062
_LON = -122.3321
_OWM_ONECALL_URL = "https://api.openweathermap.org/data/3.0/onecall"

_TEST_APPID = "INTEGRATION_TEST_APPID_12345"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/openweathermap/."""
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
    owm_appid: str | None = None,
) -> tuple[Any, FastAPI]:
    """Wire the full integration stack for a test app with OWM alerts.

    Returns (settings, app) with DB, station, units, cache, providers all wired.
    Handles both MariaDB and SQLite backends identically.
    """
    import weewx_clearskies_api.providers.alerts.openweathermap as _owm_alerts  # noqa: PLC0415
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
    from weewx_clearskies_api.db.reflection import (  # noqa: PLC0415
        STOCK_COLUMN_MAP,
        ColumnInfo,
        ColumnRegistry,
    )
    from weewx_clearskies_api.db.registry import wire_registry  # noqa: PLC0415
    from weewx_clearskies_api.db.session import wire_engine  # noqa: PLC0415
    from weewx_clearskies_api.endpoints.alerts import (  # noqa: PLC0415
        wire_alerts_settings,
        wire_openweathermap_credentials,
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
    from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
        _reset_basic_tier_warned_for_tests,
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
    _reset_basic_tier_warned_for_tests()
    _owm_alerts._rate_limiter._calls.clear()

    # Wire DB
    wire_engine(engine)
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
    }
    wire_registry(registry)

    # Wire station — Seattle coordinates match the OWM test URL mocks.
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
    if alerts_provider == "openweathermap":
        capabilities.append(_owm_alerts.CAPABILITY)

    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        alerts=AlertsSettings({"provider": alerts_provider} if alerts_provider else {}),
    )

    # Wire settings (reads NWS and Aeris credentials from env — None in tests),
    # then override OWM credentials directly. Must come AFTER wire_alerts_settings
    # because wire_alerts_settings() calls wire_openweathermap_credentials() internally
    # with the env-var-loaded values (None in tests). The explicit call below
    # overwrites with test-supplied credentials.
    wire_alerts_settings(settings)
    wire_openweathermap_credentials(owm_appid)

    app = create_app(settings)
    return settings, app


def _make_valid_owm_alerts_fixture() -> dict[str, Any]:
    """Build a valid OWM One Call alerts response with two alerts.

    Entry 1: Wind Advisory → severity 'advisory'.
    Entry 2: Tornado Warning → severity 'warning'.
    Both severity paths covered in one fixture.
    """
    return {
        "lat": _LAT,
        "lon": _LON,
        "timezone": "America/Los_Angeles",
        "timezone_offset": -25200,
        "alerts": [
            {
                "sender_name": "NWS Seattle WA",
                "event": "Wind Advisory",
                "start": 1714485600,
                "end": 1714521600,
                "description": "Southerly winds 20 to 30 mph with gusts up to 45 mph.",
                "tags": ["Wind"],
            },
            {
                "sender_name": "NWS Portland OR",
                "event": "Tornado Warning",
                "start": 1714490000,
                "end": 1714497200,
                "description": "Tornado Warning issued. Rotation detected on radar.",
                "tags": ["Tornado"],
            },
        ],
    }


def _make_warning_only_fixture() -> dict[str, Any]:
    """Build a fixture with a single warning-severity alert for severity-filter tests."""
    return {
        "lat": _LAT,
        "lon": _LON,
        "timezone": "America/Los_Angeles",
        "timezone_offset": -25200,
        "alerts": [
            {
                "sender_name": "NWS Seattle WA",
                "event": "Tornado Warning",
                "start": 1714490000,
                "end": 1714497200,
                "description": "Tornado Warning issued.",
                "tags": ["Tornado"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Integration app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_app_owm(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with OWM alerts provider configured + test credentials."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (
        reset_provider_registry_for_tests,  # noqa: PLC0415
    )
    from weewx_clearskies_api.providers.alerts.openweathermap import (
        _reset_http_client_for_tests,  # noqa: PLC0415
    )

    _, app = _wire_integration_stack(
        db_engine,
        alerts_provider="openweathermap",
        owm_appid=_TEST_APPID,
    )
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_app_owm_no_credentials(db_engine: Engine) -> Generator[FastAPI, None, None]:
    """Integration app with OWM provider but no credentials (tests 502 path)."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (
        reset_provider_registry_for_tests,  # noqa: PLC0415
    )
    from weewx_clearskies_api.providers.alerts.openweathermap import (
        _reset_http_client_for_tests,  # noqa: PLC0415
    )

    _, app = _wire_integration_stack(
        db_engine,
        alerts_provider="openweathermap",
        owm_appid=None,  # Credentials missing
    )
    yield app

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()


@pytest.fixture
def integration_client_owm(integration_app_owm: FastAPI) -> TestClient:
    """TestClient for the OWM alerts integration app."""
    return TestClient(integration_app_owm, raise_server_exceptions=False)


@pytest.fixture
def integration_client_owm_no_credentials(
    integration_app_owm_no_credentials: FastAPI,
) -> TestClient:
    """TestClient for the OWM alerts integration app with no credentials."""
    return TestClient(integration_app_owm_no_credentials, raise_server_exceptions=False)


# ===========================================================================
# Integration: dispatch table
# ===========================================================================


class TestIntegrationOwmAlertsDispatchTable:
    """('alerts', 'openweathermap') is in the dispatch table."""

    def test_openweathermap_is_in_alerts_dispatch_table(self) -> None:
        """get_provider_module(domain='alerts', provider_id='openweathermap') returns module."""
        from weewx_clearskies_api.providers._common.dispatch import (
            get_provider_module,  # noqa: PLC0415
        )
        module = get_provider_module(domain="alerts", provider_id="openweathermap")
        assert module is not None
        assert hasattr(module, "CAPABILITY")
        assert hasattr(module, "fetch")
        assert module.CAPABILITY.provider_id == "openweathermap"
        assert module.CAPABILITY.domain == "alerts"


# ===========================================================================
# Integration: startup wiring
# ===========================================================================


class TestIntegrationOwmAlertsStartupWiring:
    """AlertsSettings with openweathermap provider wires and validates correctly."""

    def test_alerts_settings_with_openweathermap_passes_validate(self) -> None:
        """AlertsSettings({'provider': 'openweathermap'}).validate() does not raise."""
        from weewx_clearskies_api.config.settings import AlertsSettings  # noqa: PLC0415
        settings = AlertsSettings({"provider": "openweathermap"})
        settings.validate()  # Should not raise

    def test_wire_openweathermap_credentials_stores_test_appid(self) -> None:
        """wire_openweathermap_credentials() stores the appid accessible by the endpoint."""
        import weewx_clearskies_api.endpoints.alerts as alerts_mod  # noqa: PLC0415
        from weewx_clearskies_api.endpoints.alerts import (
            wire_openweathermap_credentials,  # noqa: PLC0415
        )
        wire_openweathermap_credentials("TEST_APPID_WIRING")
        assert alerts_mod._openweathermap_appid == "TEST_APPID_WIRING"
        # Restore None
        wire_openweathermap_credentials(None)
        assert alerts_mod._openweathermap_appid is None

    def test_openweathermap_alerts_capability_wires_into_provider_registry(
        self, db_engine: Engine
    ) -> None:
        """wire_providers([openweathermap.CAPABILITY]) → registry has OWM alerts entry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])

        registry = get_provider_registry()
        owm_entries = [
            p for p in registry
            if p.provider_id == "openweathermap" and p.domain == "alerts"
        ]
        assert len(owm_entries) == 1, (
            f"Expected 1 openweathermap alerts entry in registry, found {len(owm_entries)}"
        )
        reset_provider_registry_for_tests()

    def test_alerts_settings_reads_openweathermap_appid_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AlertsSettings reads openweathermap_appid from env var at __init__ time."""
        from weewx_clearskies_api.config.settings import AlertsSettings  # noqa: PLC0415
        monkeypatch.setenv("WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID", "ENVTEST_APPID_123")
        settings = AlertsSettings({"provider": "openweathermap"})
        assert settings.openweathermap_appid == "ENVTEST_APPID_123"

    def test_alerts_settings_openweathermap_appid_none_when_env_absent(self) -> None:
        """AlertsSettings.openweathermap_appid = None when env var not set."""
        import os  # noqa: PLC0415

        from weewx_clearskies_api.config.settings import AlertsSettings  # noqa: PLC0415
        # Ensure env var is not set
        env_key = "WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID"
        original = os.environ.pop(env_key, None)
        try:
            settings = AlertsSettings({})
            assert settings.openweathermap_appid is None
        finally:
            if original is not None:
                os.environ[env_key] = original


# ===========================================================================
# Integration: end-to-end /alerts endpoint
# ===========================================================================


class TestIntegrationOwmAlertsEndpoint:
    """End-to-end GET /api/v1/alerts with OWM provider via respx-mocked HTTP."""

    def test_owm_alerts_returns_200_with_alerts_list_and_owm_source(
        self, integration_client_owm: TestClient
    ) -> None:
        """OWM configured + respx-mocked valid response → 200 source='openweathermap'."""
        alerts_data = _make_valid_owm_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_owm.get("/api/v1/alerts")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )
        body = response.json()
        assert body["source"] == "openweathermap", (
            f"Expected source='openweathermap', got {body.get('source')!r}"
        )
        data = body["data"]
        assert data["source"] == "openweathermap"
        assert len(data["alerts"]) == 2, (
            f"Expected 2 alerts, got {len(data['alerts'])}"
        )

    def test_owm_alerts_records_have_correct_canonical_shape(
        self, integration_client_owm: TestClient
    ) -> None:
        """Each canonical AlertRecord has required fields: id, headline, severity, event, source."""
        alerts_data = _make_valid_owm_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_owm.get("/api/v1/alerts")

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
            assert alert["source"] == "openweathermap"

    def test_owm_alerts_effective_field_is_utc_z_format(
        self, integration_client_owm: TestClient
    ) -> None:
        """canonical effective field is UTC ISO-8601 with Z suffix (datetime conversion)."""
        alerts_data = _make_valid_owm_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_owm.get("/api/v1/alerts")

        body = response.json()
        alerts = body["data"]["alerts"]
        for alert in alerts:
            assert alert["effective"].endswith("Z"), (
                f"effective should be UTC Z format, got {alert['effective']!r}"
            )

    def test_owm_alerts_ids_are_synthesized_correctly(
        self, integration_client_owm: TestClient
    ) -> None:
        """Alert IDs are synthesized as event|start|sender_name (lead-call 13)."""
        alerts_data = _make_valid_owm_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_owm.get("/api/v1/alerts")

        body = response.json()
        alerts = body["data"]["alerts"]
        assert alerts[0]["id"] == "Wind Advisory|1714485600|NWS Seattle WA", (
            f"Expected synthesized ID for first alert, got {alerts[0]['id']!r}"
        )
        assert alerts[1]["id"] == "Tornado Warning|1714490000|NWS Portland OR", (
            f"Expected synthesized ID for second alert, got {alerts[1]['id']!r}"
        )

    def test_owm_alerts_empty_response_returns_200_empty_list(
        self, integration_client_owm: TestClient
    ) -> None:
        """Empty OWM alerts[] → 200 + alerts=[] + source='openweathermap'."""
        empty_data = _load_fixture("alerts_paid_empty.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=empty_data)
            )
            response = integration_client_owm.get("/api/v1/alerts")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["alerts"] == [], (
            f"Expected empty alerts list, got {body['data']['alerts']!r}"
        )
        assert body["data"]["source"] == "openweathermap"

    def test_owm_alerts_basic_tier_401_returns_200_empty_list(
        self, integration_client_owm: TestClient
    ) -> None:
        """Basic-tier 401 (Q1=A) → 200 + alerts=[] (NOT 502 ProviderProblem).

        OWM basic-tier key returns 401 from /data/3.0/onecall; Q1=A decision
        returns graceful empty list matching 'no active alerts' UI behavior.
        """
        error_fixture = _load_fixture("error_401_basic_tier.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(401, json=error_fixture)
            )
            response = integration_client_owm.get("/api/v1/alerts")

        assert response.status_code == 200, (
            f"Expected 200 for basic-tier 401 (Q1=A), got {response.status_code}: "
            f"{response.text[:300]}"
        )
        body = response.json()
        assert body["data"]["alerts"] == [], (
            f"Expected empty alerts list for basic-tier 401, got {body['data']['alerts']!r}"
        )
        assert body["data"]["source"] == "openweathermap"

    def test_owm_alerts_severity_filter_warning_returns_warning_only(
        self, db_engine: Engine
    ) -> None:
        """?severity=warning returns only warning records (Tornado Warning → 'warning').

        Uses a fresh app (not shared fixture) to control cache state.
        """
        from weewx_clearskies_api.providers._common.cache import (
            reset_cache_for_tests,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            reset_provider_registry_for_tests,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _reset_http_client_for_tests,  # noqa: PLC0415
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

        # Mix: Wind Advisory (advisory) + Tornado Warning (warning)
        mixed_data = _make_valid_owm_alerts_fixture()

        _, app = _wire_integration_stack(
            db_engine,
            alerts_provider="openweathermap",
            owm_appid=_TEST_APPID,
        )
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
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
        assert alerts[0]["event"] == "Tornado Warning"

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()

    def test_owm_alerts_credentials_missing_returns_502(
        self, integration_client_owm_no_credentials: TestClient
    ) -> None:
        """Credentials missing (None) → 502 ProviderProblem (KeyInvalid → endpoint 502)."""
        with respx.mock(assert_all_called=False):
            response = integration_client_owm_no_credentials.get("/api/v1/alerts")

        assert response.status_code == 502, (
            f"Expected 502 for missing OWM credentials, got {response.status_code}: "
            f"{response.text[:300]}"
        )

    def test_owm_alerts_unknown_query_param_returns_400_or_422(
        self, integration_client_owm: TestClient
    ) -> None:
        """Unknown query parameter rejected by extra='forbid' via Depends pattern."""
        with respx.mock(assert_all_called=False):
            response = integration_client_owm.get(
                "/api/v1/alerts?severity=advisory&unknown_param=bad"
            )

        assert response.status_code in (400, 422), (
            f"Expected 400 or 422 for unknown query param, got {response.status_code}"
        )

    def test_owm_alerts_partial_domain_fields_are_null_in_response(
        self, integration_client_owm: TestClient
    ) -> None:
        """PARTIAL-DOMAIN fields (urgency/certainty/areaDesc/category) are null in response."""
        alerts_data = _make_valid_owm_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            response = integration_client_owm.get("/api/v1/alerts")

        body = response.json()
        alerts = body["data"]["alerts"]
        for alert in alerts:
            assert alert.get("urgency") is None, (
                f"urgency must be null (PARTIAL-DOMAIN), got {alert.get('urgency')!r}"
            )
            assert alert.get("certainty") is None, (
                f"certainty must be null (PARTIAL-DOMAIN), got {alert.get('certainty')!r}"
            )
            assert alert.get("areaDesc") is None, (
                f"areaDesc must be null (PARTIAL-DOMAIN), got {alert.get('areaDesc')!r}"
            )
            assert alert.get("category") is None, (
                f"category must be null (PARTIAL-DOMAIN), got {alert.get('category')!r}"
            )


# ===========================================================================
# Integration: memory cache end-to-end
# ===========================================================================


class TestIntegrationOwmAlertsMemoryCache:
    """OWM alerts provider: memory cache miss → fetch → cache hit (both backends)."""

    def test_cache_miss_fetches_from_owm_and_caches_result(
        self, db_engine: Engine
    ) -> None:
        """Memory cache miss → one OWM HTTP call; result cached."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            get_cache,
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _reset_basic_tier_warned_for_tests,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        _reset_basic_tier_warned_for_tests()
        openweathermap._rate_limiter._calls.clear()
        wire_cache_from_env()

        alerts_data = _make_valid_owm_alerts_fixture()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records = openweathermap.fetch(
                lat=_LAT, lon=_LON, appid=_TEST_APPID
            )
            call_count = mock.calls.call_count

        assert call_count == 1, f"Expected 1 OWM call on cache miss, got {call_count}"
        assert len(records) == 2
        assert all(r.source == "openweathermap" for r in records)

        # Cache populated
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _build_alerts_cache_key,  # noqa: PLC0415
        )
        cached = get_cache().get(_build_alerts_cache_key(_LAT, _LON))
        assert cached is not None

        reset_cache_for_tests()
        _reset_http_client_for_tests()

    def test_cache_hit_skips_owm_call_and_returns_same_records(
        self, db_engine: Engine
    ) -> None:
        """Memory cache hit → zero OWM HTTP calls; records match first fetch."""
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _reset_basic_tier_warned_for_tests,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        _reset_basic_tier_warned_for_tests()
        openweathermap._rate_limiter._calls.clear()
        wire_cache_from_env()

        alerts_data = _make_valid_owm_alerts_fixture()

        # First fetch — fills memory cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records1 = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        # Second fetch — should come from cache (zero calls)
        with respx.mock(assert_all_called=False) as mock2:
            records2 = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
            cache_hit_calls = mock2.calls.call_count

        assert cache_hit_calls == 0, (
            f"Expected 0 OWM calls on cache hit, got {cache_hit_calls}"
        )
        assert len(records2) == len(records1)
        assert all(r.source == "openweathermap" for r in records2)

        reset_cache_for_tests()
        _reset_http_client_for_tests()


# ===========================================================================
# Integration: Redis cache (optional, redis mark — MUST PASS per brief)
# ===========================================================================


@pytest.mark.redis
class TestIntegrationOwmAlertsRedisCache:
    """Real Redis from the docker-compose redis profile.

    Per brief §Process gates: Redis tier MUST PASS, not skip.
    If Redis is not reachable on weather-dev, this is a brief-gate failure
    that must be surfaced to the lead via SendMessage BEFORE closeout.
    """

    def test_owm_alerts_redis_cache_miss_stores_records(
        self, db_engine: Engine
    ) -> None:
        """Redis cache miss → one OWM HTTP call → records stored in Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _reset_basic_tier_warned_for_tests,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        _reset_basic_tier_warned_for_tests()
        openweathermap._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        alerts_data = _make_valid_owm_alerts_fixture()

        try:
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_OWM_ONECALL_URL).mock(
                    return_value=httpx.Response(200, json=alerts_data)
                )
                records = openweathermap.fetch(
                    lat=_LAT, lon=_LON, appid=_TEST_APPID
                )
                call_count = mock.calls.call_count

            assert call_count == 1, (
                f"Expected 1 OWM call on Redis cache miss, got {call_count}"
            )
            assert len(records) == 2
            assert all(r.source == "openweathermap" for r in records)

            # Verify records in Redis
            from weewx_clearskies_api.providers.alerts.openweathermap import (
                _build_alerts_cache_key,  # noqa: PLC0415
            )
            cache_key = _build_alerts_cache_key(_LAT, _LON)
            cached = cache_mod._cache.get(cache_key)
            assert cached is not None, "Records should be stored in Redis after cache miss"
            assert len(cached) == 2

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()

    def test_owm_alerts_redis_cache_hit_skips_owm_call(
        self, db_engine: Engine
    ) -> None:
        """Redis cache hit → zero OWM HTTP calls; records returned from Redis."""
        _require_redis()

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _reset_basic_tier_warned_for_tests,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        _reset_http_client_for_tests()
        _reset_basic_tier_warned_for_tests()
        openweathermap._rate_limiter._calls.clear()

        redis_cache = RedisCache(url=_REDIS_URL)
        redis_cache._client.flushdb()  # type: ignore[attr-defined]
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = redis_cache

        alerts_data = _make_valid_owm_alerts_fixture()

        try:
            # First fetch — fills Redis cache
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_OWM_ONECALL_URL).mock(
                    return_value=httpx.Response(200, json=alerts_data)
                )
                records1 = openweathermap.fetch(
                    lat=_LAT, lon=_LON, appid=_TEST_APPID
                )
                assert mock.calls.call_count > 0

            # Second fetch — should hit Redis; zero calls
            with respx.mock(assert_all_called=False) as mock2:
                records2 = openweathermap.fetch(
                    lat=_LAT, lon=_LON, appid=_TEST_APPID
                )
                second_call_count = mock2.calls.call_count

            assert second_call_count == 0, (
                f"Expected 0 OWM calls on Redis cache hit, got {second_call_count}"
            )
            assert len(records2) == len(records1)
            assert all(r.source == "openweathermap" for r in records2)

        finally:
            redis_cache._client.flushdb()  # type: ignore[attr-defined]
            reset_cache_for_tests()
            _reset_http_client_for_tests()
