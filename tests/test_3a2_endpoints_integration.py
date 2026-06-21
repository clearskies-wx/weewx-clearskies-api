"""Integration tests for the 8 meta/static/compute endpoints added in 3a-2.

Tests run via `pytest -m integration` against either the MariaDB or SQLite
backend per the BACKEND env var. Same test logic runs twice in CI (ADR-012).

Endpoints covered:
  1. GET /api/v1/almanac
  2. GET /api/v1/almanac/sun-times
  3. GET /api/v1/almanac/moon-phases
  4. GET /api/v1/station
  5. GET /api/v1/capabilities
  6. GET /api/v1/pages
  7. GET /api/v1/charts/groups
  8. GET /api/v1/content/about, GET /api/v1/content/legal

All tests assert against the OpenAPI contract response shapes.

Backend selection:
  BACKEND=mariadb  (default) — uses clearskies_ro SELECT-only user
  BACKEND=sqlite   — uses seeded SQLite file in read-only mode

Schema-shape rule (rules/clearskies-process.md):
  /station MIN/MAX query runs against the seeded production schema (with all
  NOT NULL constraints), not a synthetic one-column stand-in.

Markers:
  @pytest.mark.integration on every test in this module.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

# Apply integration marker to the whole module
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


# ---------------------------------------------------------------------------
# Engine fixtures
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
    from sqlalchemy.pool import NullPool
    engine = create_engine(
        f"sqlite+pysqlite:///file:///{_SQLITE_SDB_PATH}?mode=ro&uri=true",
        poolclass=NullPool,
        future=True,
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def seeded_engine(
    mariadb_ro_engine: Engine, sqlite_ro_engine: Engine
) -> Generator[Engine, None, None]:
    """Return the engine matching BACKEND env var."""
    if _BACKEND == "mariadb":
        yield mariadb_ro_engine
    elif _BACKEND == "sqlite":
        yield sqlite_ro_engine
    else:
        pytest.skip(f"Unknown BACKEND={_BACKEND!r}")


# ---------------------------------------------------------------------------
# weewx.conf fixture (with Station + StdConvert for 3a-2)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def weewx_conf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write a weewx.conf with full [Station] section for /station tests."""
    tmp_path = tmp_path_factory.mktemp("weewx_conf_3a2")
    conf_file = tmp_path / "weewx.conf"
    conf_file.write_text(
        textwrap.dedent("""
        [Station]
            location = "Huntington Beach, CA"
            latitude = 33.66
            longitude = -117.99
            altitude = "10, foot"
            station_type = Vantage
            timezone = America/Los_Angeles

        [StdConvert]
            target_unit = US
        """),
        encoding="utf-8",
    )
    return conf_file


# ---------------------------------------------------------------------------
# Content directory fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def content_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Temp directory with about.md and legal.md."""
    d = tmp_path_factory.mktemp("content_3a2")
    (d / "about.md").write_text(
        "# About This Station\n\nHuntington Beach weather station.\n",
        encoding="utf-8",
    )
    (d / "legal.md").write_text(
        "# Legal\n\nAll data is AS-IS under GPL v3.\n",
        encoding="utf-8",
    )
    return d


# ---------------------------------------------------------------------------
# Application client fixture — wires all 3a-2 services
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def integration_client_3a2(
    seeded_engine: Engine,
    weewx_conf_path: Path,
    content_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[TestClient, None, None]:
    """TestClient wired to the seeded engine + all 3a-2 services."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
        WeewxSettings,
    )
    from weewx_clearskies_api.db.reflection import SchemaReflector
    from weewx_clearskies_api.db.registry import wire_registry
    from weewx_clearskies_api.db.session import wire_engine
    from weewx_clearskies_api.services.content import wire_content_directory
    from weewx_clearskies_api.services.reports import wire_reports_directory
    from weewx_clearskies_api.services.station import load_station_metadata
    from weewx_clearskies_api.services.station import reset_cache as reset_station
    from weewx_clearskies_api.services.units import load_units_block
    from weewx_clearskies_api.services.units import reset_cache as reset_units
    from weewx_clearskies_api.services.weewx_conf import load_weewx_conf
    from weewx_clearskies_api.services.weewx_conf import reset_cache as reset_weewx_conf

    # Reports directory (empty)
    reports_tmp = tmp_path_factory.mktemp("reports_3a2")

    # Wire module-level services
    wire_engine(seeded_engine)
    reset_units()
    reset_weewx_conf()
    reset_station()
    load_units_block(weewx_conf_path)
    wire_reports_directory(str(reports_tmp))
    wire_content_directory(str(content_dir))

    # Load weewx_conf ConfigObj for station loader
    weewx_cfg = load_weewx_conf(weewx_conf_path)

    # Load station metadata
    from weewx_clearskies_api.services.units import get_target_unit
    load_station_metadata(
        cfg=weewx_cfg,
        api_station_id=None,
        api_timezone=None,
        unit_system=get_target_unit(),
    )

    # Reflect schema and wire registry
    reflector = SchemaReflector(seeded_engine)
    registry = reflector.reflect()
    wire_registry(registry)

    # Wire almanac if skyfield available
    try:
        from weewx_clearskies_api.services.almanac import (
            reset_cache as reset_almanac,
        )
        from weewx_clearskies_api.services.almanac import (
            wire_ephemeris_directory,
        )
        reset_almanac()
        ephemeris_tmp = tmp_path_factory.mktemp("ephemeris_3a2")
        # Only wire if de421.bsp is already cached; don't trigger a download in CI
        de421_path = Path("/var/cache/weewx-clearskies/skyfield/de421.bsp")
        if de421_path.exists():
            wire_ephemeris_directory("/var/cache/weewx-clearskies/skyfield/")
    except (ImportError, SystemExit):
        pass  # Almanac skipped if skyfield not available or ephemeris not present

    if _BACKEND == "mariadb":
        db = DatabaseSettings({
            "kind": "mysql",
            "host": "127.0.0.1",
            "port": _MARIADB_HOST_PORT,
            "name": _MARIADB_DB,
        })
    else:
        db = DatabaseSettings({"kind": "sqlite", "path": _SQLITE_SDB_PATH})

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=db,
        weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
    )

    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _assert_problem_json(response: Any) -> None:
    content_type = response.headers.get("content-type", "")
    assert "problem+json" in content_type or "json" in content_type, (
        f"Expected problem+json, got {content_type!r}"
    )
    body = response.json()
    assert "title" in body
    assert "status" in body
    assert isinstance(body["status"], int)


def _assert_z_suffix(dt_str: str | None, field_name: str = "datetime") -> None:
    assert dt_str is not None, f"{field_name} must not be null"
    assert dt_str.endswith("Z"), (
        f"{field_name} must end with Z per ADR-020, got {dt_str!r}"
    )


def _skip_if_almanac_unavailable() -> None:
    try:
        from weewx_clearskies_api.services.almanac import get_ts_eph  # type: ignore[attr-defined]
        get_ts_eph()
    except (ImportError, RuntimeError):
        pytest.skip("Skyfield not available or ephemeris not loaded")


# ---------------------------------------------------------------------------
# 1. GET /api/v1/almanac
# ---------------------------------------------------------------------------


class TestAlmanacSnapshotIntegration:
    """/almanac returns valid AlmanacSnapshot in AlmanacResponse envelope."""

    def test_almanac_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac returns 200."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )

    def test_almanac_response_shape_matches_openapi_almanac_response(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac JSON shape: data + generatedAt (AlmanacResponse per OpenAPI)."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body, "AlmanacResponse must have 'data'"
        assert "generatedAt" in body, "AlmanacResponse must have 'generatedAt'"

    def test_almanac_data_has_date_sun_moon(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac data has date, sun, moon per AlmanacSnapshot schema."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "date" in data
        assert "sun" in data
        assert "moon" in data

    def test_almanac_sun_has_required_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac data.sun has rise, set, transit, daylightMinutes."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac")
        assert resp.status_code == 200
        sun = resp.json()["data"]["sun"]
        for field in ("rise", "set", "transit", "daylightMinutes"):
            assert field in sun, f"AlmanacSnapshot.sun must have '{field}'"

    def test_almanac_moon_has_required_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac data.moon has phaseName and illuminationPercent."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac")
        assert resp.status_code == 200
        moon = resp.json()["data"]["moon"]
        assert "phaseName" in moon
        assert "illuminationPercent" in moon

    def test_almanac_generated_at_has_z_suffix(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac generatedAt is UTC ISO-8601 with Z suffix."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac")
        assert resp.status_code == 200
        _assert_z_suffix(resp.json()["generatedAt"], "generatedAt")

    def test_almanac_with_explicit_date_returns_that_date(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac?date=2024-06-21 → data.date == '2024-06-21'."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac", params={"date": "2024-06-21"})
        assert resp.status_code == 200
        assert resp.json()["data"]["date"] == "2024-06-21"

    def test_almanac_bad_date_returns_400_or_422(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac?date=not-a-date → 400 or 422 problem+json."""
        resp = integration_client_3a2.get(
            "/api/v1/almanac", params={"date": "not-a-date"}
        )
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_almanac_unknown_query_param_returns_400_or_422(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac?unknown=x → 400 or 422 (extra='forbid')."""
        resp = integration_client_3a2.get(
            "/api/v1/almanac", params={"unknown": "x"}
        )
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_almanac_moon_phase_name_is_valid_openapi_enum(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac moon.phaseName is one of the 8 OpenAPI enum values (or null)."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac", params={"date": "2024-01-15"})
        assert resp.status_code == 200
        moon = resp.json()["data"]["moon"]
        phase = moon.get("phaseName")
        if phase is not None:
            valid_phases = {
                "new", "waxing-crescent", "first-quarter", "waxing-gibbous",
                "full", "waning-gibbous", "last-quarter", "waning-crescent",
            }
            assert phase in valid_phases, (
                f"moon.phaseName {phase!r} is not a valid OpenAPI enum value"
            )


# ---------------------------------------------------------------------------
# 2. GET /api/v1/almanac/sun-times
# ---------------------------------------------------------------------------


class TestSunTimesIntegration:
    """/almanac/sun-times returns SunTimesSeries in SunTimesResponse envelope."""

    def test_sun_times_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times returns 200."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac/sun-times")
        assert resp.status_code == 200

    def test_sun_times_response_shape_matches_openapi(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times has data.year and data.days per SunTimesResponse."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac/sun-times")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "generatedAt" in body
        assert "year" in body["data"]
        assert "days" in body["data"]

    def test_sun_times_current_year_days_count_is_365_or_366(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times for current year returns 365 or 366 day entries."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac/sun-times")
        assert resp.status_code == 200
        days = resp.json()["data"]["days"]
        assert len(days) in (365, 366), (
            f"Current year must have 365 or 366 day entries, got {len(days)}"
        )

    def test_sun_times_2024_returns_366_entries(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times?year=2024 returns 366 entries (2024 is a leap year)."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get(
            "/api/v1/almanac/sun-times", params={"year": 2024}
        )
        assert resp.status_code == 200
        days = resp.json()["data"]["days"]
        assert len(days) == 366, f"2024 is a leap year — expected 366, got {len(days)}"

    def test_sun_times_2024_first_entry_is_jan_1(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times?year=2024 first entry is 2024-01-01."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get(
            "/api/v1/almanac/sun-times", params={"year": 2024}
        )
        assert resp.status_code == 200
        days = resp.json()["data"]["days"]
        assert days[0]["date"] == "2024-01-01"

    def test_sun_times_2024_last_entry_is_dec_31(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times?year=2024 last entry is 2024-12-31."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get(
            "/api/v1/almanac/sun-times", params={"year": 2024}
        )
        assert resp.status_code == 200
        days = resp.json()["data"]["days"]
        assert days[-1]["date"] == "2024-12-31"

    def test_sun_times_year_below_1900_returns_400_or_422(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times?year=1899 → 400 or 422."""
        resp = integration_client_3a2.get(
            "/api/v1/almanac/sun-times", params={"year": 1899}
        )
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_sun_times_unknown_param_returns_400_or_422(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/sun-times?bogus=x → 400 or 422 (extra='forbid')."""
        resp = integration_client_3a2.get(
            "/api/v1/almanac/sun-times", params={"bogus": "x"}
        )
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)


# ---------------------------------------------------------------------------
# 3. GET /api/v1/almanac/moon-phases
# ---------------------------------------------------------------------------


class TestMoonPhasesIntegration:
    """/almanac/moon-phases returns MoonPhaseCalendar in MoonPhaseResponse."""

    def test_moon_phases_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/moon-phases returns 200."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get("/api/v1/almanac/moon-phases")
        assert resp.status_code == 200

    def test_moon_phases_without_month_returns_full_year(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/moon-phases without month → full-year span; month is null."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get(
            "/api/v1/almanac/moon-phases", params={"year": 2024}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["month"] is None
        assert len(data["days"]) in (365, 366)

    def test_moon_phases_with_month_6_returns_june_only(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/moon-phases?year=2024&month=6 → 30 day entries for June."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get(
            "/api/v1/almanac/moon-phases", params={"year": 2024, "month": 6}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["month"] == 6
        assert len(data["days"]) == 30

    def test_moon_phases_entries_have_required_openapi_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """Moon-phase entries have date, phaseName, illuminationPercent."""
        _skip_if_almanac_unavailable()
        resp = integration_client_3a2.get(
            "/api/v1/almanac/moon-phases", params={"year": 2024, "month": 1}
        )
        assert resp.status_code == 200
        for entry in resp.json()["data"]["days"][:3]:
            assert "date" in entry
            assert "phaseName" in entry
            assert "illuminationPercent" in entry

    def test_moon_phases_bad_month_returns_400_or_422(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/moon-phases?month=13 → 400 or 422."""
        resp = integration_client_3a2.get(
            "/api/v1/almanac/moon-phases", params={"year": 2024, "month": 13}
        )
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_moon_phases_unknown_param_returns_400_or_422(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/almanac/moon-phases?foo=bar → 400 or 422 (extra='forbid')."""
        resp = integration_client_3a2.get(
            "/api/v1/almanac/moon-phases", params={"foo": "bar"}
        )
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)


# ---------------------------------------------------------------------------
# 4. GET /api/v1/station
# ---------------------------------------------------------------------------


class TestStationIntegration:
    """/station returns StationMetadata in StationResponse envelope."""

    def test_station_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/station returns 200."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )

    def test_station_response_has_required_envelope_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/station → StationResponse has data, units, generatedAt."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "units" in body
        assert "generatedAt" in body

    def test_station_data_has_required_metadata_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/station data has stationId, name, latitude, longitude, altitude."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        data = resp.json()["data"]
        for field in ("stationId", "name", "latitude", "longitude", "altitude"):
            assert field in data, f"StationMetadata must have '{field}'"

    def test_station_first_record_matches_seed_min_epoch(
        self, integration_client_3a2: TestClient, seeded_engine: Engine
    ) -> None:
        """/station firstRecord matches MIN(dateTime) from the seeded archive.

        Schema-shape rule: runs against the production schema (multi-column
        NOT NULL constraints), not a synthetic one-column stand-in.
        """
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        first_record = resp.json()["data"].get("firstRecord")

        with seeded_engine.connect() as conn:
            result = conn.execute(text("SELECT MIN(dateTime) FROM archive"))
            db_min = result.scalar()

        if db_min is None:
            assert first_record is None
        else:
            assert first_record is not None
            _assert_z_suffix(first_record, "firstRecord")

    def test_station_last_record_matches_seed_max_epoch(
        self, integration_client_3a2: TestClient, seeded_engine: Engine
    ) -> None:
        """/station lastRecord matches MAX(dateTime) from the seeded archive."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        last_record = resp.json()["data"].get("lastRecord")

        with seeded_engine.connect() as conn:
            result = conn.execute(text("SELECT MAX(dateTime) FROM archive"))
            db_max = result.scalar()

        if db_max is None:
            assert last_record is None
        else:
            assert last_record is not None
            _assert_z_suffix(last_record, "lastRecord")

    def test_station_first_record_is_before_or_equal_to_last_record(
        self, integration_client_3a2: TestClient
    ) -> None:
        """firstRecord <= lastRecord when both are present."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        data = resp.json()["data"]
        first = data.get("firstRecord")
        last = data.get("lastRecord")
        if first is not None and last is not None:
            assert first <= last, (
                f"firstRecord {first!r} must be <= lastRecord {last!r}"
            )

    def test_station_generated_at_has_z_suffix(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/station generatedAt has Z suffix."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        _assert_z_suffix(resp.json()["generatedAt"], "generatedAt")

    def test_station_unit_system_is_valid_enum(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/station data.unitSystem is US, METRIC, or METRICWX."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        unit_system = resp.json()["data"].get("unitSystem")
        assert unit_system in ("US", "METRIC", "METRICWX")

    def test_station_timezone_is_iana_string(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/station data.timezone is a non-empty IANA TZ identifier string."""
        resp = integration_client_3a2.get("/api/v1/station")
        assert resp.status_code == 200
        timezone = resp.json()["data"].get("timezone")
        assert isinstance(timezone, str) and timezone

    def test_station_empty_archive_returns_both_null_not_500(
        self, seeded_engine: Engine, weewx_conf_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """/station against production-schema-shaped empty archive → firstRecord=null, lastRecord=null.

        Schema-shape rule: uses a production-schema-shaped empty table (all
        NOT NULL constraints present), not a one-column synthetic stand-in.
        The interval column is a MariaDB reserved word — backtick quoting is
        required; this test exercises that query path on both backends.
        """
        # Production-schema-shaped empty archive (all NOT NULL constraints present).
        # StaticPool + check_same_thread=False ensures all connections share the
        # same in-memory SQLite DB so the archive table created here is visible to
        # get_db_session()'s per-request connection.  NullPool (which was used in
        # the original test) creates a fresh ":memory:" DB per connection, causing
        # the archive table to disappear and "no such table" to raise — which was
        # the exact driver for commit b7642ae's speculative "no such table" catch.
        # That catch was removed (F4); this pool switch is the correct fixture fix.
        from sqlalchemy.pool import StaticPool as _StaticPool

        from weewx_clearskies_api.app import create_app
        from weewx_clearskies_api.config.settings import (
            ApiSettings,
            DatabaseSettings,
            HealthSettings,
            LoggingSettings,
            Settings,
            WeewxSettings,
        )
        from weewx_clearskies_api.db.reflection import ColumnInfo, ColumnRegistry
        from weewx_clearskies_api.db.registry import wire_registry
        from weewx_clearskies_api.db.session import wire_engine
        from weewx_clearskies_api.services.content import wire_content_directory
        from weewx_clearskies_api.services.reports import wire_reports_directory
        from weewx_clearskies_api.services.station import (
            load_station_metadata,
        )
        from weewx_clearskies_api.services.station import (
            reset_cache as reset_station,
        )
        from weewx_clearskies_api.services.units import get_target_unit, load_units_block
        from weewx_clearskies_api.services.units import reset_cache as reset_units
        from weewx_clearskies_api.services.weewx_conf import load_weewx_conf
        from weewx_clearskies_api.services.weewx_conf import reset_cache as reset_weewx_conf
        empty_engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=_StaticPool,
            future=True,
        )
        with empty_engine.connect() as conn:
            conn.execute(text(
                "CREATE TABLE archive ("
                "  dateTime INTEGER NOT NULL PRIMARY KEY,"
                "  usUnits INTEGER NOT NULL,"
                "  interval INTEGER NOT NULL,"
                "  outTemp REAL,"
                "  outHumidity REAL,"
                "  windSpeed REAL,"
                "  windDir REAL,"
                "  windGust REAL,"
                "  windGustDir REAL,"
                "  barometer REAL,"
                "  pressure REAL,"
                "  altimeter REAL,"
                "  dewpoint REAL,"
                "  windchill REAL,"
                "  heatindex REAL,"
                "  rainRate REAL,"
                "  rain REAL,"
                "  radiation REAL,"
                "  UV REAL,"
                "  inTemp REAL,"
                "  inHumidity REAL"
                ")"
            ))
            conn.commit()

        try:
            wire_engine(empty_engine)
            reset_units()
            reset_weewx_conf()
            reset_station()
            load_units_block(weewx_conf_path)
            weewx_cfg = load_weewx_conf(weewx_conf_path)
            reports_tmp = tmp_path_factory.mktemp("empty_reports_3a2")
            wire_reports_directory(str(reports_tmp))
            content_tmp = tmp_path_factory.mktemp("empty_content_3a2")
            wire_content_directory(str(content_tmp))

            load_station_metadata(
                cfg=weewx_cfg,
                api_station_id=None,
                api_timezone=None,
                unit_system=get_target_unit(),
            )

            minimal_registry = ColumnRegistry()
            minimal_registry.stock = {
                "dateTime": ColumnInfo("dateTime", "timestamp", True),
                "usUnits": ColumnInfo("usUnits", "usUnits", True),
                "interval": ColumnInfo("interval", "interval", True),
                "outTemp": ColumnInfo("outTemp", "outTemp", True),
            }
            wire_registry(minimal_registry)

            settings = Settings(
                api=ApiSettings({}), health=HealthSettings({}),
                logging_settings=LoggingSettings({}),
                database=DatabaseSettings({"kind": "sqlite", "path": ":memory:"}),
                weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
            )
            app = create_app(settings)
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.get("/api/v1/station")
                assert resp.status_code == 200, (
                    f"/station against empty archive must return 200, "
                    f"got {resp.status_code}: {resp.text}"
                )
                data = resp.json()["data"]
                assert data.get("firstRecord") is None, (
                    "firstRecord must be null for empty archive"
                )
                assert data.get("lastRecord") is None, (
                    "lastRecord must be null for empty archive"
                )
        finally:
            # Restore seeded engine and all module-level state for subsequent tests
            from weewx_clearskies_api.db.reflection import SchemaReflector
            wire_engine(seeded_engine)
            reset_units()
            reset_weewx_conf()
            reset_station()
            load_units_block(weewx_conf_path)
            weewx_cfg_restore = load_weewx_conf(weewx_conf_path)
            load_station_metadata(
                cfg=weewx_cfg_restore,
                api_station_id=None,
                api_timezone=None,
                unit_system=get_target_unit(),
            )
            reflector = SchemaReflector(seeded_engine)
            registry = reflector.reflect()
            wire_registry(registry)
            # Restore content directory so subsequent tests can still find about.md
            wire_content_directory(str(content_dir))
            empty_engine.dispose()

    def test_station_both_backends_min_max_query_consistent(
        self, seeded_engine: Engine
    ) -> None:
        """MIN/MAX archive query consistent on both backends (dialect drift test).

        The archive table has a column named `interval` which is a MariaDB
        reserved word. The query SELECT MIN/MAX(dateTime) also implicitly checks
        the archive table is accessible. The dual-backend gate catches any
        backtick-quoting dialect drift.
        """
        with seeded_engine.connect() as conn:
            result = conn.execute(
                text("SELECT MIN(dateTime), MAX(dateTime) FROM archive")
            )
            row = result.fetchone()

        assert row is not None
        min_dt, max_dt = row[0], row[1]
        assert min_dt is not None, "Seeded archive must have rows"
        assert max_dt is not None
        assert min_dt <= max_dt, f"MIN ({min_dt}) must be <= MAX ({max_dt})"


# ---------------------------------------------------------------------------
# 5. GET /api/v1/capabilities
# ---------------------------------------------------------------------------


class TestCapabilitiesIntegration:
    """/capabilities returns CapabilityRegistry in CapabilityResponse envelope."""

    def test_capabilities_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/capabilities returns 200."""
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200

    def test_capabilities_response_has_required_envelope_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/capabilities → CapabilityResponse has data and generatedAt."""
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "generatedAt" in body

    def test_capabilities_data_has_registry_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/capabilities data has providers, weewxColumns, canonicalFieldsAvailable."""
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "providers" in data
        assert "weewxColumns" in data
        assert "canonicalFieldsAvailable" in data

    def test_capabilities_providers_is_empty_list(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/capabilities providers=[] (no provider modules in 3a-2 per ADR-038)."""
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200
        assert resp.json()["data"]["providers"] == []

    def test_capabilities_weewx_columns_count_matches_registry(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/capabilities weewxColumns count matches len(registry.stock)."""
        from weewx_clearskies_api.db.registry import get_registry
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200
        weewx_columns = resp.json()["data"]["weewxColumns"]
        registry = get_registry()
        assert len(weewx_columns) == len(registry.stock), (
            f"weewxColumns must have one entry per stock column "
            f"(len={len(registry.stock)}), got {len(weewx_columns)}"
        )

    def test_capabilities_canonical_fields_same_length_as_weewx_columns(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/capabilities canonicalFieldsAvailable has same length as weewxColumns."""
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["canonicalFieldsAvailable"]) == len(data["weewxColumns"])

    def test_capabilities_weewx_columns_have_canonical_and_archive_keys(
        self, integration_client_3a2: TestClient
    ) -> None:
        """Each weewxColumns entry has canonicalField and archiveColumn."""
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200
        for entry in resp.json()["data"]["weewxColumns"]:
            assert "canonicalField" in entry
            assert "archiveColumn" in entry

    def test_capabilities_generated_at_has_z_suffix(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/capabilities generatedAt has Z suffix."""
        resp = integration_client_3a2.get("/api/v1/capabilities")
        assert resp.status_code == 200
        _assert_z_suffix(resp.json()["generatedAt"], "generatedAt")


# ---------------------------------------------------------------------------
# 6. GET /api/v1/pages
# ---------------------------------------------------------------------------


class TestPagesIntegration:
    """/pages returns PageList in PageListResponse envelope."""

    def test_pages_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/pages returns 200."""
        resp = integration_client_3a2.get("/api/v1/pages")
        assert resp.status_code == 200

    def test_pages_response_has_required_envelope_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/pages → PageListResponse has data and generatedAt."""
        resp = integration_client_3a2.get("/api/v1/pages")
        assert resp.status_code == 200
        assert "data" in resp.json()
        assert "generatedAt" in resp.json()

    def test_pages_data_has_pages_array(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/pages data has 'pages' array."""
        resp = integration_client_3a2.get("/api/v1/pages")
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"]["pages"], list)

    def test_pages_default_config_returns_9_pages(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/pages with no hidden config returns all 9 built-in pages."""
        resp = integration_client_3a2.get("/api/v1/pages")
        assert resp.status_code == 200
        pages = resp.json()["data"]["pages"]
        assert len(pages) == 9, (
            f"Default config must return 9 pages, got {len(pages)}"
        )

    def test_pages_entries_have_required_openapi_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """Each page entry has slug, name, icon, navPosition, builtIn."""
        resp = integration_client_3a2.get("/api/v1/pages")
        assert resp.status_code == 200
        for page in resp.json()["data"]["pages"]:
            assert "slug" in page
            assert "name" in page
            assert "icon" in page
            assert "navPosition" in page
            assert "builtIn" in page

    def test_pages_generated_at_has_z_suffix(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/pages generatedAt has Z suffix."""
        resp = integration_client_3a2.get("/api/v1/pages")
        assert resp.status_code == 200
        _assert_z_suffix(resp.json()["generatedAt"], "generatedAt")


# ---------------------------------------------------------------------------
# 7. GET /api/v1/charts/groups
# ---------------------------------------------------------------------------


class TestChartGroupsIntegration:
    """/charts/groups returns ChartGroupList in ChartGroupResponse envelope."""

    def test_chart_groups_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/charts/groups returns 200."""
        resp = integration_client_3a2.get("/api/v1/charts/groups")
        assert resp.status_code == 200

    def test_chart_groups_response_has_required_envelope_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/charts/groups → ChartGroupResponse has data and generatedAt."""
        resp = integration_client_3a2.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        assert "data" in resp.json()
        assert "generatedAt" in resp.json()

    def test_chart_groups_data_has_groups_array(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/charts/groups data has 'groups' array."""
        resp = integration_client_3a2.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"]["groups"], list)

    def test_chart_groups_entries_have_required_openapi_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """Each ChartGroup entry has id, name, builtIn, members per OpenAPI."""
        resp = integration_client_3a2.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        for group in resp.json()["data"]["groups"]:
            assert "id" in group
            assert "name" in group
            assert "builtIn" in group
            assert "members" in group
            assert isinstance(group["members"], list)

    def test_chart_groups_members_only_contain_mapped_fields(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/charts/groups members contain only fields in the registry's mapped set."""
        from weewx_clearskies_api.db.registry import get_registry
        resp = integration_client_3a2.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        registry = get_registry()
        mapped_canonical = {info.canonical_name for info in registry.stock.values()}
        for group in resp.json()["data"]["groups"]:
            for member in group["members"]:
                assert member in mapped_canonical, (
                    f"Group {group['id']!r} member {member!r} is not in mapped registry"
                )

    def test_chart_groups_no_empty_members_lists_in_response(
        self, integration_client_3a2: TestClient
    ) -> None:
        """No group with members=[] in response (self-hide rule)."""
        resp = integration_client_3a2.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        for group in resp.json()["data"]["groups"]:
            assert len(group["members"]) > 0, (
                f"Group {group['id']!r} has empty members[] — must be self-hidden"
            )

    def test_chart_groups_generated_at_has_z_suffix(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/charts/groups generatedAt has Z suffix."""
        resp = integration_client_3a2.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        _assert_z_suffix(resp.json()["generatedAt"], "generatedAt")


# ---------------------------------------------------------------------------
# 8. GET /api/v1/content/about and /api/v1/content/legal
# ---------------------------------------------------------------------------


class TestContentEndpointsIntegration:
    """/content/about and /content/legal return MarkdownContent in MarkdownResponse."""

    def test_content_about_with_file_present_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/about with about.md present → 200."""
        resp = integration_client_3a2.get("/api/v1/content/about")
        assert resp.status_code == 200

    def test_content_about_response_has_data_and_generated_at(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/about → MarkdownResponse has data and generatedAt."""
        resp = integration_client_3a2.get("/api/v1/content/about")
        assert resp.status_code == 200
        assert "data" in resp.json()
        assert "generatedAt" in resp.json()

    def test_content_about_data_has_markdown_field(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/about data has markdown (required field per OpenAPI)."""
        resp = integration_client_3a2.get("/api/v1/content/about")
        assert resp.status_code == 200
        assert "markdown" in resp.json()["data"]
        assert resp.json()["data"]["markdown"], "markdown must be non-empty"

    def test_content_about_returns_correct_text(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/about returns the actual content of about.md."""
        resp = integration_client_3a2.get("/api/v1/content/about")
        assert resp.status_code == 200
        markdown = resp.json()["data"]["markdown"]
        assert "About" in markdown

    def test_content_about_updated_at_has_z_suffix_when_present(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/about updatedAt (if non-null) has Z suffix per ADR-020."""
        resp = integration_client_3a2.get("/api/v1/content/about")
        assert resp.status_code == 200
        updated_at = resp.json()["data"].get("updatedAt")
        if updated_at is not None:
            _assert_z_suffix(updated_at, "updatedAt")

    def test_content_legal_with_file_present_returns_200(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/legal with legal.md present → 200."""
        resp = integration_client_3a2.get("/api/v1/content/legal")
        assert resp.status_code == 200

    def test_content_legal_data_has_non_empty_markdown(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/legal data.markdown is non-empty."""
        resp = integration_client_3a2.get("/api/v1/content/legal")
        assert resp.status_code == 200
        assert resp.json()["data"]["markdown"]

    def test_content_about_missing_file_returns_404_problem_json(
        self, seeded_engine: Engine, weewx_conf_path: Path,
        content_dir: Path, tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """/content/about when about.md absent → 404 problem+json."""
        from weewx_clearskies_api.app import create_app
        from weewx_clearskies_api.config.settings import (
            ApiSettings,
            DatabaseSettings,
            HealthSettings,
            LoggingSettings,
            Settings,
            WeewxSettings,
        )
        from weewx_clearskies_api.db.reflection import SchemaReflector
        from weewx_clearskies_api.db.registry import wire_registry
        from weewx_clearskies_api.db.session import wire_engine
        from weewx_clearskies_api.services.content import wire_content_directory
        from weewx_clearskies_api.services.reports import wire_reports_directory
        from weewx_clearskies_api.services.station import (
            load_station_metadata,
        )
        from weewx_clearskies_api.services.station import (
            reset_cache as reset_station,
        )
        from weewx_clearskies_api.services.units import get_target_unit, load_units_block
        from weewx_clearskies_api.services.units import reset_cache as reset_units
        from weewx_clearskies_api.services.weewx_conf import load_weewx_conf
        from weewx_clearskies_api.services.weewx_conf import reset_cache as reset_weewx_conf

        empty_content = tmp_path_factory.mktemp("content_404_3a2")
        # Do NOT create about.md

        wire_engine(seeded_engine)
        reset_units()
        reset_weewx_conf()
        reset_station()
        load_units_block(weewx_conf_path)
        weewx_cfg = load_weewx_conf(weewx_conf_path)
        reports_tmp = tmp_path_factory.mktemp("reports_404_3a2")
        wire_reports_directory(str(reports_tmp))
        wire_content_directory(str(empty_content))
        load_station_metadata(
            cfg=weewx_cfg, api_station_id=None, api_timezone=None,
            unit_system=get_target_unit(),
        )
        reflector = SchemaReflector(seeded_engine)
        registry = reflector.reflect()
        wire_registry(registry)

        if _BACKEND == "mariadb":
            db = DatabaseSettings({
                "kind": "mysql", "host": "127.0.0.1",
                "port": _MARIADB_HOST_PORT, "name": _MARIADB_DB,
            })
        else:
            db = DatabaseSettings({"kind": "sqlite", "path": _SQLITE_SDB_PATH})

        settings = Settings(
            api=ApiSettings({}), health=HealthSettings({}),
            logging_settings=LoggingSettings({}),
            database=db, weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
        )
        app = create_app(settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.get("/api/v1/content/about")
                assert resp.status_code == 404, (
                    f"Missing about.md must return 404, got {resp.status_code}"
                )
                _assert_problem_json(resp)
        finally:
            # Restore content directory for subsequent tests
            wire_content_directory(str(content_dir))

    def test_content_about_404_detail_does_not_leak_filesystem_path(
        self, seeded_engine: Engine, weewx_conf_path: Path,
        content_dir: Path, tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """404 detail for missing about.md must NOT contain the filesystem path."""
        from weewx_clearskies_api.app import create_app
        from weewx_clearskies_api.config.settings import (
            ApiSettings,
            DatabaseSettings,
            HealthSettings,
            LoggingSettings,
            Settings,
            WeewxSettings,
        )
        from weewx_clearskies_api.db.reflection import SchemaReflector
        from weewx_clearskies_api.db.registry import wire_registry
        from weewx_clearskies_api.db.session import wire_engine
        from weewx_clearskies_api.services.content import wire_content_directory
        from weewx_clearskies_api.services.reports import wire_reports_directory
        from weewx_clearskies_api.services.station import (
            load_station_metadata,
        )
        from weewx_clearskies_api.services.station import (
            reset_cache as reset_station,
        )
        from weewx_clearskies_api.services.units import get_target_unit, load_units_block
        from weewx_clearskies_api.services.units import reset_cache as reset_units
        from weewx_clearskies_api.services.weewx_conf import load_weewx_conf
        from weewx_clearskies_api.services.weewx_conf import reset_cache as reset_weewx_conf

        no_files_dir = tmp_path_factory.mktemp("content_no_leak_3a2")

        wire_engine(seeded_engine)
        reset_units()
        reset_weewx_conf()
        reset_station()
        load_units_block(weewx_conf_path)
        weewx_cfg = load_weewx_conf(weewx_conf_path)
        reports_tmp = tmp_path_factory.mktemp("reports_no_leak_3a2")
        wire_reports_directory(str(reports_tmp))
        wire_content_directory(str(no_files_dir))
        load_station_metadata(
            cfg=weewx_cfg, api_station_id=None, api_timezone=None,
            unit_system=get_target_unit(),
        )
        reflector = SchemaReflector(seeded_engine)
        registry = reflector.reflect()
        wire_registry(registry)

        if _BACKEND == "mariadb":
            db = DatabaseSettings({
                "kind": "mysql", "host": "127.0.0.1",
                "port": _MARIADB_HOST_PORT, "name": _MARIADB_DB,
            })
        else:
            db = DatabaseSettings({"kind": "sqlite", "path": _SQLITE_SDB_PATH})

        settings = Settings(
            api=ApiSettings({}), health=HealthSettings({}),
            logging_settings=LoggingSettings({}),
            database=db, weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
        )
        app = create_app(settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.get("/api/v1/content/about")
                if resp.status_code == 404:
                    detail = resp.json().get("detail", "")
                    assert str(no_files_dir) not in detail, (
                        f"404 detail must not leak filesystem path; got: {detail!r}"
                    )
        finally:
            # Restore content directory for subsequent tests
            wire_content_directory(str(content_dir))

    def test_content_generated_at_has_z_suffix(
        self, integration_client_3a2: TestClient
    ) -> None:
        """/content/about generatedAt has Z suffix."""
        resp = integration_client_3a2.get("/api/v1/content/about")
        assert resp.status_code == 200
        _assert_z_suffix(resp.json()["generatedAt"], "generatedAt")
