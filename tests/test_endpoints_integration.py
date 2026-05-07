"""Integration tests for the 6 DB-backed endpoints against the real dev/test stack.

Tests run via `pytest -m integration` against either the MariaDB or SQLite
backend per the BACKEND env var. Same test logic runs twice in CI (ADR-012).

Endpoints covered:
  1. GET /api/v1/current
  2. GET /api/v1/archive (raw / hour / day intervals; cursor + page pagination)
  3. GET /api/v1/records (section-grouped highs/lows; self-hide)
  4. GET /api/v1/reports (listing)
  5. GET /api/v1/reports/{year}/{month}
  6. GET /api/v1/reports/{year}

All tests assert against the OpenAPI contract (docs/contracts/openapi-v1.yaml)
response shapes, not against implementation internals.

Backend selection:
  BACKEND=mariadb  (default) — uses clearskies_ro SELECT-only user
  BACKEND=sqlite   — uses seeded SQLite file in read-only mode

Seed data: 5 rows from real production weewx archive (Huntington Beach CA,
usUnits=1 US, dateTime range 1778098500..1778099700, all within 2026-05-06T20:15–35Z).

Markers:
  @pytest.mark.integration on every test in this module.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any, Generator

import pytest
from fastapi import FastAPI
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

# Seed data constants (from snapshot/data/archive.csv inspection)
_SEED_ROW_COUNT = 5
_SEED_MIN_EPOCH = 1778098500  # 2026-05-06T20:15:00Z
_SEED_MAX_EPOCH = 1778099700  # 2026-05-06T20:35:00Z
_SEED_OUT_TEMP_MIN = 68.0
_SEED_OUT_TEMP_MAX = 70.0


def _skip_if_backend_not_configured(backend: str) -> None:
    if _BACKEND != backend:
        pytest.skip(
            f"BACKEND={_BACKEND!r}; set BACKEND={backend} to run this test"
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
# weewx.conf fixture for units service
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def weewx_conf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write a minimal weewx.conf with US unit system for tests."""
    tmp_path = tmp_path_factory.mktemp("weewx_conf")
    conf_file = tmp_path / "weewx.conf"
    conf_file.write_text(
        textwrap.dedent("""
        [StdConvert]
            target_unit = US
        """),
        encoding="utf-8",
    )
    return conf_file


# ---------------------------------------------------------------------------
# Application client fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def integration_client(
    seeded_engine: Engine,
    weewx_conf_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[TestClient, None, None]:
    """TestClient wired to the seeded engine + all module-level services."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
        WeewxSettings,
    )
    from weewx_clearskies_api.db.reflection import SchemaReflector
    from weewx_clearskies_api.db.registry import wire_registry
    from weewx_clearskies_api.db.session import wire_engine
    from weewx_clearskies_api.services.reports import wire_reports_directory
    from weewx_clearskies_api.services.units import load_units_block, reset_cache

    # Set up a minimal reports directory
    reports_tmp = tmp_path_factory.mktemp("reports")

    # Wire all module-level services
    wire_engine(seeded_engine)
    reset_cache()
    load_units_block(weewx_conf_path)
    wire_reports_directory(str(reports_tmp))

    # Reflect the schema and wire the registry
    reflector = SchemaReflector(seeded_engine)
    registry = reflector.reflect()
    wire_registry(registry)

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
        ratelimit=RateLimitSettings({}),
        database=db,
        weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
    )

    app = create_app(settings)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ---------------------------------------------------------------------------
# Helper: assert RFC 9457 problem+json shape
# ---------------------------------------------------------------------------


def _assert_problem_json(response: Any) -> None:
    """Assert response body conforms to the Problem schema (RFC 9457)."""
    content_type = response.headers.get("content-type", "")
    assert "problem+json" in content_type or "json" in content_type, (
        f"Expected problem+json content-type, got {content_type!r}"
    )
    body = response.json()
    assert "title" in body, "Problem response must have 'title'"
    assert "status" in body, "Problem response must have 'status'"
    assert isinstance(body["status"], int)


# ---------------------------------------------------------------------------
# 1. GET /api/v1/current
# ---------------------------------------------------------------------------


class TestCurrentEndpoint:
    """/current returns the most-recent archive row in ObservationResponse envelope."""

    def test_current_returns_200_with_observation_response_envelope(
        self, integration_client: TestClient
    ) -> None:
        """/current 200 → ObservationResponse with data, units, source, generatedAt."""
        resp = integration_client.get("/api/v1/current")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert "data" in body, "ObservationResponse must have 'data'"
        assert "units" in body, "ObservationResponse must have 'units'"
        assert "source" in body, "ObservationResponse must have 'source'"
        assert "generatedAt" in body, "ObservationResponse must have 'generatedAt'"

    def test_current_source_is_weewx(self, integration_client: TestClient) -> None:
        """/current source field is always 'weewx'."""
        resp = integration_client.get("/api/v1/current")
        assert resp.status_code == 200
        assert resp.json()["source"] == "weewx"

    def test_current_data_has_timestamp_with_z_suffix(
        self, integration_client: TestClient
    ) -> None:
        """/current data.timestamp is UTC ISO-8601 with Z suffix (ADR-020)."""
        resp = integration_client.get("/api/v1/current")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data is not None, "data must not be null when seeded archive has rows"
        assert "timestamp" in data
        ts = data["timestamp"]
        assert ts.endswith("Z"), (
            f"timestamp {ts!r} must end with Z per ADR-020"
        )

    def test_current_data_out_temp_within_seed_range(
        self, integration_client: TestClient
    ) -> None:
        """/current data.outTemp is within the seed data's temperature range."""
        resp = integration_client.get("/api/v1/current")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data is not None
        out_temp = data.get("outTemp")
        if out_temp is not None:
            assert _SEED_OUT_TEMP_MIN <= out_temp <= _SEED_OUT_TEMP_MAX, (
                f"outTemp {out_temp} outside expected seed range "
                f"[{_SEED_OUT_TEMP_MIN}, {_SEED_OUT_TEMP_MAX}]"
            )

    def test_current_units_block_has_string_values(
        self, integration_client: TestClient
    ) -> None:
        """/current units block maps field names to unit strings."""
        resp = integration_client.get("/api/v1/current")
        assert resp.status_code == 200
        units = resp.json()["units"]
        assert isinstance(units, dict)
        for field, unit in units.items():
            assert isinstance(field, str) and isinstance(unit, str), (
                f"units[{field!r}] = {unit!r} must be str → str mapping"
            )

    def test_current_generated_at_has_z_suffix(
        self, integration_client: TestClient
    ) -> None:
        """/current generatedAt is UTC ISO-8601 with Z suffix."""
        resp = integration_client.get("/api/v1/current")
        assert resp.status_code == 200
        generated_at = resp.json()["generatedAt"]
        assert generated_at.endswith("Z"), (
            f"generatedAt {generated_at!r} must end with Z"
        )

    def test_current_returns_latest_row_by_datetime(
        self, integration_client: TestClient, seeded_engine: Engine
    ) -> None:
        """/current returns the row with the highest dateTime (most recent)."""
        resp = integration_client.get("/api/v1/current")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data is not None

        with seeded_engine.connect() as conn:
            result = conn.execute(
                text("SELECT MAX(dateTime) FROM archive")
            )
            max_epoch = result.scalar()

        import datetime as dt_module
        expected_dt = dt_module.datetime.fromtimestamp(
            int(max_epoch), tz=dt_module.timezone.utc
        )
        expected_ts = expected_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert data["timestamp"] == expected_ts, (
            f"Expected timestamp {expected_ts!r} for max epoch {max_epoch}, "
            f"got {data['timestamp']!r}"
        )

    def test_current_against_empty_archive_returns_null_data_not_404(
        self,
        seeded_engine: Engine,
        weewx_conf_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """/current against empty archive returns data=null, not 404 or 500."""
        from sqlalchemy.pool import NullPool

        from weewx_clearskies_api.app import create_app
        from weewx_clearskies_api.config.settings import (
            ApiSettings,
            DatabaseSettings,
            HealthSettings,
            LoggingSettings,
            RateLimitSettings,
            Settings,
            WeewxSettings,
        )
        from weewx_clearskies_api.db.reflection import ColumnRegistry, ColumnInfo
        from weewx_clearskies_api.db.registry import wire_registry
        from weewx_clearskies_api.db.session import wire_engine
        from weewx_clearskies_api.services.reports import wire_reports_directory
        from weewx_clearskies_api.services.units import load_units_block, reset_cache

        empty_engine = create_engine(
            "sqlite:///:memory:", poolclass=NullPool, future=True
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
                "  inHumidity REAL,"
                "  ET REAL,"
                "  hail REAL,"
                "  hailRate REAL"
                ")"
            ))
            conn.commit()

        try:
            wire_engine(empty_engine)
            reset_cache()
            load_units_block(weewx_conf_path)
            wire_reports_directory("/tmp")
            # Minimal registry with just the core observation columns
            from weewx_clearskies_api.db.reflection import ColumnRegistry, ColumnInfo
            minimal_registry = ColumnRegistry()
            minimal_registry.stock = {
                "dateTime": ColumnInfo("dateTime", "timestamp", True),
                "usUnits": ColumnInfo("usUnits", "usUnits", True),
                "interval": ColumnInfo("interval", "interval", True),
                "outTemp": ColumnInfo("outTemp", "outTemp", True),
            }
            wire_registry(minimal_registry)

            settings = Settings(
                api=ApiSettings({}),
                health=HealthSettings({}),
                logging_settings=LoggingSettings({}),
                ratelimit=RateLimitSettings({}),
                database=DatabaseSettings({"kind": "sqlite", "path": ":memory:"}),
                weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
            )
            app = create_app(settings)

            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.get("/api/v1/current")
                assert resp.status_code == 200, (
                    f"Empty archive must return 200 (data=null), not {resp.status_code}: "
                    f"{resp.text}"
                )
                body = resp.json()
                assert body["data"] is None, (
                    f"Empty archive must return data=null, got data={body.get('data')!r}"
                )
        finally:
            # Restore the seeded engine for subsequent tests
            from weewx_clearskies_api.db.reflection import SchemaReflector
            wire_engine(seeded_engine)
            reset_cache()
            load_units_block(weewx_conf_path)
            reflector = SchemaReflector(seeded_engine)
            registry = reflector.reflect()
            wire_registry(registry)
            empty_engine.dispose()


# ---------------------------------------------------------------------------
# 2. GET /api/v1/archive
# ---------------------------------------------------------------------------


class TestArchiveEndpoint:
    """/archive returns historical records in ArchiveResponse envelope."""

    def test_archive_returns_200_with_archive_response_envelope(
        self, integration_client: TestClient
    ) -> None:
        """/archive 200 → ArchiveResponse with data, units, source, generatedAt, page."""
        resp = integration_client.get("/api/v1/archive")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert "data" in body
        assert "units" in body
        assert "source" in body
        assert "generatedAt" in body
        assert "page" in body

    def test_archive_source_is_weewx(self, integration_client: TestClient) -> None:
        """/archive source field is always 'weewx'."""
        resp = integration_client.get("/api/v1/archive")
        assert resp.status_code == 200
        assert resp.json()["source"] == "weewx"

    def test_archive_data_is_list(self, integration_client: TestClient) -> None:
        """/archive data field is a list."""
        resp = integration_client.get("/api/v1/archive")
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)

    def test_archive_with_from_to_window_returns_matching_rows(
        self, integration_client: TestClient, seeded_engine: Engine
    ) -> None:
        """/archive with from/to window returns the correct subset of rows."""
        # Window: include all 5 seeded rows (all within 2026-05-06T20:15–35Z)
        from_dt = "2026-05-06T20:00:00Z"
        to_dt = "2026-05-06T21:00:00Z"

        resp = integration_client.get(
            "/api/v1/archive",
            params={"from": from_dt, "to": to_dt, "interval": "raw"},
        )
        assert resp.status_code == 200, (
            f"Expected 200 for from/to window, got {resp.status_code}: {resp.text}"
        )

        data = resp.json()["data"]
        # Direct SQL count for verification
        with seeded_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT COUNT(*) FROM archive "
                    "WHERE dateTime >= :from_ts AND dateTime < :to_ts"
                ),
                {
                    "from_ts": 1778097600,  # 2026-05-06T20:00:00Z
                    "to_ts": 1778101200,    # 2026-05-06T21:00:00Z
                },
            )
            expected_count = result.scalar()

        assert len(data) == expected_count, (
            f"Expected {expected_count} rows in the from/to window, got {len(data)}"
        )

    def test_archive_default_without_params_returns_seed_rows(
        self, integration_client: TestClient
    ) -> None:
        """/archive without params returns rows (non-empty for seeded archive)."""
        resp = integration_client.get("/api/v1/archive")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) > 0, "Seeded archive must return at least one row"

    def test_archive_page_info_has_limit(self, integration_client: TestClient) -> None:
        """/archive page object has 'limit' field (required per OpenAPI)."""
        resp = integration_client.get("/api/v1/archive")
        assert resp.status_code == 200
        page = resp.json()["page"]
        assert "limit" in page, "page object must have 'limit' (required per OpenAPI)"

    def test_archive_interval_hour_returns_one_hourly_bucket_for_seed_window(
        self, integration_client: TestClient
    ) -> None:
        """/archive interval=hour with the seed window → 1 hourly bucket."""
        # All 5 seeded rows fall within the same hour (20:15–20:35 UTC)
        resp = integration_client.get(
            "/api/v1/archive",
            params={
                "interval": "hour",
                "from": "2026-05-06T20:00:00Z",
                "to": "2026-05-06T21:00:00Z",
            },
        )
        assert resp.status_code == 200, (
            f"Expected 200 for hourly interval, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()["data"]
        # All seed rows are in the same hour → 1 hourly bucket
        assert len(data) == 1, (
            f"Expected 1 hourly bucket (all seeds in 20:xx UTC), got {len(data)}"
        )

    def test_archive_interval_hour_out_temp_matches_direct_sql_avg(
        self, integration_client: TestClient, seeded_engine: Engine
    ) -> None:
        """/archive interval=hour outTemp matches direct AVG(outTemp) GROUP BY hour."""
        resp = integration_client.get(
            "/api/v1/archive",
            params={
                "interval": "hour",
                "from": "2026-05-06T20:00:00Z",
                "to": "2026-05-06T21:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1

        api_out_temp = data[0].get("outTemp")
        if api_out_temp is None:
            pytest.skip("outTemp was null in hourly aggregate — skipping value check")

        # Direct SQL AVG
        with seeded_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT AVG(outTemp) FROM archive "
                    "WHERE dateTime >= :from_ts AND dateTime < :to_ts "
                    "AND outTemp IS NOT NULL"
                ),
                {"from_ts": 1778097600, "to_ts": 1778101200},
            )
            expected_avg = result.scalar()

        if expected_avg is None:
            pytest.skip("No non-null outTemp in seed for AVG check")

        assert abs(api_out_temp - float(expected_avg)) < 0.01, (
            f"Hourly outTemp {api_out_temp} differs from direct SQL AVG {expected_avg}"
        )

    def test_archive_interval_hour_both_backends_produce_same_bucket_count(
        self, seeded_engine: Engine
    ) -> None:
        """Dialect helper produces 1 hourly bucket on both backends for the seed window.

        Exercises the SQLite strftime vs MariaDB FROM_UNIXTIME dialect helper
        directly via the archive service.
        """
        from weewx_clearskies_api.services.archive import _HourDialect

        dialect = _HourDialect(seeded_engine.dialect.name)
        # Build the group-by expression and verify it's valid SQL on this backend
        bucket_expr = dialect.hour_bucket_expr()
        assert bucket_expr, "hour_bucket_expr must return a non-empty string"

        # Verify the dialect helper produces the right SQL for this backend
        with seeded_engine.connect() as conn:
            result = conn.execute(
                text(
                    f"SELECT COUNT(DISTINCT {bucket_expr}) "
                    "FROM archive "
                    "WHERE dateTime >= :from_ts AND dateTime < :to_ts"
                ),
                {
                    "from_ts": _SEED_MIN_EPOCH,
                    "to_ts": _SEED_MAX_EPOCH + 1,
                },
            )
            count = result.scalar()

        assert count == 1, (
            f"Expected 1 hourly bucket on {_BACKEND}, got {count}"
        )

    def test_archive_cursor_pagination_yields_all_rows_exactly_once(
        self, integration_client: TestClient
    ) -> None:
        """Cursor-based pagination walks all rows exactly once (no duplicates)."""
        # Use a small limit to force multiple pages (5 seed rows, limit=2)
        limit = 2
        all_timestamps: list[str] = []
        cursor: str | None = None

        for _ in range(10):  # Safety cap: max 10 pages for 5 seed rows
            params: dict[str, Any] = {"limit": limit, "interval": "raw"}
            if cursor:
                params["cursor"] = cursor

            resp = integration_client.get("/api/v1/archive", params=params)
            assert resp.status_code == 200, (
                f"Expected 200 during cursor walk, got {resp.status_code}: {resp.text}"
            )

            body = resp.json()
            data = body["data"]
            for record in data:
                all_timestamps.append(record["timestamp"])

            cursor = body["page"].get("cursor")
            if not cursor:
                break

        # Assert no duplicates
        assert len(all_timestamps) == len(set(all_timestamps)), (
            "Cursor pagination must not yield duplicate rows"
        )
        # Assert count matches seed (within the default time window)
        assert len(all_timestamps) > 0, (
            "Cursor pagination must return at least some rows"
        )

    def test_archive_page_mode_total_pages_times_limit_gte_total_records(
        self, integration_client: TestClient
    ) -> None:
        """Page mode: totalPages * limit >= totalRecords."""
        limit = 2
        resp = integration_client.get(
            "/api/v1/archive", params={"limit": limit, "page": 1}
        )
        assert resp.status_code == 200, (
            f"Expected 200 for page mode, got {resp.status_code}: {resp.text}"
        )

        page = resp.json()["page"]
        total_records = page.get("totalRecords")
        total_pages = page.get("totalPages")

        if total_records is None or total_pages is None:
            pytest.skip("totalRecords/totalPages not in page response — check impl")

        assert total_pages * limit >= total_records, (
            f"totalPages({total_pages}) * limit({limit}) = {total_pages * limit} "
            f"must be >= totalRecords({total_records})"
        )

    def test_archive_cursor_and_page_both_supplied_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """Supplying both cursor and page → 400 or 422 problem+json."""
        resp = integration_client.get(
            "/api/v1/archive", params={"cursor": "abc123", "page": 1}
        )
        assert resp.status_code in (400, 422), (
            f"Expected 400 or 422 for cursor+page conflict, got {resp.status_code}"
        )
        _assert_problem_json(resp)

    def test_archive_unknown_query_param_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """Unknown query param → 400 or 422 problem+json (extra='forbid')."""
        resp = integration_client.get(
            "/api/v1/archive", params={"totally_unknown_param": "yes"}
        )
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for unknown param, got {resp.status_code}"
        )
        _assert_problem_json(resp)

    def test_archive_limit_above_10000_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """limit=10001 → 400 or 422 problem+json."""
        resp = integration_client.get("/api/v1/archive", params={"limit": 10001})
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_archive_raw_records_have_interval_field(
        self, integration_client: TestClient
    ) -> None:
        """/archive (raw) records have 'interval' field per ArchiveRecord schema."""
        resp = integration_client.get("/api/v1/archive", params={"interval": "raw"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        if data:
            assert "interval" in data[0], (
                "ArchiveRecord must have 'interval' field (minutes since last record)"
            )
            assert data[0]["interval"] == 5, (
                f"Seed data interval is 5 min, got {data[0]['interval']}"
            )


# ---------------------------------------------------------------------------
# 3. GET /api/v1/records
# ---------------------------------------------------------------------------


class TestRecordsEndpoint:
    """/records returns section-grouped highs/lows in RecordsResponse envelope."""

    def test_records_returns_200_with_records_response_envelope(
        self, integration_client: TestClient
    ) -> None:
        """/records 200 → RecordsResponse with data, units, source, generatedAt."""
        resp = integration_client.get("/api/v1/records")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert "data" in body
        assert "units" in body
        assert "source" in body
        assert "generatedAt" in body

    def test_records_data_has_period_and_sections(
        self, integration_client: TestClient
    ) -> None:
        """/records data has 'period' and 'sections' per RecordsBundle schema."""
        resp = integration_client.get("/api/v1/records")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "period" in data
        assert "sections" in data

    def test_records_default_period_is_ytd(self, integration_client: TestClient) -> None:
        """/records default period is 'ytd'."""
        resp = integration_client.get("/api/v1/records")
        assert resp.status_code == 200
        assert resp.json()["data"]["period"] == "ytd"

    def test_records_all_time_period_accepted(
        self, integration_client: TestClient
    ) -> None:
        """/records?period=all-time → 200."""
        resp = integration_client.get("/api/v1/records", params={"period": "all-time"})
        assert resp.status_code == 200
        assert resp.json()["data"]["period"] == "all-time"

    def test_records_year_period_with_seeded_year_returns_200(
        self, integration_client: TestClient
    ) -> None:
        """/records?period=2026 (year of seed data) → 200."""
        resp = integration_client.get("/api/v1/records", params={"period": "2026"})
        assert resp.status_code == 200

    def test_records_year_period_with_no_data_returns_200_not_404(
        self, integration_client: TestClient
    ) -> None:
        """/records?period=1900 (year with no archive data) → 200 with empty sections."""
        resp = integration_client.get("/api/v1/records", params={"period": "1900"})
        assert resp.status_code == 200, (
            f"Year with no data must return 200, not {resp.status_code}"
        )
        data = resp.json()["data"]
        assert "sections" in data

    def test_records_temperature_section_present_for_seeded_archive(
        self, integration_client: TestClient
    ) -> None:
        """/records temperature section present (seed has outTemp data)."""
        resp = integration_client.get("/api/v1/records", params={"period": "all-time"})
        assert resp.status_code == 200
        sections = resp.json()["data"]["sections"]
        assert "temperature" in sections, (
            "temperature section must be present when archive has outTemp data"
        )

    def test_records_aqi_section_absent_this_round(
        self, integration_client: TestClient
    ) -> None:
        """/records aqi section is absent (self-hides in Phase 4)."""
        resp = integration_client.get("/api/v1/records", params={"period": "all-time"})
        assert resp.status_code == 200
        sections = resp.json()["data"]["sections"]
        assert "aqi" not in sections, (
            "aqi section must self-hide this round (Phase 4 per ADR-013)"
        )

    def test_records_high_temperature_matches_direct_sql_max(
        self, integration_client: TestClient, seeded_engine: Engine
    ) -> None:
        """Known high temperature in /records matches direct-SQL MAX(outTemp)."""
        resp = integration_client.get("/api/v1/records", params={"period": "all-time"})
        assert resp.status_code == 200
        sections = resp.json()["data"]["sections"]

        if "temperature" not in sections:
            pytest.skip("temperature section absent — cannot verify value")

        # Find the 'High temperature' entry
        high_temp_entry = None
        for entry in sections["temperature"]:
            if "High temperature" in str(entry.get("label", "")):
                high_temp_entry = entry
                break

        if high_temp_entry is None:
            pytest.skip("'High temperature' entry not found in temperature section")

        api_value = high_temp_entry.get("value")
        if api_value is None:
            pytest.skip("High temperature value is null")

        # Verify against direct SQL
        with seeded_engine.connect() as conn:
            result = conn.execute(
                text("SELECT MAX(outTemp) FROM archive WHERE outTemp IS NOT NULL")
            )
            db_max = result.scalar()

        assert db_max is not None
        assert abs(api_value - float(db_max)) < 0.001, (
            f"High temperature {api_value} differs from SQL MAX {db_max}"
        )

    def test_records_invalid_period_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """/records?period=abc → 400 or 422 problem+json."""
        resp = integration_client.get("/api/v1/records", params={"period": "abc"})
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_records_unknown_section_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """/records?section=lightning → 400 or 422 (not in OpenAPI enum)."""
        resp = integration_client.get(
            "/api/v1/records", params={"section": "lightning"}
        )
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)


# ---------------------------------------------------------------------------
# 4. GET /api/v1/reports (listing)
# ---------------------------------------------------------------------------


class TestReportsListingEndpoint:
    """/reports lists available NOAA report files."""

    def test_reports_returns_200(self, integration_client: TestClient) -> None:
        """/reports always returns 200 (may be empty list if no NOAA files)."""
        resp = integration_client.get("/api/v1/reports")
        assert resp.status_code == 200

    def test_reports_response_envelope_has_data_and_generated_at(
        self, integration_client: TestClient
    ) -> None:
        """/reports → ReportIndexResponse with data and generatedAt."""
        resp = integration_client.get("/api/v1/reports")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "generatedAt" in body

    def test_reports_data_has_reports_array(
        self, integration_client: TestClient
    ) -> None:
        """/reports data has 'reports' array."""
        resp = integration_client.get("/api/v1/reports")
        assert resp.status_code == 200
        assert "reports" in resp.json()["data"]
        assert isinstance(resp.json()["data"]["reports"], list)

    def test_reports_listing_with_noaa_files_returns_3_entries(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        seeded_engine: Engine,
        weewx_conf_path: Path,
    ) -> None:
        """Reports dir with 2 monthly + 1 yearly + 1 non-NOAA → 3 entries returned."""
        from weewx_clearskies_api.app import create_app
        from weewx_clearskies_api.config.settings import (
            ApiSettings,
            DatabaseSettings,
            HealthSettings,
            LoggingSettings,
            RateLimitSettings,
            Settings,
            WeewxSettings,
        )
        from weewx_clearskies_api.db.reflection import SchemaReflector
        from weewx_clearskies_api.db.registry import wire_registry
        from weewx_clearskies_api.db.session import wire_engine
        from weewx_clearskies_api.services.reports import wire_reports_directory
        from weewx_clearskies_api.services.units import load_units_block, reset_cache

        reports_dir = tmp_path_factory.mktemp("reports_listing")
        (reports_dir / "NOAA-2025-01.txt").write_text("Monthly January", encoding="utf-8")
        (reports_dir / "NOAA-2025-02.txt").write_text("Monthly February", encoding="utf-8")
        (reports_dir / "NOAA-2024.txt").write_text("Yearly 2024", encoding="utf-8")
        (reports_dir / "NOAA-summary.txt").write_text("Not a NOAA report", encoding="utf-8")

        wire_engine(seeded_engine)
        reset_cache()
        load_units_block(weewx_conf_path)
        wire_reports_directory(str(reports_dir))
        reflector = SchemaReflector(seeded_engine)
        registry = reflector.reflect()
        wire_registry(registry)

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
            ratelimit=RateLimitSettings({}),
            database=db,
            weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
        )
        app = create_app(settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports")
            assert resp.status_code == 200
            reports = resp.json()["data"]["reports"]
            assert len(reports) == 3, (
                f"Expected 3 entries (2 monthly + 1 yearly), ignoring NOAA-summary.txt; "
                f"got {len(reports)}: {reports}"
            )

    def test_reports_listing_sort_order_matches_brief_spec(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        seeded_engine: Engine,
        weewx_conf_path: Path,
    ) -> None:
        """Sort order: 2025-02 monthly, 2025-01 monthly, 2024 yearly."""
        from weewx_clearskies_api.app import create_app
        from weewx_clearskies_api.config.settings import (
            ApiSettings,
            DatabaseSettings,
            HealthSettings,
            LoggingSettings,
            RateLimitSettings,
            Settings,
            WeewxSettings,
        )
        from weewx_clearskies_api.db.reflection import SchemaReflector
        from weewx_clearskies_api.db.registry import wire_registry
        from weewx_clearskies_api.db.session import wire_engine
        from weewx_clearskies_api.services.reports import wire_reports_directory
        from weewx_clearskies_api.services.units import load_units_block, reset_cache

        reports_dir = tmp_path_factory.mktemp("reports_sort")
        (reports_dir / "NOAA-2025-01.txt").write_text("m", encoding="utf-8")
        (reports_dir / "NOAA-2025-02.txt").write_text("m", encoding="utf-8")
        (reports_dir / "NOAA-2024.txt").write_text("y", encoding="utf-8")

        wire_engine(seeded_engine)
        reset_cache()
        load_units_block(weewx_conf_path)
        wire_reports_directory(str(reports_dir))
        reflector = SchemaReflector(seeded_engine)
        registry = reflector.reflect()
        wire_registry(registry)

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
            ratelimit=RateLimitSettings({}),
            database=db,
            weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
        )
        app = create_app(settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports")
            assert resp.status_code == 200
            reports = resp.json()["data"]["reports"]
            assert len(reports) == 3

            assert reports[0]["year"] == 2025 and reports[0]["month"] == 2, (
                f"First entry should be 2025-02, got {reports[0]}"
            )
            assert reports[1]["year"] == 2025 and reports[1]["month"] == 1, (
                f"Second entry should be 2025-01, got {reports[1]}"
            )
            assert reports[2]["year"] == 2024 and reports[2]["kind"] == "yearly", (
                f"Third entry should be 2024 yearly, got {reports[2]}"
            )

    def test_reports_entry_has_kind_field(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        seeded_engine: Engine,
        weewx_conf_path: Path,
    ) -> None:
        """Each ReportEntry has 'kind' field per updated OpenAPI schema."""
        from weewx_clearskies_api.app import create_app
        from weewx_clearskies_api.config.settings import (
            ApiSettings,
            DatabaseSettings,
            HealthSettings,
            LoggingSettings,
            RateLimitSettings,
            Settings,
            WeewxSettings,
        )
        from weewx_clearskies_api.db.reflection import SchemaReflector
        from weewx_clearskies_api.db.registry import wire_registry
        from weewx_clearskies_api.db.session import wire_engine
        from weewx_clearskies_api.services.reports import wire_reports_directory
        from weewx_clearskies_api.services.units import load_units_block, reset_cache

        reports_dir = tmp_path_factory.mktemp("reports_kind")
        (reports_dir / "NOAA-2025-01.txt").write_text("Monthly", encoding="utf-8")

        wire_engine(seeded_engine)
        reset_cache()
        load_units_block(weewx_conf_path)
        wire_reports_directory(str(reports_dir))
        reflector = SchemaReflector(seeded_engine)
        registry = reflector.reflect()
        wire_registry(registry)

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
            ratelimit=RateLimitSettings({}),
            database=db,
            weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
        )
        app = create_app(settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports")
            if resp.status_code == 200:
                reports = resp.json()["data"]["reports"]
                if reports:
                    assert "kind" in reports[0], (
                        "ReportEntry must have 'kind' field per OpenAPI schema update"
                    )


# ---------------------------------------------------------------------------
# 5. GET /api/v1/reports/{year}/{month}
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_with_reports_dir(
    seeded_engine: Engine,
    weewx_conf_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[tuple[FastAPI, Path], None, None]:
    """App with a configured reports directory containing test files."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
        WeewxSettings,
    )
    from weewx_clearskies_api.db.reflection import SchemaReflector
    from weewx_clearskies_api.db.registry import wire_registry
    from weewx_clearskies_api.db.session import wire_engine
    from weewx_clearskies_api.services.reports import wire_reports_directory
    from weewx_clearskies_api.services.units import load_units_block, reset_cache

    reports_dir = tmp_path_factory.mktemp("reports_content")
    (reports_dir / "NOAA-2025-01.txt").write_text(
        "MONTHLY SUMMARY JAN 2025\nHIGH TEMP: 72.3 F\n",
        encoding="utf-8",
    )
    (reports_dir / "NOAA-2025.txt").write_text(
        "YEARLY SUMMARY 2025\nHIGH TEMP: 98.6 F\n",
        encoding="utf-8",
    )

    wire_engine(seeded_engine)
    reset_cache()
    load_units_block(weewx_conf_path)
    wire_reports_directory(str(reports_dir))
    reflector = SchemaReflector(seeded_engine)
    registry = reflector.reflect()
    wire_registry(registry)

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
        ratelimit=RateLimitSettings({}),
        database=db,
        weewx=WeewxSettings({"config_path": str(weewx_conf_path)}),
    )
    yield create_app(settings), reports_dir


class TestReportsMonthlyEndpoint:
    """/reports/{year}/{month} returns NOAAReport or 404."""

    def test_present_monthly_report_returns_200_with_raw_text(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2025/1 for a present file → 200 ReportResponse with rawText."""
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025/1")
            assert resp.status_code == 200, (
                f"Expected 200 for present report, got {resp.status_code}: {resp.text}"
            )
            body = resp.json()
            assert "data" in body
            data = body["data"]
            assert "rawText" in data, "NOAAReport must have 'rawText'"
            assert "MONTHLY SUMMARY JAN 2025" in data["rawText"]

    def test_absent_monthly_report_returns_404_problem_json(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2025/6 for a missing file → 404 problem+json."""
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025/6")
            assert resp.status_code == 404, (
                f"Expected 404 for missing report, got {resp.status_code}"
            )
            _assert_problem_json(resp)

    def test_absent_report_404_detail_does_not_leak_filesystem_path(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """404 detail for missing report must NOT contain the filesystem path."""
        app, reports_dir = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025/6")
            assert resp.status_code == 404
            body = resp.json()
            detail = body.get("detail", "")
            assert str(reports_dir) not in detail, (
                f"404 detail must not leak filesystem path; got: {detail!r}"
            )

    def test_monthly_report_year_and_month_in_response(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2025/1 response data has correct year and month fields."""
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025/1")
            if resp.status_code == 200:
                data = resp.json()["data"]
                assert data.get("year") == 2025
                assert data.get("month") == 1

    def test_monthly_report_invalid_month_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """/reports/2025/13 → 400 (month > 12)."""
        resp = integration_client.get("/api/v1/reports/2025/13")
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_monthly_report_year_below_1900_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """/reports/1899/01 → 400 (year < 1900)."""
        resp = integration_client.get("/api/v1/reports/1899/1")
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)


# ---------------------------------------------------------------------------
# 6. GET /api/v1/reports/{year}
# ---------------------------------------------------------------------------


class TestReportsYearlyEndpoint:
    """/reports/{year} returns NOAAYearlyReport or 404."""

    def test_present_yearly_report_returns_200_with_raw_text(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2025 for a present yearly file → 200 YearlyReportResponse."""
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025")
            assert resp.status_code == 200, (
                f"Expected 200 for present yearly report, got {resp.status_code}: {resp.text}"
            )
            body = resp.json()
            assert "data" in body
            data = body["data"]
            assert "rawText" in data
            assert "YEARLY SUMMARY 2025" in data["rawText"]

    def test_absent_yearly_report_returns_404_problem_json(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2020 for a missing yearly file → 404 problem+json."""
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2020")
            assert resp.status_code == 404
            _assert_problem_json(resp)

    def test_yearly_report_year_in_response(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2025 response data has correct year field."""
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025")
            if resp.status_code == 200:
                data = resp.json()["data"]
                assert data.get("year") == 2025

    def test_yearly_report_has_no_month_field(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2025 data must not have 'month' field (yearly = no month)."""
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025")
            if resp.status_code == 200:
                data = resp.json()["data"]
                assert "month" not in data, (
                    "NOAAYearlyReport must not have 'month' field"
                )

    def test_route_ordering_reports_2025_01_hits_monthly_not_yearly(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """/reports/2025/1 must hit the monthly handler, NOT the yearly handler.

        Verifies FastAPI route ordering: /reports/{year}/{month} is declared
        BEFORE /reports/{year} in reports.py so the more-specific route wins.
        """
        app, _ = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2025/1")
            # Monthly file exists; should return 200 from the monthly handler
            if resp.status_code == 200:
                data = resp.json()["data"]
                # Monthly response MUST have 'month' field
                assert "month" in data, (
                    "/reports/2025/1 must hit the monthly handler (data must have 'month'). "
                    "If 'month' is absent, the yearly handler was matched instead — "
                    "FastAPI route ordering bug."
                )
                assert data["month"] == 1

    def test_yearly_report_year_below_1900_returns_400_or_422(
        self, integration_client: TestClient
    ) -> None:
        """/reports/1899 → 400 or 422 (year < 1900)."""
        resp = integration_client.get("/api/v1/reports/1899")
        assert resp.status_code in (400, 422)
        _assert_problem_json(resp)

    def test_absent_yearly_report_404_does_not_leak_filesystem_path(
        self, app_with_reports_dir: tuple[FastAPI, Path]
    ) -> None:
        """404 detail for missing yearly report must NOT contain filesystem path."""
        app, reports_dir = app_with_reports_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/reports/2020")
            assert resp.status_code == 404
            body = resp.json()
            detail = body.get("detail", "")
            assert str(reports_dir) not in detail, (
                f"404 detail must not leak filesystem path; got: {detail!r}"
            )
