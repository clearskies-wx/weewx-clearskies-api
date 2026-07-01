"""Tests for weewx_clearskies_api/correction/db.py (ADR-079 Phase 1).

Validates the correction DB persistence layer: schema creation, CRUD operations,
purge, counts, date range queries, model metadata round-trip, and WAL mode.

Uses in-memory SQLite via StaticPool per the conftest.py pattern so no filesystem
access is required and every test starts from a clean state.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from weewx_clearskies_api.correction import db as correction_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def correction_engine():
    """In-memory SQLite engine for correction DB tests.

    Wires the module-level engine, creates tables, and resets module state on
    teardown so tests are fully independent.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    correction_db.wire_engine(engine)
    correction_db._create_tables(engine)
    yield engine
    # Reset module-level state so the next test gets a fresh engine
    correction_db._engine = None


def _insert_pair(
    *,
    timestamp: int,
    provider_id: str = "openmeteo",
    month: int = 6,
    hour: int = 14,
    day_of_year: int = 180,
    fcst_temp: float = 22.5,
    fcst_wind_dir: float | None = 270.0,
    fcst_humidity: float | None = 65.0,
    fcst_cloud_cover: float | None = 40.0,
    fcst_wind_speed: float | None = 12.0,
    actual_temp: float = 21.8,
) -> None:
    """Helper — thin wrapper around correction_db.insert_pair with sensible defaults."""
    correction_db.insert_pair(
        timestamp=timestamp,
        provider_id=provider_id,
        month=month,
        hour=hour,
        day_of_year=day_of_year,
        fcst_temp=fcst_temp,
        fcst_wind_dir=fcst_wind_dir,
        fcst_humidity=fcst_humidity,
        fcst_cloud_cover=fcst_cloud_cover,
        fcst_wind_speed=fcst_wind_speed,
        actual_temp=actual_temp,
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchemaCreation:
    def test_create_tables_is_idempotent(self, correction_engine) -> None:
        """Calling _create_tables() twice raises no error (IF NOT EXISTS is honoured)."""
        # First call already happened in the fixture; a second call must not raise.
        correction_db._create_tables(correction_engine)

    def test_both_tables_exist_after_init(self, correction_engine) -> None:
        """Both forecast_observation_pairs and model_metadata tables are present."""
        with correction_engine.connect() as conn:
            pairs_result = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='forecast_observation_pairs'"
                )
            )
            assert pairs_result.fetchone() is not None, (
                "forecast_observation_pairs table missing after _create_tables()"
            )

            meta_result = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='model_metadata'"
                )
            )
            assert meta_result.fetchone() is not None, (
                "model_metadata table missing after _create_tables()"
            )


# ---------------------------------------------------------------------------
# insert_pair tests
# ---------------------------------------------------------------------------


class TestInsertPair:
    def test_insert_pair_stores_all_fields(self, correction_engine) -> None:
        """insert_pair() persists every column; SELECT returns matching values."""
        _insert_pair(
            timestamp=1_700_000_000,
            provider_id="nws",
            month=11,
            hour=8,
            day_of_year=305,
            fcst_temp=5.0,
            fcst_wind_dir=90.0,
            fcst_humidity=80.0,
            fcst_cloud_cover=75.0,
            fcst_wind_speed=20.0,
            actual_temp=4.2,
        )

        with correction_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT timestamp, provider_id, month, hour, day_of_year, "
                    "fcst_temp, fcst_wind_dir, fcst_humidity, fcst_cloud_cover, "
                    "fcst_wind_speed, actual_temp "
                    "FROM forecast_observation_pairs WHERE timestamp = 1700000000"
                )
            ).fetchone()

        assert row is not None, "Row not found after insert"
        assert row[0] == 1_700_000_000   # timestamp
        assert row[1] == "nws"           # provider_id
        assert row[2] == 11              # month
        assert row[3] == 8               # hour
        assert row[4] == 305             # day_of_year
        assert row[5] == pytest.approx(5.0)   # fcst_temp
        assert row[6] == pytest.approx(90.0)  # fcst_wind_dir
        assert row[7] == pytest.approx(80.0)  # fcst_humidity
        assert row[8] == pytest.approx(75.0)  # fcst_cloud_cover
        assert row[9] == pytest.approx(20.0)  # fcst_wind_speed
        assert row[10] == pytest.approx(4.2)  # actual_temp

    def test_insert_pair_ignores_duplicate_timestamp(self, correction_engine) -> None:
        """INSERT OR IGNORE: inserting the same timestamp twice leaves exactly one row."""
        _insert_pair(timestamp=1_750_000_000, fcst_temp=15.0, actual_temp=14.5)
        _insert_pair(timestamp=1_750_000_000, fcst_temp=99.9, actual_temp=99.0)

        with correction_engine.connect() as conn:
            count_row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM forecast_observation_pairs "
                    "WHERE timestamp = 1750000000"
                )
            ).fetchone()

        assert count_row[0] == 1, (
            "Duplicate timestamp should be silently ignored; got more than one row"
        )

    def test_insert_pair_nullable_features_stored_as_none(self, correction_engine) -> None:
        """Nullable feature columns (wind_dir, humidity, cloud_cover, wind_speed) accept None."""
        correction_db.insert_pair(
            timestamp=1_760_000_000,
            provider_id="openmeteo",
            month=3,
            hour=6,
            day_of_year=75,
            fcst_temp=8.0,
            fcst_wind_dir=None,
            fcst_humidity=None,
            fcst_cloud_cover=None,
            fcst_wind_speed=None,
            actual_temp=7.5,
        )

        with correction_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT fcst_wind_dir, fcst_humidity, fcst_cloud_cover, fcst_wind_speed "
                    "FROM forecast_observation_pairs WHERE timestamp = 1760000000"
                )
            ).fetchone()

        assert row is not None, "Row not found after insert with None features"
        assert row[0] is None, f"fcst_wind_dir should be None, got {row[0]}"
        assert row[1] is None, f"fcst_humidity should be None, got {row[1]}"
        assert row[2] is None, f"fcst_cloud_cover should be None, got {row[2]}"
        assert row[3] is None, f"fcst_wind_speed should be None, got {row[3]}"


# ---------------------------------------------------------------------------
# get_training_data / get_validation_data tests
# ---------------------------------------------------------------------------


class TestTrainingValidationSplit:
    """Tests the training/validation cutoff split (30-day boundary pattern)."""

    def _populate_pairs_around_cutoff(self, cutoff: int) -> None:
        """Insert three pairs before cutoff and two at/after cutoff."""
        # Before cutoff (training)
        _insert_pair(timestamp=cutoff - 3600, fcst_temp=10.0, actual_temp=9.0)
        _insert_pair(timestamp=cutoff - 7200, fcst_temp=11.0, actual_temp=10.5)
        _insert_pair(timestamp=cutoff - 100, fcst_temp=12.0, actual_temp=11.5)
        # At or after cutoff (validation)
        _insert_pair(timestamp=cutoff, fcst_temp=13.0, actual_temp=12.5)
        _insert_pair(timestamp=cutoff + 3600, fcst_temp=14.0, actual_temp=13.5)

    def test_get_training_data_returns_pairs_older_than_cutoff(
        self, correction_engine
    ) -> None:
        """get_training_data(cutoff) returns only pairs with timestamp < cutoff."""
        cutoff = 1_800_000_000
        self._populate_pairs_around_cutoff(cutoff)

        rows = correction_db.get_training_data(cutoff)

        timestamps = [r["timestamp"] for r in rows]
        assert all(ts < cutoff for ts in timestamps), (
            f"Training rows should all have timestamp < {cutoff}; got {timestamps}"
        )
        assert len(rows) == 3

    def test_get_validation_data_returns_pairs_at_or_after_cutoff(
        self, correction_engine
    ) -> None:
        """get_validation_data(cutoff) returns only pairs with timestamp >= cutoff."""
        cutoff = 1_800_000_000
        self._populate_pairs_around_cutoff(cutoff)

        rows = correction_db.get_validation_data(cutoff)

        timestamps = [r["timestamp"] for r in rows]
        assert all(ts >= cutoff for ts in timestamps), (
            f"Validation rows should all have timestamp >= {cutoff}; got {timestamps}"
        )
        assert len(rows) == 2

    def test_training_and_validation_sets_are_disjoint(self, correction_engine) -> None:
        """Training and validation sets share no timestamps (non-overlapping split)."""
        cutoff = 1_810_000_000
        self._populate_pairs_around_cutoff(cutoff)

        training_ts = {r["timestamp"] for r in correction_db.get_training_data(cutoff)}
        validation_ts = {r["timestamp"] for r in correction_db.get_validation_data(cutoff)}

        overlap = training_ts & validation_ts
        assert not overlap, f"Training and validation sets overlap at: {overlap}"


# ---------------------------------------------------------------------------
# purge_old_records tests
# ---------------------------------------------------------------------------


class TestPurgeOldRecords:
    def test_purge_deletes_old_records_and_keeps_recent(self, correction_engine) -> None:
        """purge_old_records(cutoff) removes old pairs and retains recent ones."""
        cutoff = 1_900_000_000

        # Insert 3 old pairs (before cutoff) and 2 recent pairs (at/after cutoff)
        for delta in (86400, 172800, 259200):
            _insert_pair(timestamp=cutoff - delta)
        _insert_pair(timestamp=cutoff, fcst_temp=20.0, actual_temp=19.5)
        _insert_pair(timestamp=cutoff + 3600, fcst_temp=21.0, actual_temp=20.5)

        deleted = correction_db.purge_old_records(cutoff)

        assert deleted == 3, f"Expected 3 deletions, got {deleted}"
        assert correction_db.get_pair_count() == 2, (
            "Two recent pairs should survive purge"
        )

    def test_purge_returns_zero_when_nothing_to_delete(self, correction_engine) -> None:
        """purge_old_records() returns 0 when all records are newer than cutoff."""
        cutoff = 1_000_000_000  # far in the past

        _insert_pair(timestamp=1_900_000_100)
        _insert_pair(timestamp=1_900_000_200)

        deleted = correction_db.purge_old_records(cutoff)
        assert deleted == 0


# ---------------------------------------------------------------------------
# get_pair_count tests
# ---------------------------------------------------------------------------


class TestGetPairCount:
    def test_pair_count_reflects_inserted_rows(self, correction_engine) -> None:
        """get_pair_count() returns the exact number of inserted pairs."""
        n = 7
        for i in range(n):
            _insert_pair(timestamp=1_850_000_000 + i * 3600)

        assert correction_db.get_pair_count() == n

    def test_pair_count_zero_on_empty_table(self, correction_engine) -> None:
        """get_pair_count() returns 0 when no pairs have been inserted."""
        assert correction_db.get_pair_count() == 0


# ---------------------------------------------------------------------------
# get_date_range tests
# ---------------------------------------------------------------------------


class TestGetDateRange:
    def test_date_range_returns_min_and_max(self, correction_engine) -> None:
        """get_date_range() returns the exact MIN and MAX timestamps."""
        timestamps = [1_700_000_000, 1_710_000_000, 1_720_000_000]
        for ts in timestamps:
            _insert_pair(timestamp=ts)

        min_ts, max_ts = correction_db.get_date_range()
        assert min_ts == min(timestamps)
        assert max_ts == max(timestamps)

    def test_date_range_returns_none_none_when_empty(self, correction_engine) -> None:
        """get_date_range() returns (None, None) when the table is empty."""
        min_ts, max_ts = correction_db.get_date_range()
        assert min_ts is None
        assert max_ts is None


# ---------------------------------------------------------------------------
# save_model_metadata / get_model_metadata tests
# ---------------------------------------------------------------------------


class TestModelMetadata:
    def test_get_model_metadata_returns_none_when_no_metadata(
        self, correction_engine
    ) -> None:
        """get_model_metadata() returns None when no metadata row exists."""
        result = correction_db.get_model_metadata()
        assert result is None

    def test_save_and_get_model_metadata_round_trip(self, correction_engine) -> None:
        """save_model_metadata() persists all fields; get_model_metadata() reads them back."""
        correction_db.save_model_metadata(
            last_trained="2026-06-30T12:00:00Z",
            sample_count=1234,
            mae_raw=2.75,
            mae_corrected=1.50,
            provider_score=97.25,
            correction_pct=45.5,
            model_path="/etc/weewx-clearskies/forecast_correction_model.pkl",
            training_status="idle",
        )

        meta = correction_db.get_model_metadata()

        assert meta is not None, "Expected metadata row, got None"
        assert meta["id"] == 1
        assert meta["last_trained"] == "2026-06-30T12:00:00Z"
        assert meta["sample_count"] == 1234
        assert meta["mae_raw"] == pytest.approx(2.75)
        assert meta["mae_corrected"] == pytest.approx(1.50)
        assert meta["provider_score"] == pytest.approx(97.25)
        assert meta["correction_pct"] == pytest.approx(45.5)
        assert meta["model_path"] == "/etc/weewx-clearskies/forecast_correction_model.pkl"
        assert meta["training_status"] == "idle"

    def test_save_model_metadata_singleton_constraint_keeps_latest_values(
        self, correction_engine
    ) -> None:
        """Calling save_model_metadata() twice keeps exactly one row with the latest values."""
        correction_db.save_model_metadata(
            last_trained="2026-01-01T00:00:00Z",
            sample_count=500,
            mae_raw=3.0,
            mae_corrected=2.0,
            provider_score=97.0,
            correction_pct=33.3,
            model_path="/old/path/model.pkl",
            training_status="idle",
        )
        correction_db.save_model_metadata(
            last_trained="2026-06-30T12:00:00Z",
            sample_count=1500,
            mae_raw=2.5,
            mae_corrected=1.2,
            provider_score=97.5,
            correction_pct=52.0,
            model_path="/new/path/model.pkl",
            training_status="idle",
        )

        with correction_engine.connect() as conn:
            count_row = conn.execute(
                text("SELECT COUNT(*) FROM model_metadata")
            ).fetchone()

        assert count_row[0] == 1, "Singleton constraint violated: more than one metadata row"

        meta = correction_db.get_model_metadata()
        assert meta["last_trained"] == "2026-06-30T12:00:00Z"
        assert meta["sample_count"] == 1500
        assert meta["model_path"] == "/new/path/model.pkl"

    def test_save_model_metadata_accepts_all_none_optional_fields(
        self, correction_engine
    ) -> None:
        """save_model_metadata() succeeds when all optional numeric fields are None."""
        correction_db.save_model_metadata(
            last_trained=None,
            sample_count=None,
            mae_raw=None,
            mae_corrected=None,
            provider_score=None,
            correction_pct=None,
            model_path=None,
            training_status="training",
        )

        meta = correction_db.get_model_metadata()
        assert meta is not None
        assert meta["last_trained"] is None
        assert meta["sample_count"] is None
        assert meta["mae_raw"] is None
        assert meta["training_status"] == "training"


# ---------------------------------------------------------------------------
# WAL mode test (requires on-disk file — uses tmp_path)
# ---------------------------------------------------------------------------


class TestWALMode:
    def test_init_engine_enables_wal_journal_mode(self, tmp_path) -> None:
        """init_engine() sets journal_mode=WAL via the connect event listener."""
        db_file = str(tmp_path / "test_correction.db")
        engine = correction_db.init_engine(db_file)

        try:
            with engine.connect() as conn:
                row = conn.execute(text("PRAGMA journal_mode")).fetchone()
            assert row is not None
            assert row[0].lower() == "wal", (
                f"Expected WAL journal mode, got: {row[0]!r}"
            )
        finally:
            # Clean up module state so subsequent tests aren't affected
            correction_db._engine = None
            engine.dispose()
