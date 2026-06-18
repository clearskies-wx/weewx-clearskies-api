"""Unit tests for the `agg` query parameter on GET /archive.

Commit ddda7ba added an `agg` override to /archive.  When interval=day the
endpoint reads from archive_day_* weewx daily summary tables; each table has
columns: min, mintime, max, maxtime, sum, count, wsum, sumtime, avg.

Previously each field used a hardcoded column from DAY_AGGREGATOR (e.g.
outTemp → avg).  Now `agg=min|max|avg|sum|count` overrides that default.
`agg` omitted preserves backward-compatibility.

Test cases (7):
  1. All 5 valid agg values accepted with interval=day → 200
  2. Invalid agg values rejected → 422
  3. Backward compatibility: interval=day, no agg → uses DAY_AGGREGATOR default
     (outTemp → avg), values match pre-change behaviour
  4. agg=min → returns daily minimum outTemp
  5. agg=max → returns daily maximum outTemp
  6. agg ignored for interval=raw → same results as without agg
  7. agg=min on interval=hour → returns hourly MIN instead of default AVG

SQLite in-memory fixtures follow the conftest.py pattern.  The archive and
archive_day_* tables use the real weewx production schema column set.

ADR references: ADR-012 (dialect), ADR-020 (UTC epoch), brief §agg-override.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Float, Integer, MetaData, Table, create_engine
from sqlalchemy.pool import StaticPool

from weewx_clearskies_api.config.settings import (
    ApiSettings,
    DatabaseSettings,
    HealthSettings,
    LoggingSettings,
    Settings,
)
from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP, ColumnInfo, ColumnRegistry
from weewx_clearskies_api.db.registry import wire_registry
from weewx_clearskies_api.db.session import wire_engine

# ---------------------------------------------------------------------------
# Epoch constants — two calendar days within a UTC window the tests control.
# Day 1: 2024-01-01 00:00:00 UTC → 2024-01-01 23:59:59 UTC
# Day 2: 2024-01-02 00:00:00 UTC → 2024-01-02 23:59:59 UTC
# Archive rows use 5-minute intervals within each day.
# ---------------------------------------------------------------------------

# start-of-day epoch values for the two test days
_DAY1_START = 1704067200  # 2024-01-01 00:00:00 UTC
_DAY2_START = 1704153600  # 2024-01-02 00:00:00 UTC

# archive row timestamps: 3 readings per day, 5 min apart (300 s each)
_DAY1_T1 = _DAY1_START + 300    # 00:05
_DAY1_T2 = _DAY1_START + 600    # 00:10
_DAY1_T3 = _DAY1_START + 900    # 00:15
_DAY2_T1 = _DAY2_START + 300
_DAY2_T2 = _DAY2_START + 600
_DAY2_T3 = _DAY2_START + 900

# outTemp values for day 1: min=60.0, max=70.0, avg=65.0, sum=195.0, count=3
_DAY1_TEMPS = [60.0, 65.0, 70.0]
# outTemp values for day 2: min=72.0, max=85.0, avg=79.0 (rounded), sum=237.0, count=3
_DAY2_TEMPS = [72.0, 80.0, 85.0]

# Known per-day aggregate values for outTemp (what archive_day_outTemp stores):
_DAY1_AGG = {"min": 60.0, "max": 70.0, "avg": 65.0, "sum": 195.0, "count": 3}
_DAY2_AGG = {"min": 72.0, "max": 85.0, "avg": 79.0, "sum": 237.0, "count": 3}

# Time window for queries — covers both test days plus buffer
_QUERY_FROM = "2024-01-01T00:00:00Z"
_QUERY_TO = "2024-01-03T00:00:00Z"


# ---------------------------------------------------------------------------
# Fixture: SQLite in-memory engine with archive + archive_day_outTemp tables
# ---------------------------------------------------------------------------


def _build_agg_test_engine():
    """Build an in-memory SQLite engine with archive + day summary tables.

    archive: 6 rows (3 per day) with varying outTemp values.
    archive_day_outTemp: 2 rows (one per day) with known min/max/avg/sum/count.

    The day summary table has the full weewx schema:
        dateTime INTEGER, min REAL, mintime INTEGER, max REAL, maxtime INTEGER,
        sum REAL, count INTEGER, wsum REAL, sumtime INTEGER
    plus an `avg` computed column (some weewx versions include it directly).

    Note: the real weewx schema stores avg as a computed column or derives it
    from sum/count; for test purposes we store it explicitly.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    meta = MetaData()

    # archive table — production schema (NOT NULL on required cols)
    archive = Table(
        "archive",
        meta,
        Column("dateTime", Integer, primary_key=True),
        Column("usUnits", Integer, nullable=False),
        Column("interval", Integer, nullable=False),
        Column("outTemp", Float, nullable=True),
    )

    # archive_day_outTemp — full weewx daily summary schema
    archive_day_outTemp = Table(  # noqa: N806 — mirroring weewx table name
        "archive_day_outTemp",
        meta,
        Column("dateTime", Integer, primary_key=True),
        Column("min", Float, nullable=True),
        Column("mintime", Integer, nullable=True),
        Column("max", Float, nullable=True),
        Column("maxtime", Integer, nullable=True),
        Column("sum", Float, nullable=True),
        Column("count", Integer, nullable=True),
        Column("wsum", Float, nullable=True),
        Column("sumtime", Integer, nullable=True),
        Column("avg", Float, nullable=True),   # weewx v4+ stores avg directly
    )

    meta.create_all(engine)

    with engine.begin() as conn:
        # Insert 3 archive rows for day 1
        conn.execute(archive.insert(), [
            {"dateTime": _DAY1_T1, "usUnits": 1, "interval": 5,
             "outTemp": _DAY1_TEMPS[0]},
            {"dateTime": _DAY1_T2, "usUnits": 1, "interval": 5,
             "outTemp": _DAY1_TEMPS[1]},
            {"dateTime": _DAY1_T3, "usUnits": 1, "interval": 5,
             "outTemp": _DAY1_TEMPS[2]},
        ])
        # Insert 3 archive rows for day 2
        conn.execute(archive.insert(), [
            {"dateTime": _DAY2_T1, "usUnits": 1, "interval": 5,
             "outTemp": _DAY2_TEMPS[0]},
            {"dateTime": _DAY2_T2, "usUnits": 1, "interval": 5,
             "outTemp": _DAY2_TEMPS[1]},
            {"dateTime": _DAY2_T3, "usUnits": 1, "interval": 5,
             "outTemp": _DAY2_TEMPS[2]},
        ])
        # Insert day-summary rows with known aggregated values
        conn.execute(archive_day_outTemp.insert(), [
            {
                "dateTime": _DAY1_START,
                "min": _DAY1_AGG["min"],
                "mintime": _DAY1_T1,
                "max": _DAY1_AGG["max"],
                "maxtime": _DAY1_T3,
                "sum": _DAY1_AGG["sum"],
                "count": _DAY1_AGG["count"],
                "wsum": _DAY1_AGG["sum"],
                "sumtime": 900,
                "avg": _DAY1_AGG["avg"],
            },
            {
                "dateTime": _DAY2_START,
                "min": _DAY2_AGG["min"],
                "mintime": _DAY2_T1,
                "max": _DAY2_AGG["max"],
                "maxtime": _DAY2_T3,
                "sum": _DAY2_AGG["sum"],
                "count": _DAY2_AGG["count"],
                "wsum": _DAY2_AGG["sum"],
                "sumtime": 900,
                "avg": _DAY2_AGG["avg"],
            },
        ])

    return engine


def _build_agg_test_registry(engine) -> ColumnRegistry:  # type: ignore[type-arg]
    """Build a minimal ColumnRegistry for the agg test engine.

    Only wires the columns present in the test tables (dateTime, usUnits,
    interval, outTemp) so the registry matches the schema.
    """
    registry = ColumnRegistry()
    registry.stock = {
        col: ColumnInfo(db_name=col, canonical_name=canon, is_stock=True)
        for col, canon in STOCK_COLUMN_MAP.items()
        if col in ("dateTime", "usUnits", "interval", "outTemp")
    }
    return registry


def _make_agg_test_settings() -> Settings:
    return Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )


@pytest.fixture()
def agg_client() -> TestClient:
    """TestClient backed by an in-memory SQLite DB with day summary data.

    Wires its own engine and registry so the autouse _wire_minimal_services
    fixture's in-memory DB (which has no day tables) does not interfere.
    The conftest autouse fixture runs first and wires a minimal DB; we
    override by calling wire_engine/wire_registry again with our richer DB.
    """
    engine = _build_agg_test_engine()
    registry = _build_agg_test_registry(engine)

    # Override the engine and registry wired by _wire_minimal_services.
    wire_engine(engine)
    wire_registry(registry)

    from weewx_clearskies_api.app import create_app
    app = create_app(_make_agg_test_settings())
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper: extract outTemp values from /archive response
# ---------------------------------------------------------------------------


def _out_temps(response_json: dict) -> list:  # type: ignore[type-arg]
    """Extract outTemp values from an /archive JSON response body.

    Response shape per OpenAPI contract (ArchiveResponse):
        {"data": [{"timestamp": ..., "outTemp": ..., ...}, ...], "page": {...}}

    Each element in data is a flat ArchiveRecord — outTemp is at the top level,
    not nested under a "data" key.
    """
    records = response_json.get("data", [])
    return [r.get("outTemp") for r in records]


# ---------------------------------------------------------------------------
# Test class 1: parameter validation
# ---------------------------------------------------------------------------


class TestAggParameterValidation:
    """agg query parameter validation: accepted values and rejection of invalid ones."""

    def test_agg_min_accepted_with_interval_day(self, agg_client: TestClient) -> None:
        """agg=min with interval=day returns 200."""
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "min",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=min with interval=day should be accepted; got {response.status_code}: "
            f"{response.text}"
        )

    def test_agg_max_accepted_with_interval_day(self, agg_client: TestClient) -> None:
        """agg=max with interval=day returns 200."""
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "max",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=max with interval=day should be accepted; got {response.status_code}: "
            f"{response.text}"
        )

    def test_agg_avg_accepted_with_interval_day(self, agg_client: TestClient) -> None:
        """agg=avg with interval=day returns 200."""
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "avg",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=avg with interval=day should be accepted; got {response.status_code}: "
            f"{response.text}"
        )

    def test_agg_sum_accepted_with_interval_day(self, agg_client: TestClient) -> None:
        """agg=sum with interval=day returns 200."""
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "sum",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=sum with interval=day should be accepted; got {response.status_code}: "
            f"{response.text}"
        )

    def test_agg_count_accepted_with_interval_day(self, agg_client: TestClient) -> None:
        """agg=count with interval=day returns 200."""
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "count",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=count with interval=day should be accepted; got {response.status_code}: "
            f"{response.text}"
        )

    def test_agg_invalid_string_rejected_with_422(self, agg_client: TestClient) -> None:
        """agg=invalid (not in allowed set) is rejected with 422."""
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "invalid",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code in (400, 422), (
            f"agg=invalid should be rejected; got {response.status_code}: {response.text}"
        )

    def test_agg_mintime_rejected_with_422(self, agg_client: TestClient) -> None:
        """agg=mintime is a raw column name, not an allowed agg value — rejected with 422.

        This is a SQL-injection guard: `mintime` is a valid archive_day_* column
        but it is not in the allowed set {min, max, avg, sum, count}.
        Accepting it would bypass the allow-list and expose an unexpected column.
        """
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "mintime",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code in (400, 422), (
            f"agg=mintime (raw column, not allowed) should be rejected; "
            f"got {response.status_code}: {response.text}"
        )

    def test_agg_wsum_rejected_with_422(self, agg_client: TestClient) -> None:
        """agg=wsum is a raw weewx internal column, not an allowed agg value — rejected.

        `wsum` appears in archive_day_* tables (weighted sum for avg computation)
        but is not a user-facing aggregation.  The allow-list must block it.
        """
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "wsum",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code in (400, 422), (
            f"agg=wsum (internal weewx column, not allowed) should be rejected; "
            f"got {response.status_code}: {response.text}"
        )


# ---------------------------------------------------------------------------
# Test class 2: backward compatibility
# ---------------------------------------------------------------------------


class TestAggBackwardCompatibility:
    """Omitting agg preserves the pre-change default behaviour (DAY_AGGREGATOR)."""

    def test_no_agg_param_uses_day_aggregator_default_for_out_temp(
        self, agg_client: TestClient
    ) -> None:
        """interval=day&fields=outTemp without agg returns avg (DAY_AGGREGATOR default).

        outTemp → 'avg' in DAY_AGGREGATOR.  The test DB stores explicit avg
        values (65.0 for day 1, 79.0 for day 2) that differ from min and max,
        so this test exercises the correct column path and not just a coincidence.
        """
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"interval=day without agg should return 200; got {response.status_code}: "
            f"{response.text}"
        )
        body = response.json()
        temps = _out_temps(body)
        assert len(temps) == 2, (
            f"Expected 2 daily records (one per day), got {len(temps)}: {temps}"
        )
        # Without agg override, outTemp uses 'avg' column — matches stored avg values.
        assert _DAY1_AGG["avg"] in temps, (
            f"Day-1 avg outTemp {_DAY1_AGG['avg']!r} missing from results {temps!r}. "
            "Without agg override, outTemp should come from the 'avg' column "
            "(DAY_AGGREGATOR default for outTemp)."
        )
        assert _DAY2_AGG["avg"] in temps, (
            f"Day-2 avg outTemp {_DAY2_AGG['avg']!r} missing from results {temps!r}."
        )
        # Confirm the result is NOT the min or max (distinguishes avg from other columns).
        assert _DAY1_AGG["min"] not in temps, (
            f"Day-1 min {_DAY1_AGG['min']!r} should NOT appear when agg is omitted "
            f"(outTemp default is avg, not min). Got temps: {temps!r}"
        )

    def test_explicit_agg_avg_matches_no_agg_default(
        self, agg_client: TestClient
    ) -> None:
        """agg=avg and no agg produce identical results for outTemp/interval=day.

        Confirms the backward-compatibility guarantee: explicitly passing the
        default value is indistinguishable from omitting agg.
        """
        no_agg_response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        explicit_avg_response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "avg",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert no_agg_response.status_code == 200
        assert explicit_avg_response.status_code == 200

        no_agg_temps = _out_temps(no_agg_response.json())
        avg_temps = _out_temps(explicit_avg_response.json())
        assert no_agg_temps == avg_temps, (
            f"agg=avg should produce same results as no-agg (outTemp default is avg). "
            f"no-agg: {no_agg_temps!r}, agg=avg: {avg_temps!r}"
        )


# ---------------------------------------------------------------------------
# Test class 3: agg=min and agg=max on interval=day
# ---------------------------------------------------------------------------


class TestAggMinMaxOnIntervalDay:
    """agg=min / agg=max return the correct per-day column from archive_day_* tables."""

    def test_agg_min_interval_day_returns_daily_minimum_out_temp(
        self, agg_client: TestClient
    ) -> None:
        """agg=min&interval=day returns the daily minimum outTemp from archive_day_outTemp.min.

        Test data:
          Day 1 min = 60.0 (stored in archive_day_outTemp.min)
          Day 2 min = 72.0
        These differ from both avg and max, so the assertion is unambiguous.
        """
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "min",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=min&interval=day should succeed; got {response.status_code}: "
            f"{response.text}"
        )
        body = response.json()
        temps = _out_temps(body)
        assert len(temps) == 2, (
            f"Expected 2 daily records, got {len(temps)}: {temps}"
        )
        assert _DAY1_AGG["min"] in temps, (
            f"Day-1 daily minimum {_DAY1_AGG['min']!r} missing from agg=min results {temps!r}. "
            "The endpoint must read archive_day_outTemp.min when agg=min."
        )
        assert _DAY2_AGG["min"] in temps, (
            f"Day-2 daily minimum {_DAY2_AGG['min']!r} missing from agg=min results {temps!r}."
        )
        # Confirm avg values are NOT returned (distinguishes min from avg).
        assert _DAY1_AGG["avg"] not in temps, (
            f"Day-1 avg {_DAY1_AGG['avg']!r} should NOT appear with agg=min. "
            f"Got: {temps!r}"
        )

    def test_agg_max_interval_day_returns_daily_maximum_out_temp(
        self, agg_client: TestClient
    ) -> None:
        """agg=max&interval=day returns the daily maximum outTemp from archive_day_outTemp.max.

        Test data:
          Day 1 max = 70.0 (stored in archive_day_outTemp.max)
          Day 2 max = 85.0
        """
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "max",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=max&interval=day should succeed; got {response.status_code}: "
            f"{response.text}"
        )
        body = response.json()
        temps = _out_temps(body)
        assert len(temps) == 2, (
            f"Expected 2 daily records, got {len(temps)}: {temps}"
        )
        assert _DAY1_AGG["max"] in temps, (
            f"Day-1 daily maximum {_DAY1_AGG['max']!r} missing from agg=max results {temps!r}. "
            "The endpoint must read archive_day_outTemp.max when agg=max."
        )
        assert _DAY2_AGG["max"] in temps, (
            f"Day-2 daily maximum {_DAY2_AGG['max']!r} missing from agg=max results {temps!r}."
        )
        # Confirm avg values are NOT returned (distinguishes max from avg).
        assert _DAY1_AGG["avg"] not in temps, (
            f"Day-1 avg {_DAY1_AGG['avg']!r} should NOT appear with agg=max. "
            f"Got: {temps!r}"
        )

    def test_agg_sum_interval_day_returns_daily_sum(
        self, agg_client: TestClient
    ) -> None:
        """agg=sum&interval=day returns the daily sum from archive_day_outTemp.sum.

        Day 1 sum = 195.0, Day 2 sum = 237.0.  Distinct from avg/min/max.
        """
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "day",
                "fields": "outTemp",
                "agg": "sum",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200
        body = response.json()
        temps = _out_temps(body)
        assert len(temps) == 2
        assert _DAY1_AGG["sum"] in temps, (
            f"Day-1 sum {_DAY1_AGG['sum']!r} missing from agg=sum results {temps!r}."
        )
        assert _DAY2_AGG["sum"] in temps, (
            f"Day-2 sum {_DAY2_AGG['sum']!r} missing from agg=sum results {temps!r}."
        )


# ---------------------------------------------------------------------------
# Test class 4: agg ignored for interval=raw
# ---------------------------------------------------------------------------


class TestAggIgnoredForIntervalRaw:
    """agg parameter is silently ignored when interval=raw."""

    def test_agg_min_with_interval_raw_returns_200(
        self, agg_client: TestClient
    ) -> None:
        """interval=raw&agg=min returns 200 (agg is accepted but ignored for raw)."""
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "raw",
                "fields": "outTemp",
                "agg": "min",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"interval=raw&agg=min should return 200; got {response.status_code}: "
            f"{response.text}"
        )

    def test_agg_ignored_for_interval_raw_produces_same_results_as_no_agg(
        self, agg_client: TestClient
    ) -> None:
        """interval=raw&agg=min and interval=raw (no agg) produce identical records.

        For raw mode, the endpoint reads directly from the archive table — no
        aggregation is performed.  The agg parameter must not change the result.
        """
        no_agg_response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "raw",
                "fields": "outTemp",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        with_agg_response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "raw",
                "fields": "outTemp",
                "agg": "min",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert no_agg_response.status_code == 200
        assert with_agg_response.status_code == 200

        no_agg_temps = _out_temps(no_agg_response.json())
        agg_temps = _out_temps(with_agg_response.json())

        assert no_agg_temps == agg_temps, (
            f"agg must be ignored for interval=raw. "
            f"no-agg temps: {no_agg_temps!r}, agg=min temps: {agg_temps!r}"
        )
        # Also verify we got the raw archive values (individual readings), not aggregates.
        assert len(no_agg_temps) == 6, (
            f"interval=raw should return all 6 archive rows, got {len(no_agg_temps)}"
        )


# ---------------------------------------------------------------------------
# Test class 5: agg=min on interval=hour
# ---------------------------------------------------------------------------


class TestAggMinOnIntervalHour:
    """agg=min on interval=hour applies MIN() instead of the default AVG()."""

    def test_agg_min_interval_hour_returns_hourly_minimum(
        self, agg_client: TestClient
    ) -> None:
        """agg=min&interval=hour uses MIN(outTemp) GROUP BY hour instead of AVG.

        Test data has 3 readings within the same hour (00:05, 00:10, 00:15)
        for each day:
          Day 1: [60.0, 65.0, 70.0] → AVG=65.0, MIN=60.0
          Day 2: [72.0, 80.0, 85.0] → AVG=79.0, MIN=72.0

        With agg=min the hourly bucket must produce 60.0 and 72.0, NOT 65.0/79.0.
        """
        response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "hour",
                "fields": "outTemp",
                "agg": "min",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert response.status_code == 200, (
            f"agg=min&interval=hour should return 200; got {response.status_code}: "
            f"{response.text}"
        )
        body = response.json()
        temps = _out_temps(body)
        assert len(temps) == 2, (
            f"Expected 2 hourly buckets (one per day, all readings in the same hour), "
            f"got {len(temps)}: {temps}"
        )
        # With agg=min each bucket uses MIN(), not AVG().
        assert _DAY1_TEMPS[0] in temps, (
            f"Hour-1 MIN outTemp {_DAY1_TEMPS[0]!r} ({_DAY1_TEMPS[0]}) missing from "
            f"agg=min&interval=hour results {temps!r}. "
            "The endpoint must apply MIN() for the hourly GROUP BY when agg=min."
        )
        assert _DAY2_TEMPS[0] in temps, (
            f"Hour-2 MIN outTemp {_DAY2_TEMPS[0]!r} missing from agg=min results {temps!r}."
        )

    def test_agg_min_interval_hour_differs_from_default_avg(
        self, agg_client: TestClient
    ) -> None:
        """agg=min hourly values differ from the default AVG — confirms override fires.

        If agg=min returned the same values as no-agg, the override would be a
        no-op; this test confirms the two paths produce different numbers.
        """
        avg_response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "hour",
                "fields": "outTemp",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        min_response = agg_client.get(
            "/api/v1/archive",
            params={
                "interval": "hour",
                "fields": "outTemp",
                "agg": "min",
                "from": _QUERY_FROM,
                "to": _QUERY_TO,
            },
        )
        assert avg_response.status_code == 200
        assert min_response.status_code == 200

        avg_temps = _out_temps(avg_response.json())
        min_temps = _out_temps(min_response.json())

        assert avg_temps != min_temps, (
            f"agg=min hourly should differ from default AVG. "
            f"avg temps: {avg_temps!r}, min temps: {min_temps!r}. "
            "If they are equal, the agg override is not being applied."
        )
