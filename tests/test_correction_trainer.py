"""Tests for weewx_clearskies_api/correction/trainer.py (ADR-079 Phase 3).

Validates train_model():
- Known constant bias: model learns +2°F bias.
- Hour-dependent diurnal bias: model captures pattern.
- min_samples gate: early return when insufficient data.
- None features: median imputation survives.
- Model serialization round-trip: loaded dict has correct keys; predictions match.
- TruScore math: provider_score and correction_score computed correctly.
- Retention purge: old records deleted before training.
- Atomic write failure: temp file cleaned up, training_status=failed.
- FEATURE_COLUMNS constant: exactly 7 entries in documented order.
- Bootstrap fallback: all data older than 30 days → training data used as validation.

Uses in-memory SQLite for the correction DB so no filesystem DB access is needed.
Settings objects are constructed WITHOUT calling validate() so tmp_path-based
model_path values (outside /etc/weewx-clearskies/) are accepted.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from unittest.mock import patch

import joblib
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from weewx_clearskies_api.correction import db as correction_db
from weewx_clearskies_api.correction.trainer import FEATURE_COLUMNS, train_model
from weewx_clearskies_api.config.settings import ForecastCorrectionSettings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def correction_engine(tmp_path):
    """In-memory SQLite correction engine; resets module state after each test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    correction_db.wire_engine(engine)
    correction_db._create_tables(engine)
    yield engine
    correction_db._engine = None


@pytest.fixture()
def correction_settings(tmp_path):
    """ForecastCorrectionSettings pointing at tmp_path, NOT validated.

    validate() is NOT called because tmp_path is outside the /etc/weewx-clearskies/
    allowlist. Training logic tests do not require settings validation.
    """
    s = ForecastCorrectionSettings({
        "enabled": "true",
        "min_samples": "100",
        "retention_years": "3",
    })
    # Override paths to tmp_path AFTER construction to bypass path validation.
    s.model_path = str(tmp_path / "model.pkl")
    s.db_path = str(tmp_path / "correction.db")
    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_training_pairs(
    *,
    count: int,
    base_ts: int,
    fcst_temp: float = 20.0,
    bias: float = 2.0,
    hour_override: int | None = None,
    month: int = 6,
    day_of_year: int = 180,
    ts_spacing: int = 3600,
    fcst_wind_dir: float | None = 270.0,
    fcst_humidity: float | None = 65.0,
    fcst_cloud_cover: float | None = 40.0,
    fcst_wind_speed: float | None = 12.0,
) -> None:
    """Insert `count` forecast-observation pairs into the correction DB."""
    for i in range(count):
        ts = base_ts + i * ts_spacing
        h = hour_override if hour_override is not None else (i % 24)
        correction_db.insert_pair(
            timestamp=ts,
            provider_id="openmeteo",
            month=month,
            hour=h,
            day_of_year=day_of_year,
            fcst_temp=fcst_temp,
            fcst_wind_dir=fcst_wind_dir,
            fcst_humidity=fcst_humidity,
            fcst_cloud_cover=fcst_cloud_cover,
            fcst_wind_speed=fcst_wind_speed,
            actual_temp=fcst_temp + bias,
        )


# Unix epoch for "3 years ago" — safely in the training window (>30 days old).
_THREE_YEARS_AGO = int(time.time()) - int(3 * 365.25 * 86400)
# Unix epoch for "60 days ago" — in the training window, not the validation window.
_SIXTY_DAYS_AGO = int(time.time()) - 60 * 86400


# ---------------------------------------------------------------------------
# FEATURE_COLUMNS constant
# ---------------------------------------------------------------------------


class TestFeatureColumnsConstant:
    def test_feature_columns_has_exactly_seven_entries(self) -> None:
        """FEATURE_COLUMNS must list exactly 7 feature names (per ADR-079 spec)."""
        assert len(FEATURE_COLUMNS) == 7, (
            f"Expected 7 feature columns, got {len(FEATURE_COLUMNS)}: {FEATURE_COLUMNS}"
        )

    def test_feature_columns_documented_order(self) -> None:
        """FEATURE_COLUMNS must contain the 7 documented names in documented order."""
        expected = [
            "month",
            "hour",
            "fcst_temp",
            "fcst_wind_dir",
            "fcst_humidity",
            "fcst_cloud_cover",
            "fcst_wind_speed",
        ]
        assert FEATURE_COLUMNS == expected, (
            f"FEATURE_COLUMNS order mismatch.\nExpected: {expected}\nGot: {FEATURE_COLUMNS}"
        )


# ---------------------------------------------------------------------------
# min_samples gate
# ---------------------------------------------------------------------------


class TestMinSamplesGate:
    def test_insufficient_data_returns_success_false(
        self, correction_engine, correction_settings
    ) -> None:
        """Fewer training pairs than min_samples → success=False with descriptive message."""
        correction_settings.min_samples = 500

        # Insert only 50 pairs older than 30 days (in the training set).
        _insert_training_pairs(count=50, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600)

        result = train_model(correction_settings)

        assert result["success"] is False
        assert "insufficient" in result["message"].lower() or "50" in result["message"], (
            f"Expected message about insufficient data, got: {result['message']!r}"
        )
        assert result["sample_count"] == 50

    def test_exactly_min_samples_crosses_gate(
        self, correction_engine, correction_settings
    ) -> None:
        """Exactly min_samples pairs in training set → model trains (success=True)."""
        min_s = 100
        correction_settings.min_samples = min_s

        _insert_training_pairs(count=min_s, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600)

        result = train_model(correction_settings)

        assert result["success"] is True, (
            f"Expected success=True at exactly min_samples={min_s}, got: {result}"
        )


# ---------------------------------------------------------------------------
# Known constant bias → model learns it
# ---------------------------------------------------------------------------


class TestKnownConstantBias:
    def test_constant_two_degree_bias_is_learned(
        self, correction_engine, correction_settings
    ) -> None:
        """600 training pairs with actual_temp = fcst_temp + 2.0 → MAE_raw ≈ 2.0,
        MAE_corrected < MAE_raw, success=True.
        """
        correction_settings.min_samples = 100

        _insert_training_pairs(
            count=600,
            base_ts=_SIXTY_DAYS_AGO,
            fcst_temp=20.0,
            bias=2.0,
            ts_spacing=600,
        )

        result = train_model(correction_settings)

        assert result["success"] is True, f"Training failed: {result}"
        assert result["mae_raw"] == pytest.approx(2.0, abs=0.1), (
            f"Expected MAE_raw ≈ 2.0, got {result['mae_raw']}"
        )
        assert result["mae_corrected"] < result["mae_raw"], (
            f"Corrected MAE {result['mae_corrected']} should be < raw MAE {result['mae_raw']}"
        )


# ---------------------------------------------------------------------------
# Hour-dependent diurnal bias
# ---------------------------------------------------------------------------


class TestDiurnalBias:
    def test_diurnal_bias_pattern_captured(
        self, correction_engine, correction_settings
    ) -> None:
        """Afternoon (hour 12-17) bias=+3, morning (6-11) bias=+1. Model captures pattern."""
        correction_settings.min_samples = 100
        base = _SIXTY_DAYS_AGO

        # Insert afternoon pairs with +3 bias.
        for i in range(300):
            ts = base + i * 1200
            hour = 12 + (i % 6)  # 12..17
            correction_db.insert_pair(
                timestamp=ts,
                provider_id="openmeteo",
                month=6,
                hour=hour,
                day_of_year=180,
                fcst_temp=25.0,
                fcst_wind_dir=270.0,
                fcst_humidity=60.0,
                fcst_cloud_cover=30.0,
                fcst_wind_speed=10.0,
                actual_temp=28.0,  # +3 bias
            )

        # Insert morning pairs with +1 bias.
        for i in range(300):
            ts = base + 300 * 1200 + i * 1200  # offset to avoid ts collisions
            hour = 6 + (i % 6)  # 6..11
            correction_db.insert_pair(
                timestamp=ts,
                provider_id="openmeteo",
                month=6,
                hour=hour,
                day_of_year=180,
                fcst_temp=15.0,
                fcst_wind_dir=270.0,
                fcst_humidity=70.0,
                fcst_cloud_cover=20.0,
                fcst_wind_speed=8.0,
                actual_temp=16.0,  # +1 bias
            )

        result = train_model(correction_settings)

        assert result["success"] is True, f"Training failed: {result}"
        # With a clear diurnal signal, corrected MAE should improve over raw.
        assert result["mae_corrected"] < result["mae_raw"], (
            f"Diurnal model should improve MAE: raw={result['mae_raw']}, "
            f"corrected={result['mae_corrected']}"
        )


# ---------------------------------------------------------------------------
# None features → median imputation
# ---------------------------------------------------------------------------


class TestNullFeatureMedianImputation:
    def test_null_features_do_not_crash_training(
        self, correction_engine, correction_settings
    ) -> None:
        """~20% null windDir/cloudCover values: training completes without error."""
        correction_settings.min_samples = 100
        base = _SIXTY_DAYS_AGO

        for i in range(300):
            ts = base + i * 1200
            wind_dir = None if i % 5 == 0 else 270.0
            cloud = None if i % 5 == 0 else 40.0
            correction_db.insert_pair(
                timestamp=ts,
                provider_id="openmeteo",
                month=6,
                hour=i % 24,
                day_of_year=180,
                fcst_temp=20.0,
                fcst_wind_dir=wind_dir,
                fcst_humidity=65.0,
                fcst_cloud_cover=cloud,
                fcst_wind_speed=10.0,
                actual_temp=22.0,
            )

        result = train_model(correction_settings)

        assert result["success"] is True, f"Training with null features failed: {result}"


# ---------------------------------------------------------------------------
# Model serialization round-trip
# ---------------------------------------------------------------------------


class TestModelSerializationRoundTrip:
    def test_model_file_contains_required_keys(
        self, correction_engine, correction_settings
    ) -> None:
        """After training, joblib.load() returns dict with 'model', 'feature_medians', 'feature_columns'."""
        correction_settings.min_samples = 100

        _insert_training_pairs(count=200, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600, bias=1.5)

        result = train_model(correction_settings)
        assert result["success"] is True

        loaded = joblib.load(correction_settings.model_path)
        assert "model" in loaded, "Model dict must contain 'model' key"
        assert "feature_medians" in loaded, "Model dict must contain 'feature_medians' key"
        assert "feature_columns" in loaded, "Model dict must contain 'feature_columns' key"
        assert loaded["feature_columns"] == FEATURE_COLUMNS

    def test_loaded_model_produces_predictions(
        self, correction_engine, correction_settings
    ) -> None:
        """Loaded model's predict() returns an array of floats for a valid feature matrix."""
        correction_settings.min_samples = 100

        _insert_training_pairs(count=200, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600, bias=2.0)

        train_model(correction_settings)

        loaded = joblib.load(correction_settings.model_path)
        model = loaded["model"]

        # Construct a minimal feature vector: [month, hour, fcst_temp, wind_dir, humidity, cloud, wind_speed]
        X = np.array([[6, 14, 20.0, 270.0, 65.0, 40.0, 12.0]], dtype=np.float64)
        preds = model.predict(X)

        assert len(preds) == 1
        assert isinstance(float(preds[0]), float)


# ---------------------------------------------------------------------------
# TruScore math verification
# ---------------------------------------------------------------------------


class TestTruScoreMath:
    def test_provider_score_equals_100_minus_mae_raw(
        self, correction_engine, correction_settings
    ) -> None:
        """provider_score must equal 100.0 - mae_raw (per ADR-079 formula)."""
        correction_settings.min_samples = 100

        _insert_training_pairs(count=200, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600, bias=3.0)

        result = train_model(correction_settings)
        assert result["success"] is True

        expected_provider_score = round(100.0 - result["mae_raw"], 2)
        assert result["provider_score"] == pytest.approx(expected_provider_score, abs=0.01), (
            f"provider_score={result['provider_score']} != 100 - mae_raw={result['mae_raw']}"
        )

    def test_correction_score_equals_100_minus_mae_corrected(
        self, correction_engine, correction_settings
    ) -> None:
        """correction_score must equal 100.0 - mae_corrected (same scale as provider_score)."""
        correction_settings.min_samples = 100

        _insert_training_pairs(count=200, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600, bias=2.0)

        result = train_model(correction_settings)
        assert result["success"] is True

        expected = round(100.0 - result["mae_corrected"], 2)
        assert result["correction_score"] == pytest.approx(expected, abs=0.01), (
            f"correction_score={result['correction_score']} != 100 - mae_corrected={result['mae_corrected']}"
        )


# ---------------------------------------------------------------------------
# Retention purge
# ---------------------------------------------------------------------------


class TestRetentionPurge:
    def test_old_records_purged_before_training(
        self, correction_engine, correction_settings
    ) -> None:
        """Records older than retention_years are deleted before model fit."""
        correction_settings.min_samples = 100
        correction_settings.retention_years = 3

        # Pairs 4 years old — should be purged.
        four_years_ago = int(time.time()) - int(4 * 365.25 * 86400)
        _insert_training_pairs(count=20, base_ts=four_years_ago, ts_spacing=600, bias=1.0)

        # Pairs ~1 year old (safely within retention window and >30 days for training set).
        one_year_ago = int(time.time()) - int(1 * 365 * 86400)
        _insert_training_pairs(count=200, base_ts=one_year_ago, ts_spacing=600, bias=2.0)

        count_before = correction_db.get_pair_count()
        assert count_before == 220, f"Expected 220 pairs before training, got {count_before}"

        train_model(correction_settings)

        count_after = correction_db.get_pair_count()
        # The 20 old pairs should be gone; the 200 recent pairs survive.
        assert count_after == 200, (
            f"Expected 200 pairs after purge (4-year-old records deleted), got {count_after}"
        )


# ---------------------------------------------------------------------------
# Atomic write failure → temp file cleaned up
# ---------------------------------------------------------------------------


class TestAtomicWriteFailure:
    def test_temp_file_cleaned_on_joblib_dump_failure(
        self, correction_engine, correction_settings
    ) -> None:
        """If joblib.dump raises, the temp .pkl.tmp file is not left on disk."""
        correction_settings.min_samples = 100

        _insert_training_pairs(count=200, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600, bias=1.0)

        model_dir = os.path.dirname(correction_settings.model_path)

        tmp_files_before = {
            f for f in os.listdir(model_dir) if f.endswith(".pkl.tmp")
        }

        with patch("joblib.dump", side_effect=OSError("simulated disk full")):
            with pytest.raises(OSError):
                train_model(correction_settings)

        tmp_files_after = {
            f for f in os.listdir(model_dir) if f.endswith(".pkl.tmp")
        }
        new_tmp_files = tmp_files_after - tmp_files_before
        assert not new_tmp_files, (
            f"Expected no .pkl.tmp files left after failed dump; found: {new_tmp_files}"
        )

    def test_training_status_set_to_failed_on_exception(
        self, correction_engine, correction_settings
    ) -> None:
        """When training raises, training_status is persisted as 'failed' in model_metadata."""
        correction_settings.min_samples = 100

        _insert_training_pairs(count=200, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600, bias=1.0)

        with patch("joblib.dump", side_effect=OSError("simulated disk full")):
            with pytest.raises(OSError):
                train_model(correction_settings)

        meta = correction_db.get_model_metadata()
        assert meta is not None, "Model metadata should be written even on failure"
        assert meta["training_status"] == "failed", (
            f"Expected training_status='failed', got {meta['training_status']!r}"
        )


# ---------------------------------------------------------------------------
# Bootstrap fallback: all data older than 30 days
# ---------------------------------------------------------------------------


class TestBootstrapValidationFallback:
    def test_all_training_data_older_than_30_days_uses_bootstrap(
        self, correction_engine, correction_settings
    ) -> None:
        """When all pairs are >30 days old (empty validation set), bootstrap warning fires
        and training still succeeds using training data as validation.
        """
        correction_settings.min_samples = 100

        # All 200 pairs are 60 days old → training set >=100, validation set is empty.
        _insert_training_pairs(count=200, base_ts=_SIXTY_DAYS_AGO, ts_spacing=600, bias=2.0)

        # Verify there is nothing in the last 30 days.
        cutoff = int(time.time()) - 30 * 86400
        validation_rows = correction_db.get_validation_data(cutoff)
        assert len(validation_rows) == 0, "Precondition: no validation data"

        result = train_model(correction_settings)

        assert result["success"] is True, (
            f"Bootstrap fallback should produce success=True, got: {result}"
        )
        assert "mae_raw" in result
        assert "mae_corrected" in result

    def test_all_data_within_30_days_uses_bootstrap(
        self, correction_engine, correction_settings
    ) -> None:
        """When all pairs are <30 days old (empty training set but enough total
        pairs), bootstrap mode trains using all data for both sets.

        This is the fresh-deployment case: the system was just installed and
        has been collecting pairs for less than 30 days.
        """
        correction_settings.min_samples = 100

        # All 200 pairs are within the last 30 days.
        recent_ts = int(time.time()) - 10 * 86400  # 10 days ago
        _insert_training_pairs(count=200, base_ts=recent_ts, ts_spacing=600, bias=2.0)

        # Verify training set is empty (all data within 30 days).
        cutoff = int(time.time()) - 30 * 86400
        training_rows = correction_db.get_training_data(cutoff)
        assert len(training_rows) == 0, "Precondition: no training data older than 30 days"

        result = train_model(correction_settings)

        assert result["success"] is True, (
            f"Bootstrap (all-recent) should produce success=True, got: {result}"
        )
        assert "mae_raw" in result
        assert "mae_corrected" in result
        assert result["sample_count"] == 200

    def test_all_data_within_30_days_below_min_samples_fails(
        self, correction_engine, correction_settings
    ) -> None:
        """When all pairs are <30 days old and total count is below min_samples,
        training correctly returns success=False.
        """
        correction_settings.min_samples = 500

        recent_ts = int(time.time()) - 10 * 86400
        _insert_training_pairs(count=50, base_ts=recent_ts, ts_spacing=600, bias=2.0)

        result = train_model(correction_settings)

        assert result["success"] is False
        assert result["sample_count"] == 50
