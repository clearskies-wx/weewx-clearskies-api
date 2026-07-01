"""End-to-end integration tests for the forecast correction pipeline (ADR-079 Phase 4).

Tests the full pipeline: collect (synthetic) → train_model() → correct_bundle() → verify.
These tests use in-memory SQLite for the correction DB and real sklearn RF models.
No HTTP layer is required — the core correction pipeline is exercised directly.

Marked @pytest.mark.integration per conftest.py convention.

Test cases:
1. End-to-end pipeline: insert pairs → train → correct_bundle() → temps differ from originals.
2. Correction improves accuracy: mae_corrected < mae_raw from training result.
3. TruScore positive improvement: provider_score > 0 and correction_score > 0.
4. No-model passthrough: without training, correct_bundle() leaves temps unchanged.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from weewx_clearskies_api.correction import db as correction_db
from weewx_clearskies_api.correction.trainer import train_model
from weewx_clearskies_api.config.settings import ForecastCorrectionSettings


# ---------------------------------------------------------------------------
# Mock bundle shapes
# ---------------------------------------------------------------------------


class MockHourlyPoint:
    """Minimal stand-in for HourlyForecastPoint."""

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
    """Minimal stand-in for ForecastBundle."""

    def __init__(self, hourly: list[MockHourlyPoint] | None = None) -> None:
        self.hourly = hourly if hourly is not None else []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_SIXTY_DAYS_AGO = int(time.time()) - 60 * 86400


@pytest.fixture()
def correction_engine():
    """In-memory SQLite correction engine; resets module state on teardown."""
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
    """ForecastCorrectionSettings pointing at tmp_path, NOT validated."""
    s = ForecastCorrectionSettings({
        "enabled": "true",
        "min_samples": "100",
        "retention_years": "3",
    })
    s.model_path = str(tmp_path / "model.pkl")
    s.db_path = str(tmp_path / "correction.db")
    return s


@pytest.fixture(autouse=True)
def _reset_corrector_state():
    """Reset corrector module state after each test."""
    yield
    import weewx_clearskies_api.correction.corrector as c
    c._model = None
    c._enabled = False
    c._model_path = None
    c._feature_medians = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_known_bias_pairs(*, count: int, bias: float = 2.0) -> None:
    """Insert `count` pairs with a known constant temperature bias."""
    base = _SIXTY_DAYS_AGO
    for i in range(count):
        ts = base + i * 1200
        correction_db.insert_pair(
            timestamp=ts,
            provider_id="openmeteo",
            month=6,
            hour=i % 24,
            day_of_year=180,
            fcst_temp=20.0,
            fcst_wind_dir=270.0,
            fcst_humidity=65.0,
            fcst_cloud_cover=40.0,
            fcst_wind_speed=12.0,
            actual_temp=20.0 + bias,
        )


def _make_test_bundle(n_points: int = 6, base_hour: int = 10) -> MockForecastBundle:
    """Create a ForecastBundle with n_points hourly entries at known temperatures."""
    hourly = []
    for i in range(n_points):
        hour = (base_hour + i) % 24
        hourly.append(MockHourlyPoint(
            validTime=f"2026-06-30T{hour:02d}:00:00Z",
            outTemp=20.0,
        ))
    return MockForecastBundle(hourly=hourly)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEndToEndPipeline:
    def test_end_to_end_collect_train_correct(
        self, correction_engine, correction_settings
    ) -> None:
        """Insert >=500 synthetic pairs with +2°F bias → train → correct_bundle() → temps differ.

        The corrector must adjust outTemp values away from the original 20.0.
        We do not assert the exact correction amount (RF predictions vary) but we
        verify that the pipeline ran end-to-end without error and produced a non-trivial
        correction on at least one hourly point.
        """
        import weewx_clearskies_api.correction.corrector as corrector

        _insert_known_bias_pairs(count=500, bias=2.0)

        result = train_model(correction_settings)
        assert result["success"] is True, f"Training failed: {result}"

        corrector.wire_corrector(correction_settings)
        assert corrector.is_active(), "Corrector must be active after training"

        bundle = _make_test_bundle(n_points=6, base_hour=10)
        original_temps = [p.outTemp for p in bundle.hourly]

        corrector.correct_bundle(bundle)
        corrected_temps = [p.outTemp for p in bundle.hourly]

        # At least one point must have been adjusted.
        assert corrected_temps != original_temps, (
            "correct_bundle() must modify at least one hourly outTemp with an active model"
        )

    def test_correction_improves_accuracy(
        self, correction_engine, correction_settings
    ) -> None:
        """mae_corrected < mae_raw from train_model() result when a real bias exists."""
        _insert_known_bias_pairs(count=500, bias=3.0)

        result = train_model(correction_settings)

        assert result["success"] is True
        assert result["mae_corrected"] < result["mae_raw"], (
            f"Correction should improve accuracy: "
            f"mae_raw={result['mae_raw']}, mae_corrected={result['mae_corrected']}"
        )

    def test_truscores_are_positive_with_bias_data(
        self, correction_engine, correction_settings
    ) -> None:
        """provider_score > 0 and correction_score > 0 when training data has a clear bias."""
        _insert_known_bias_pairs(count=500, bias=2.5)

        result = train_model(correction_settings)

        assert result["success"] is True
        assert result["provider_score"] > 0, (
            f"provider_score must be positive, got {result['provider_score']}"
        )
        assert result["correction_score"] > 0, (
            f"correction_score must be positive when bias is correctable, "
            f"got {result['correction_score']}"
        )

    def test_no_model_passthrough(self, correction_engine, correction_settings) -> None:
        """Without training (no model file), correct_bundle() leaves temps unchanged."""
        import weewx_clearskies_api.correction.corrector as corrector

        # Do NOT call train_model() — model file never exists.
        corrector.wire_corrector(correction_settings)
        assert corrector.is_active() is False, (
            "Corrector must not be active without a model file"
        )

        bundle = _make_test_bundle(n_points=4, base_hour=8)
        original_temps = [p.outTemp for p in bundle.hourly]

        corrector.correct_bundle(bundle)
        after_temps = [p.outTemp for p in bundle.hourly]

        assert after_temps == original_temps, (
            "Without an active model, correct_bundle() must be a pure no-op"
        )
