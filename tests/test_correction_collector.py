"""Tests for weewx_clearskies_api/correction/collector.py (ADR-079 Phase 2).

Validates the ForecastCollector daemon thread:
- Pair written with correct features from archive + forecast data.
- Guard conditions (empty archive, null outTemp, missing bundle, empty hourly).
- Duplicate timestamp handling (INSERT OR IGNORE).
- None feature values stored correctly.
- collection_enabled runtime gate.
- Closest hourly point selection by validTime proximity.

Uses in-memory SQLite for both the archive engine and correction DB, following
the pattern established in test_correction_db.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import Column, Float, Integer, MetaData, Table, create_engine
from sqlalchemy.pool import StaticPool

from weewx_clearskies_api.correction import db as correction_db
from weewx_clearskies_api.correction import collector as collector_mod
from weewx_clearskies_api.correction.collector import ForecastCollector, set_collection_enabled


# ---------------------------------------------------------------------------
# Mock data shapes
# ---------------------------------------------------------------------------


class MockHourlyPoint:
    """Minimal stand-in for a HourlyForecastPoint."""

    def __init__(
        self,
        validTime: str,
        outTemp: float | None,
        windDir: float | None = 270.0,
        outHumidity: float | None = 65.0,
        cloudCover: float | None = 40.0,
        windSpeed: float | None = 12.0,
    ) -> None:
        self.validTime = validTime
        self.outTemp = outTemp
        self.windDir = windDir
        self.outHumidity = outHumidity
        self.cloudCover = cloudCover
        self.windSpeed = windSpeed


class MockForecastBundle:
    """Minimal stand-in for a ForecastBundle."""

    def __init__(self, hourly: list[MockHourlyPoint] | None = None) -> None:
        self.hourly = hourly if hourly is not None else []


class MockForecastSettings:
    """Minimal forecast settings bag used by ForecastCollector."""

    def __init__(self, provider: str = "openmeteo") -> None:
        self.provider = provider
        self.nws_user_agent_contact = "test@example.com"
        self.aeris_client_id = ""
        self.aeris_client_secret = ""
        self.aeris_forecast_model = "standard"
        self.openweathermap_appid = ""
        self.wunderground_api_key = ""
        self.wunderground_pws_station_id = ""


class MockStationInfo:
    """Minimal station info bag used by ForecastCollector."""

    def __init__(
        self,
        latitude: float = 42.375,
        longitude: float = -72.519,
        timezone: str = "America/Los_Angeles",
        unit_system: str = "us",
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.timezone = timezone
        self.unit_system = unit_system


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_archive_engine() -> Any:
    """Return a minimal in-memory SQLite archive engine with the archive table."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    meta = MetaData()
    Table(
        "archive",
        meta,
        Column("dateTime", Integer, primary_key=True),
        Column("usUnits", Integer, nullable=False),
        Column("interval", Integer, nullable=False),
        Column("outTemp", Float, nullable=True),
    )
    meta.create_all(engine)
    return engine


def _make_correction_engine() -> Any:
    """Return an in-memory SQLite correction engine."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    correction_db.wire_engine(engine)
    correction_db._create_tables(engine)
    return engine


@pytest.fixture()
def archive_engine():
    """Fresh in-memory archive engine for each test."""
    return _make_archive_engine()


@pytest.fixture()
def correction_engine():
    """Fresh in-memory correction engine; resets module state on teardown."""
    engine = _make_correction_engine()
    yield engine
    correction_db._engine = None


@pytest.fixture()
def collector(archive_engine, correction_engine):
    """ForecastCollector instance wired to in-memory engines."""
    return ForecastCollector(
        engine=archive_engine,
        settings=object(),  # Not used directly in _collect_one
        forecast_settings=MockForecastSettings(provider="openmeteo"),
        station_info=MockStationInfo(),
        archive_interval=300,
    )


@pytest.fixture(autouse=True)
def _reset_collection_gate():
    """Restore collection_enabled=True after each test."""
    yield
    set_collection_enabled(True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_archive_record(engine: Any, ts: int, out_temp: float | None) -> None:
    """Insert a single row into the archive table."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO archive (dateTime, usUnits, interval, outTemp) "
                "VALUES (:ts, 1, 300, :temp)"
            ),
            {"ts": ts, "temp": out_temp},
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Test: pair written with correct features
# ---------------------------------------------------------------------------


class TestPairWrittenWithCorrectFeatures:
    def test_pair_written_with_all_features(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """_collect_one() writes a pair with correct feature values from archive + forecast.

        Archive record: ts=1_750_000_000, outTemp=21.5 (in UTC → month=6, hour=...).
        Forecast bundle: single hourly point at that ts with known feature values.
        """
        archive_ts = 1_750_000_000
        _insert_archive_record(archive_engine, archive_ts, 21.5)

        # validTime matches archive_ts exactly.
        dt_utc = datetime.fromtimestamp(archive_ts, tz=timezone.utc)
        valid_time_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(
                validTime=valid_time_str,
                outTemp=23.0,
                windDir=180.0,
                outHumidity=70.0,
                cloudCover=50.0,
                windSpeed=15.0,
            )
        ])

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        assert correction_db.get_pair_count() == 1

        # Verify stored features.
        from sqlalchemy import text
        with correction_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT timestamp, fcst_temp, fcst_wind_dir, fcst_humidity, "
                    "fcst_cloud_cover, fcst_wind_speed, actual_temp, month, hour "
                    "FROM forecast_observation_pairs LIMIT 1"
                )
            ).fetchone()

        assert row is not None
        assert row[0] == archive_ts
        assert row[1] == pytest.approx(23.0)   # fcst_temp
        assert row[2] == pytest.approx(180.0)  # fcst_wind_dir
        assert row[3] == pytest.approx(70.0)   # fcst_humidity
        assert row[4] == pytest.approx(50.0)   # fcst_cloud_cover
        assert row[5] == pytest.approx(15.0)   # fcst_wind_speed
        assert row[6] == pytest.approx(21.5)   # actual_temp
        # month and hour extracted from archive_ts in station timezone.
        assert row[7] in range(1, 13)           # month in valid range
        assert row[8] in range(0, 24)           # hour in valid range


# ---------------------------------------------------------------------------
# Test: missing archive record → no pair
# ---------------------------------------------------------------------------


class TestMissingArchiveRecord:
    def test_empty_archive_table_produces_no_pair(
        self, collector: ForecastCollector, correction_engine: Any
    ) -> None:
        """Empty archive table: _collect_one() skips tick, no pair written."""
        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(validTime="2026-06-30T12:00:00Z", outTemp=22.0)
        ])

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        assert correction_db.get_pair_count() == 0


# ---------------------------------------------------------------------------
# Test: null outTemp in archive → no pair
# ---------------------------------------------------------------------------


class TestNullOutTempInArchive:
    def test_null_out_temp_in_archive_produces_no_pair(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """Archive record with outTemp=None: _collect_one() skips tick."""
        _insert_archive_record(archive_engine, 1_750_000_100, None)

        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(validTime="2026-06-30T12:00:00Z", outTemp=22.0)
        ])

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        assert correction_db.get_pair_count() == 0


# ---------------------------------------------------------------------------
# Test: missing forecast bundle → no pair
# ---------------------------------------------------------------------------


class TestMissingForecastBundle:
    def test_none_forecast_bundle_produces_no_pair(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """Provider returns None: _collect_one() skips tick."""
        _insert_archive_record(archive_engine, 1_750_000_200, 20.5)

        with patch.object(collector, "_fetch_forecast_bundle", return_value=None):
            collector._collect_one()

        assert correction_db.get_pair_count() == 0


# ---------------------------------------------------------------------------
# Test: empty hourly list → no pair
# ---------------------------------------------------------------------------


class TestEmptyHourlyList:
    def test_empty_hourly_list_produces_no_pair(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """Bundle with hourly=[] skips tick without error."""
        _insert_archive_record(archive_engine, 1_750_000_300, 19.0)

        bundle = MockForecastBundle(hourly=[])

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        assert correction_db.get_pair_count() == 0


# ---------------------------------------------------------------------------
# Test: duplicate timestamp → no error, still 1 pair
# ---------------------------------------------------------------------------


class TestDuplicateTimestamp:
    def test_duplicate_archive_timestamp_produces_one_pair(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """INSERT OR IGNORE: two _collect_one() calls at same archive ts → 1 pair."""
        archive_ts = 1_750_000_400
        _insert_archive_record(archive_engine, archive_ts, 18.0)

        dt_utc = datetime.fromtimestamp(archive_ts, tz=timezone.utc)
        valid_time_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(validTime=valid_time_str, outTemp=19.0)
        ])

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()
            collector._collect_one()

        assert correction_db.get_pair_count() == 1, (
            "Duplicate timestamp should be silently ignored; got more than one pair"
        )


# ---------------------------------------------------------------------------
# Test: None feature values stored correctly
# ---------------------------------------------------------------------------


class TestNoneFeatureValues:
    def test_none_wind_dir_and_cloud_cover_stored_as_null(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """Hourly point with windDir=None and cloudCover=None stores NULL in DB."""
        archive_ts = 1_750_000_500
        _insert_archive_record(archive_engine, archive_ts, 15.0)

        dt_utc = datetime.fromtimestamp(archive_ts, tz=timezone.utc)
        valid_time_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(
                validTime=valid_time_str,
                outTemp=16.0,
                windDir=None,
                outHumidity=55.0,
                cloudCover=None,
                windSpeed=8.0,
            )
        ])

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        from sqlalchemy import text
        with correction_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT fcst_wind_dir, fcst_cloud_cover "
                    "FROM forecast_observation_pairs LIMIT 1"
                )
            ).fetchone()

        assert row is not None
        assert row[0] is None, f"fcst_wind_dir should be NULL, got {row[0]}"
        assert row[1] is None, f"fcst_cloud_cover should be NULL, got {row[1]}"


# ---------------------------------------------------------------------------
# Test: collection_enabled gate
# ---------------------------------------------------------------------------


class TestCollectionEnabledGate:
    def test_disabled_collection_produces_no_pair(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """When collection_enabled=False, _collect_one() returns early without writing."""
        archive_ts = 1_750_000_600
        _insert_archive_record(archive_engine, archive_ts, 17.0)

        dt_utc = datetime.fromtimestamp(archive_ts, tz=timezone.utc)
        valid_time_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(validTime=valid_time_str, outTemp=18.0)
        ])

        set_collection_enabled(False)

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        assert correction_db.get_pair_count() == 0, (
            "collection_enabled=False must gate the entire tick"
        )

    def test_re_enabled_collection_writes_pair(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """After re-enabling collection, _collect_one() proceeds normally."""
        archive_ts = 1_750_000_700
        _insert_archive_record(archive_engine, archive_ts, 14.0)

        dt_utc = datetime.fromtimestamp(archive_ts, tz=timezone.utc)
        valid_time_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(validTime=valid_time_str, outTemp=15.0)
        ])

        set_collection_enabled(False)
        set_collection_enabled(True)

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        assert correction_db.get_pair_count() == 1


# ---------------------------------------------------------------------------
# Test: closest hourly point selection
# ---------------------------------------------------------------------------


class TestClosestHourlyPointSelection:
    def test_closest_hourly_point_by_valid_time(
        self, collector: ForecastCollector, archive_engine: Any, correction_engine: Any
    ) -> None:
        """Three hourly points at 10:00, 11:00, 12:00 UTC; archive at 10:45 → 11:00 selected.

        The archive timestamp is 10:45 UTC. The closest valid hourly point is 11:00
        (15 min away) not 10:00 (45 min away) or 12:00 (75 min away).
        """
        # 2026-06-30 at 10:45:00 UTC
        archive_ts = int(datetime(2026, 6, 30, 10, 45, 0, tzinfo=timezone.utc).timestamp())
        _insert_archive_record(archive_engine, archive_ts, 25.0)

        bundle = MockForecastBundle(hourly=[
            MockHourlyPoint(validTime="2026-06-30T10:00:00Z", outTemp=20.0),
            MockHourlyPoint(validTime="2026-06-30T11:00:00Z", outTemp=22.0),  # closest
            MockHourlyPoint(validTime="2026-06-30T12:00:00Z", outTemp=24.0),
        ])

        with patch.object(collector, "_fetch_forecast_bundle", return_value=bundle):
            collector._collect_one()

        from sqlalchemy import text
        with correction_engine.connect() as conn:
            row = conn.execute(
                text("SELECT fcst_temp FROM forecast_observation_pairs LIMIT 1")
            ).fetchone()

        assert row is not None, "Expected one pair to be written"
        assert row[0] == pytest.approx(22.0), (
            f"Expected fcst_temp from 11:00 point (22.0), got {row[0]}"
        )


# ---------------------------------------------------------------------------
# Test: find_closest_hourly static method
# ---------------------------------------------------------------------------


class TestFindClosestHourly:
    def test_static_method_returns_none_when_all_valid_times_unparseable(self) -> None:
        """_find_closest_hourly() returns None when all validTime strings are bad."""
        points = [
            MockHourlyPoint(validTime="not-a-date", outTemp=10.0),
            MockHourlyPoint(validTime="", outTemp=11.0),
        ]
        result = ForecastCollector._find_closest_hourly(points, 1_750_000_000)
        assert result is None

    def test_static_method_skips_point_with_none_valid_time(self) -> None:
        """_find_closest_hourly() skips points with validTime=None."""
        ts = int(datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        points = [
            MockHourlyPoint(validTime=None, outTemp=10.0),  # type: ignore[arg-type]
            MockHourlyPoint(validTime="2026-06-30T12:00:00Z", outTemp=20.0),
        ]
        result = ForecastCollector._find_closest_hourly(points, ts)
        assert result is not None
        assert result.outTemp == pytest.approx(20.0)
