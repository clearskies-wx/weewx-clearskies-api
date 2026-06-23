"""Unit tests for weewx_clearskies_api.sse.auto_calibration (ADR-068).

Validates the monthly-normals model introduced in ADR-068:
  - _percentile() linear interpolation helper (unchanged from v1)
  - compute_monthly_baseline() per-month 92nd-percentile computation
  - get_calibration_state() state transitions (no-data / bootstrapping /
    partial / fully-calibrated) and per-month list structure
  - Drift detection via _check_drift()
  - Station type tracking via set_station_type() / check_station_type_change()
  - Flat fallback baseline via _compute_flat_baseline() / get_current_baseline()
  - process_packet() gate sequence via mocked dependencies
  - persist() / load_persisted() in v2 format via tmp_path
  - v1 → v2 migration: flat sample list distributed into monthly buckets
  - Timezone-aware month binning

Module-level state is intentional in auto_calibration.py; the autouse
fixture calls reset() before every test to provide clean isolation.
"""

from __future__ import annotations

import json

import pytest

from weewx_clearskies_api.sse import auto_calibration


# ---------------------------------------------------------------------------
# Autouse reset fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_auto_cal():
    """Reset auto_calibration module state before and after each test."""
    auto_calibration.reset()
    yield
    auto_calibration.reset()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A realistic epoch (2027-01-14 ~13:26:40 UTC).  Large enough that
# _FROZEN_NOW - _RAIN_HOLDOFF is still positive, so the rain-holdoff gate
# is always satisfied when _last_rain_time defaults to 0 after reset().
_FROZEN_NOW = 1_800_000_000.0

# Matches _MIN_SAMPLES_PER_MONTH in the module.
_MIN_SAMPLES = 30

# 30 realistic clean-sky Kcs values (clear afternoon at a good station).
_CLEAN_KCS = [0.88 + (i % 10) * 0.005 for i in range(30)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_monthly_samples(
    month: int,
    kcs_values: list[float],
    base_ts: float = _FROZEN_NOW,
) -> None:
    """Directly populate auto_calibration._monthly_samples[month].

    Timestamps are 1 hour apart, ending at base_ts.
    Does NOT call process_packet() — bypasses all gates so tests can
    exercise compute_monthly_baseline() and get_calibration_state() directly.
    """
    for i, kcs in enumerate(kcs_values):
        ts = base_ts - len(kcs_values) * 3600 + i * 3600  # 1 hour apart
        auto_calibration._monthly_samples[month].append((ts, kcs))


# ===========================================================================
# Group 1: _percentile() linear interpolation  (UNCHANGED from v1 — keep as-is)
# ===========================================================================


class TestPercentileHelper:
    """_percentile() computes the k-th percentile with linear interpolation."""

    def test_single_value_returns_that_value(self) -> None:
        """Single-element list → the only value is returned at any percentile."""
        result = auto_calibration._percentile([0.90], 50.0)
        assert result == pytest.approx(0.90)

    def test_two_values_at_percentile_0_returns_first(self) -> None:
        """Percentile 0 of [0.80, 0.90] → 0.80 (first element, idx=0)."""
        result = auto_calibration._percentile([0.80, 0.90], 0.0)
        assert result == pytest.approx(0.80)

    def test_two_values_at_percentile_100_returns_last(self) -> None:
        """Percentile 100 of [0.80, 0.90] → 0.90 (last element, hi>=n guard)."""
        result = auto_calibration._percentile([0.80, 0.90], 100.0)
        assert result == pytest.approx(0.90)

    def test_two_values_at_percentile_50_is_midpoint(self) -> None:
        """Percentile 50 of [0.80, 0.90] → 0.85 (midpoint by linear interp)."""
        result = auto_calibration._percentile([0.80, 0.90], 50.0)
        assert result == pytest.approx(0.85)

    def test_four_values_at_percentile_50(self) -> None:
        """Percentile 50 of [0.80, 0.85, 0.90, 0.95] → ≈ 0.875.

        idx = 0.50 * (4-1) = 1.5
        interp = 0.85 + 0.5 * (0.90 - 0.85) = 0.875
        """
        result = auto_calibration._percentile([0.80, 0.85, 0.90, 0.95], 50.0)
        assert result == pytest.approx(0.875)

    def test_four_values_at_percentile_90(self) -> None:
        """Percentile 90 of [0.80, 0.85, 0.90, 0.95] → ≈ 0.935.

        idx = 0.90 * (4-1) = 2.7
        interp = 0.90 + 0.7 * (0.95 - 0.90) = 0.90 + 0.035 = 0.935
        """
        result = auto_calibration._percentile([0.80, 0.85, 0.90, 0.95], 90.0)
        assert result == pytest.approx(0.935)

    def test_four_values_at_percentile_95(self) -> None:
        """Percentile 95 of [0.80, 0.85, 0.90, 0.95] → ≈ 0.9425.

        idx = 0.95 * (4-1) = 2.85
        interp = 0.90 + 0.85 * (0.95 - 0.90) = 0.90 + 0.0425 = 0.9425
        """
        result = auto_calibration._percentile([0.80, 0.85, 0.90, 0.95], 95.0)
        assert result == pytest.approx(0.9425)

    def test_monotone_sorted_list_boundary_clamp(self) -> None:
        """hi >= n clamp: percentile 100 on any list returns the last element."""
        values = [0.70, 0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96]
        result = auto_calibration._percentile(values, 100.0)
        assert result == pytest.approx(values[-1])


# ===========================================================================
# Group 2: compute_monthly_baseline()
# ===========================================================================


class TestComputeMonthlyBaseline:
    """compute_monthly_baseline() returns 92nd-percentile Kcs or None."""

    def test_empty_month_returns_none(self) -> None:
        """No samples for a month → None."""
        result = auto_calibration.compute_monthly_baseline(6)
        assert result is None

    def test_below_30_samples_returns_none(self) -> None:
        """29 samples → None (requires >= 30)."""
        _inject_monthly_samples(3, [0.90] * 29)
        result = auto_calibration.compute_monthly_baseline(3)
        assert result is None

    def test_exactly_30_samples_returns_float(self) -> None:
        """Exactly 30 samples → returns a float (threshold is met)."""
        _inject_monthly_samples(3, _CLEAN_KCS)  # _CLEAN_KCS has exactly 30 values
        result = auto_calibration.compute_monthly_baseline(3)
        assert result is not None
        assert isinstance(result, float)

    def test_50_samples_returns_float_in_plausible_range(self) -> None:
        """50 realistic Kcs samples → baseline in [0.80, 1.05]."""
        kcs_50 = [0.88 + (i % 10) * 0.005 for i in range(50)]
        _inject_monthly_samples(7, kcs_50)
        result = auto_calibration.compute_monthly_baseline(7)
        assert result is not None
        assert 0.80 <= result <= 1.05, (
            f"Baseline {result} outside plausible range [0.80, 1.05]"
        )

    def test_wrong_months_samples_excluded(self) -> None:
        """Month 1 samples do not affect month 2's baseline."""
        _inject_monthly_samples(1, _CLEAN_KCS)  # 30 samples in month 1
        result = auto_calibration.compute_monthly_baseline(2)
        assert result is None, (
            "Month 2 baseline must be None — month 1's samples must not bleed over"
        )

    def test_92nd_percentile_is_near_top_of_distribution(self) -> None:
        """92nd percentile of uniform 0.90 samples is 0.90."""
        _inject_monthly_samples(5, [0.90] * 30)
        result = auto_calibration.compute_monthly_baseline(5)
        assert result == pytest.approx(0.90)


# ===========================================================================
# Group 3: get_calibration_state() — state transitions
# ===========================================================================


class TestGetCalibrationState:
    """State transitions: no-data → bootstrapping → partial → fully-calibrated."""

    def test_no_samples_no_data(self) -> None:
        """No samples → overall_state='no-data', months_calibrated=0."""
        state = auto_calibration.get_calibration_state()
        assert state["overall_state"] == "no-data"
        assert state["months_calibrated"] == 0

    def test_some_samples_below_threshold_bootstrapping(self) -> None:
        """29 samples in one month (< 30) → 'bootstrapping', months_calibrated=0."""
        _inject_monthly_samples(4, [0.90] * 29)
        state = auto_calibration.get_calibration_state()
        assert state["overall_state"] == "bootstrapping"
        assert state["months_calibrated"] == 0

    def test_one_month_calibrated_partial(self) -> None:
        """One month with >= 30 samples → 'partial', months_calibrated=1."""
        _inject_monthly_samples(4, _CLEAN_KCS)
        auto_calibration._monthly_baselines[4] = auto_calibration.compute_monthly_baseline(4)
        state = auto_calibration.get_calibration_state()
        assert state["overall_state"] == "partial"
        assert state["months_calibrated"] == 1

    def test_all_12_months_calibrated_fully_calibrated(self) -> None:
        """All 12 months with >= 30 samples → 'fully-calibrated', months_calibrated=12."""
        for m in range(1, 13):
            _inject_monthly_samples(m, _CLEAN_KCS)
            auto_calibration._monthly_baselines[m] = auto_calibration.compute_monthly_baseline(m)
        state = auto_calibration.get_calibration_state()
        assert state["overall_state"] == "fully-calibrated"
        assert state["months_calibrated"] == 12

    def test_per_month_list_has_12_entries(self) -> None:
        """per_month list always has exactly 12 entries."""
        state = auto_calibration.get_calibration_state()
        assert len(state["per_month"]) == 12

    def test_per_month_entries_have_correct_keys(self) -> None:
        """Each per_month entry has the required keys."""
        required_keys = {"month", "name", "sample_count", "baseline_kcs", "is_calibrated"}
        state = auto_calibration.get_calibration_state()
        for entry in state["per_month"]:
            assert required_keys.issubset(entry.keys()), (
                f"per_month entry missing keys: {required_keys - entry.keys()!r}"
            )

    def test_per_month_month_numbers_are_1_through_12(self) -> None:
        """per_month entries are in order, with month numbers 1–12."""
        state = auto_calibration.get_calibration_state()
        months = [e["month"] for e in state["per_month"]]
        assert months == list(range(1, 13))

    def test_station_type_reflected_in_state(self) -> None:
        """set_station_type() value appears in get_calibration_state()."""
        auto_calibration.set_station_type("Vantage")
        state = auto_calibration.get_calibration_state()
        assert state["station_type"] == "Vantage"

    def test_station_type_none_by_default(self) -> None:
        """station_type is None before set_station_type() is called."""
        state = auto_calibration.get_calibration_state()
        assert state["station_type"] is None

    def test_flat_baseline_in_state(self) -> None:
        """flat_baseline in state reflects _flat_baseline module variable."""
        auto_calibration._flat_baseline = 0.905
        state = auto_calibration.get_calibration_state()
        assert state["flat_baseline"] == pytest.approx(0.905)

    def test_flat_baseline_none_when_no_data(self) -> None:
        """flat_baseline is None with no samples."""
        state = auto_calibration.get_calibration_state()
        assert state["flat_baseline"] is None

    def test_sample_count_reflected_in_per_month(self) -> None:
        """sample_count in per_month entry matches number of injected samples."""
        _inject_monthly_samples(8, [0.90] * 15)
        state = auto_calibration.get_calibration_state()
        aug_entry = state["per_month"][7]  # month 8 is index 7
        assert aug_entry["sample_count"] == 15

    def test_is_calibrated_false_below_threshold(self) -> None:
        """is_calibrated=False when month has fewer than 30 samples."""
        _inject_monthly_samples(2, [0.90] * 10)
        state = auto_calibration.get_calibration_state()
        feb_entry = state["per_month"][1]  # month 2 is index 1
        assert feb_entry["is_calibrated"] is False

    def test_is_calibrated_true_after_threshold(self) -> None:
        """is_calibrated=True when month has >= 30 samples and baseline is set."""
        _inject_monthly_samples(2, _CLEAN_KCS)
        auto_calibration._monthly_baselines[2] = auto_calibration.compute_monthly_baseline(2)
        state = auto_calibration.get_calibration_state()
        feb_entry = state["per_month"][1]
        assert feb_entry["is_calibrated"] is True
        assert feb_entry["baseline_kcs"] is not None


# ===========================================================================
# Group 4: Drift detection
# ===========================================================================


class TestDriftDetection:
    """_check_drift() returns warning dict or None."""

    def test_no_baseline_returns_none(self) -> None:
        """No baseline for the month → no drift warning."""
        _inject_monthly_samples(3, [0.90] * 15)
        # _monthly_baselines[3] stays None
        result = auto_calibration._check_drift(3)
        assert result is None

    def test_fewer_than_10_recent_samples_returns_none(self) -> None:
        """Fewer than 10 samples → not enough recent data for drift check."""
        _inject_monthly_samples(3, [0.90] * 9)
        auto_calibration._monthly_baselines[3] = 0.90  # set a baseline manually
        result = auto_calibration._check_drift(3)
        assert result is None

    def test_recent_mean_close_to_baseline_no_warning(self) -> None:
        """Recent mean within 0.05 of baseline → no drift warning."""
        # 10 samples near 0.90, baseline 0.90 → divergence ≈ 0
        _inject_monthly_samples(3, [0.90] * 10)
        auto_calibration._monthly_baselines[3] = 0.90
        result = auto_calibration._check_drift(3)
        assert result is None

    def test_recent_mean_diverged_returns_warning(self) -> None:
        """Recent mean > 0.05 from baseline → warning with correct fields."""
        # 10 recent samples averaging ~0.80, baseline is 0.92
        _inject_monthly_samples(3, [0.80] * 10)
        auto_calibration._monthly_baselines[3] = 0.92
        result = auto_calibration._check_drift(3)
        assert result is not None, "Expected drift warning when recent mean diverged > 0.05"
        assert "month" in result
        assert "baseline" in result
        assert "recent_mean" in result
        assert "divergence" in result
        assert result["month"] == 3
        assert result["divergence"] > 0.05

    def test_drift_warning_fields_are_rounded(self) -> None:
        """Drift warning values are rounded to 4 decimal places."""
        _inject_monthly_samples(6, [0.80] * 10)
        auto_calibration._monthly_baselines[6] = 0.9199
        result = auto_calibration._check_drift(6)
        assert result is not None
        # Verify precision — each value should have at most 4 decimal places
        for key in ("baseline", "recent_mean", "divergence"):
            val = result[key]
            assert round(val, 4) == val, f"{key}={val} has more than 4 decimal places"

    def test_drift_warnings_appear_in_calibration_state(self) -> None:
        """Drift warnings surface through get_calibration_state()."""
        _inject_monthly_samples(6, [0.80] * 10)
        auto_calibration._monthly_baselines[6] = 0.92
        state = auto_calibration.get_calibration_state()
        warnings = state["drift_warnings"]
        assert isinstance(warnings, list)
        assert any(w["month"] == 6 for w in warnings), (
            "Drift warning for month 6 must appear in get_calibration_state()"
        )


# ===========================================================================
# Group 5: Station type tracking
# ===========================================================================


class TestStationTypeTracking:
    """set_station_type() + check_station_type_change() contract."""

    def test_same_type_returns_false(self) -> None:
        """Type unchanged → check_station_type_change() returns False."""
        auto_calibration.set_station_type("Vantage")
        result = auto_calibration.check_station_type_change("Vantage")
        assert result is False

    def test_different_type_returns_true(self) -> None:
        """Type changed from 'Vantage' to 'Davis' → returns True."""
        auto_calibration.set_station_type("Vantage")
        result = auto_calibration.check_station_type_change("Davis")
        assert result is True

    def test_stored_none_current_string_returns_false(self) -> None:
        """Stored type is None → returns False even with a non-None current type."""
        auto_calibration.set_station_type(None)
        result = auto_calibration.check_station_type_change("Vantage")
        assert result is False

    def test_stored_string_current_none_returns_false(self) -> None:
        """Current type is None → returns False (cannot confirm change)."""
        auto_calibration.set_station_type("Vantage")
        result = auto_calibration.check_station_type_change(None)
        assert result is False

    def test_both_none_returns_false(self) -> None:
        """Both stored and current are None → returns False."""
        auto_calibration.set_station_type(None)
        result = auto_calibration.check_station_type_change(None)
        assert result is False

    def test_set_station_type_updates_module_state(self) -> None:
        """set_station_type() updates _station_type_at_load."""
        auto_calibration.set_station_type("FineOffset")
        assert auto_calibration._station_type_at_load == "FineOffset"


# ===========================================================================
# Group 6: Flat fallback baseline
# ===========================================================================


class TestFlatFallbackBaseline:
    """_compute_flat_baseline() and get_current_baseline() fallback behaviour."""

    def test_fewer_than_30_total_returns_none(self) -> None:
        """< 30 total samples across all months → flat baseline is None."""
        _inject_monthly_samples(1, [0.90] * 10)
        _inject_monthly_samples(2, [0.90] * 10)
        result = auto_calibration._compute_flat_baseline()
        assert result is None

    def test_30_plus_across_months_returns_float(self) -> None:
        """30+ samples across months → flat baseline is a float."""
        _inject_monthly_samples(1, [0.90] * 20)
        _inject_monthly_samples(7, [0.90] * 15)  # 35 total
        result = auto_calibration._compute_flat_baseline()
        assert result is not None
        assert isinstance(result, float)
        assert 0.80 <= result <= 1.05

    def test_get_current_baseline_returns_monthly_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_current_baseline() returns monthly baseline when that month is calibrated."""
        # Freeze "now" to a known timestamp in January (UTC).
        # _FROZEN_NOW = 1_800_000_000.0 → 2027-01-14, which is January.
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        auto_calibration._monthly_baselines[1] = 0.912
        auto_calibration._flat_baseline = 0.888

        result = auto_calibration.get_current_baseline()
        assert result == pytest.approx(0.912)

    def test_get_current_baseline_falls_back_to_flat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_current_baseline() falls back to flat when current month has no baseline."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        # Month 1 (January) has no baseline; flat_baseline is set.
        auto_calibration._monthly_baselines[1] = None
        auto_calibration._flat_baseline = 0.888

        result = auto_calibration.get_current_baseline()
        assert result == pytest.approx(0.888)

    def test_get_current_baseline_none_when_no_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_current_baseline() is None when no data exists at all."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        result = auto_calibration.get_current_baseline()
        assert result is None


# ===========================================================================
# Group 7: process_packet() — gate sequence via mocks
# ===========================================================================


class TestProcessPacket:
    """process_packet() gate sequence — mocked dependencies, no live weewx."""

    def _patch_all_gates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rain_rate: float | None = 0.0,
        elevation: float | None = 45.0,
        sky_label: str | None = "Clear",
        pm25: float | None = 5.0,
        pm10: float | None = 10.0,
        kcs: float | None = 0.90,
        now: float = _FROZEN_NOW,
    ) -> None:
        """Patch all external dependencies so process_packet() can run cleanly.

        _FROZEN_NOW is large relative to _RAIN_HOLDOFF (1800s), so the
        rain-holdoff gate is satisfied as long as _last_rain_time is 0
        (the post-reset default).
        """
        from weewx_clearskies_api.sse.enrichment import input_smoother
        from weewx_clearskies_api.sse import sky_condition

        monkeypatch.setattr(auto_calibration.time, "time", lambda: now)

        def _get_smoothed(key: str) -> float | None:
            if key == "rainRate":
                return rain_rate
            if key == "pollutantPM25":
                return pm25
            if key == "pollutantPM10":
                return pm10
            return None

        monkeypatch.setattr(input_smoother, "get_smoothed", _get_smoothed)
        monkeypatch.setattr(sky_condition, "get_solar_elevation", lambda: elevation)
        monkeypatch.setattr(sky_condition, "classify", lambda: sky_label)
        monkeypatch.setattr(sky_condition, "get_current_kcs", lambda: kcs)

    def _total_samples(self) -> int:
        """Count all samples across all months."""
        return sum(
            len(auto_calibration._monthly_samples[m]) for m in range(1, 13)
        )

    def test_clean_packet_adds_sample_to_correct_month(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All gates pass → Kcs sample is added to the correct month bucket.

        _FROZEN_NOW maps to January 2027 in UTC, so the sample goes to month 1.
        """
        auto_calibration._timezone_name = "UTC"
        self._patch_all_gates(monkeypatch)
        auto_calibration.process_packet({})

        assert self._total_samples() == 1, "Clean packet must append exactly one sample"
        # Verify it landed in month 1 (January) with expected Kcs value.
        assert len(auto_calibration._monthly_samples[1]) == 1
        assert auto_calibration._monthly_samples[1][0][1] == pytest.approx(0.90)

    def test_rain_gate_suppresses_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rain active → Gate 1 fires, no sample collected."""
        self._patch_all_gates(monkeypatch, rain_rate=0.1)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "Active rain must suppress Kcs sample collection"

    def test_low_elevation_gate_suppresses_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """solar_elevation <= 10° → Gate 2 fires, no sample collected."""
        self._patch_all_gates(monkeypatch, elevation=5.0)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "Low solar elevation must suppress sample"

    def test_none_elevation_suppresses_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """solar_elevation=None → Gate 2 fires, no sample collected."""
        self._patch_all_gates(monkeypatch, elevation=None)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "None solar elevation must suppress sample"

    def test_cloudy_sky_suppresses_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cloudy sky label → Gate 3 fires (no 'Clear'/'Sunny' substring)."""
        self._patch_all_gates(monkeypatch, sky_label="Cloudy")
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "Cloudy sky must suppress sample collection"

    def test_none_sky_label_suppresses_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sky_label=None (startup) → Gate 3 fires, no sample collected."""
        self._patch_all_gates(monkeypatch, sky_label=None)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "None sky label must suppress sample"

    def test_dirty_pm25_suppresses_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PM2.5 >= 12 µg/m³ → Gate 4 fires (dirty air), no sample collected."""
        self._patch_all_gates(monkeypatch, pm25=15.0)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "PM2.5 >= 12 must suppress sample"

    def test_dirty_pm10_suppresses_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PM10 >= 50 µg/m³ → Gate 4 fires (dirty air), no sample collected."""
        self._patch_all_gates(monkeypatch, pm10=55.0)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "PM10 >= 50 must suppress sample"

    def test_none_pm25_suppresses_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pm25=None → Gate 4 fires (cannot confirm clean air)."""
        self._patch_all_gates(monkeypatch, pm25=None)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "None PM2.5 must suppress sample"

    def test_none_kcs_suppresses_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kcs=None → no Kcs value available, sample not appended."""
        self._patch_all_gates(monkeypatch, kcs=None)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, "None Kcs must suppress sample"

    def test_no_radiation_skips_if_no_kcs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_has_radiation=False and get_current_kcs()=None → return early, no sample."""
        auto_calibration.set_has_radiation(False)
        self._patch_all_gates(monkeypatch, kcs=None)
        auto_calibration.process_packet({})
        assert self._total_samples() == 0, (
            "No radiation + no Kcs data must return early with no sample"
        )

    def test_no_radiation_but_kcs_available_flips_flag_and_collects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_has_radiation=False but get_current_kcs() returns a value → flip True, collect sample."""
        auto_calibration.set_has_radiation(False)
        auto_calibration._timezone_name = "UTC"
        self._patch_all_gates(monkeypatch, kcs=0.90)
        auto_calibration.process_packet({})
        assert auto_calibration._has_radiation is True, (
            "Radiation flag must be flipped to True when Kcs data appears"
        )
        assert self._total_samples() == 1, (
            "After radiation flag flips, sample must be collected in the same packet"
        )

    def test_30_samples_triggers_haze_condition_set_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After 30+ samples in the current month, process_packet() calls haze_condition.set_baseline()."""
        from weewx_clearskies_api.sse import haze_condition

        set_baseline_calls: list[float] = []

        def _record_set_baseline(value: float) -> None:
            set_baseline_calls.append(value)

        monkeypatch.setattr(haze_condition, "set_baseline", _record_set_baseline)

        # Inject 29 samples directly into month 1 (January, matching _FROZEN_NOW in UTC).
        auto_calibration._timezone_name = "UTC"
        for i in range(29):
            ts = _FROZEN_NOW - (30 - i) * 60
            auto_calibration._monthly_samples[1].append((ts, 0.90))

        # The 30th sample via process_packet() pushes the month over the threshold.
        self._patch_all_gates(monkeypatch)
        auto_calibration.process_packet({})

        assert len(set_baseline_calls) >= 1, (
            "process_packet() must call haze_condition.set_baseline() after 30th sample"
        )
        assert set_baseline_calls[-1] > 0.0, "Baseline value must be positive"


# ===========================================================================
# Group 8: Persistence — v2 format
# ===========================================================================


class TestPersistenceV2:
    """persist() / load_persisted() round-trip in v2 format via tmp_path."""

    def test_persist_writes_valid_json_with_version_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """persist() writes a valid JSON file with version=2 and expected top-level keys."""
        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))

        _inject_monthly_samples(1, _CLEAN_KCS)
        auto_calibration._monthly_baselines[1] = 0.912
        auto_calibration._flat_baseline = 0.908
        auto_calibration.set_station_type("Vantage")

        auto_calibration.persist()

        assert persist_path.exists(), "persist() must create the calibration file"
        data = json.loads(persist_path.read_text(encoding="utf-8"))

        assert data["version"] == 2
        assert "monthly_samples" in data
        assert "monthly_baselines" in data
        assert "flat_baseline" in data
        assert "station_type" in data

    def test_persist_monthly_samples_structure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """monthly_samples has keys '1'-'12'; month 3 has the injected samples."""
        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))

        _inject_monthly_samples(3, _CLEAN_KCS)
        auto_calibration.persist()

        data = json.loads(persist_path.read_text(encoding="utf-8"))
        monthly = data["monthly_samples"]
        assert set(monthly.keys()) == {str(m) for m in range(1, 13)}
        assert len(monthly["3"]) == 30, "Month 3 must have 30 persisted samples"
        assert monthly["1"] == [], "Month 1 must be empty"

    def test_persist_records_flat_baseline_and_station_type(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """flat_baseline and station_type are written correctly."""
        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))

        auto_calibration._flat_baseline = 0.905
        auto_calibration.set_station_type("WS-2000")
        auto_calibration.persist()

        data = json.loads(persist_path.read_text(encoding="utf-8"))
        assert data["flat_baseline"] == pytest.approx(0.905)
        assert data["station_type"] == "WS-2000"

    def test_load_persisted_v2_restores_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() restores v2 monthly samples, station_type, and recomputes baselines.

        Note: flat_baseline is NOT restored verbatim from disk — load_persisted()
        always recomputes it from the loaded samples via _flat_baseline_update().
        The persisted value is ignored after load.  The test verifies the
        recomputed baseline is a valid float in the plausible range.
        """
        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        samples_m1 = [[_FROZEN_NOW - (30 - i) * 3600, 0.90] for i in range(30)]
        data = {
            "version": 2,
            "monthly_samples": {str(m): (samples_m1 if m == 1 else []) for m in range(1, 13)},
            "monthly_baselines": {str(m): (0.912 if m == 1 else None) for m in range(1, 13)},
            "flat_baseline": 0.908,
            "station_type": "Vantage",
        }
        persist_path.write_text(json.dumps(data), encoding="utf-8")

        auto_calibration.load_persisted()

        assert len(auto_calibration._monthly_samples[1]) == 30, (
            "load_persisted() must restore month 1's 30 samples"
        )
        # Baselines are recomputed from loaded samples, not taken verbatim from disk.
        assert auto_calibration._monthly_baselines[1] is not None, (
            "Month 1 baseline must be recomputed from 30 loaded samples"
        )
        # flat_baseline is recomputed from all months' samples (not the persisted value).
        assert auto_calibration._flat_baseline is not None, (
            "Flat baseline must be recomputed from loaded samples"
        )
        assert 0.80 <= auto_calibration._flat_baseline <= 1.05, (
            f"Recomputed flat baseline {auto_calibration._flat_baseline} outside plausible range"
        )
        assert auto_calibration._station_type_at_load == "Vantage"

    def test_load_persisted_prunes_samples_older_than_3_years(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() discards samples outside the 3-year window."""
        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        window_secs = 3 * 365.25 * 86400.0
        stale_ts = _FROZEN_NOW - window_secs - 86400.0  # just outside 3-year window
        fresh_ts = _FROZEN_NOW - 30 * 86400.0            # 30 days old — well inside

        data = {
            "version": 2,
            "monthly_samples": {
                str(m): (
                    [[stale_ts, 0.88], [fresh_ts, 0.90]] if m == 6 else []
                )
                for m in range(1, 13)
            },
            "monthly_baselines": {str(m): None for m in range(1, 13)},
            "flat_baseline": None,
            "station_type": None,
        }
        persist_path.write_text(json.dumps(data), encoding="utf-8")

        auto_calibration.load_persisted()

        # Only the fresh sample should survive pruning.
        assert len(auto_calibration._monthly_samples[6]) == 1, (
            "Stale sample outside 3-year window must be pruned"
        )
        assert auto_calibration._monthly_samples[6][0][0] == pytest.approx(fresh_ts)

    def test_load_persisted_missing_file_starts_fresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() with a missing file → starts fresh."""
        persist_path = tmp_path / "nonexistent_calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))

        auto_calibration.load_persisted()

        total = sum(len(auto_calibration._monthly_samples[m]) for m in range(1, 13))
        assert total == 0
        assert all(
            auto_calibration._monthly_baselines[m] is None for m in range(1, 13)
        )

    def test_load_persisted_invalid_json_starts_fresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() with corrupt JSON → starts fresh (no crash)."""
        persist_path = tmp_path / "calibration.json"
        persist_path.write_text("not valid json at all }{", encoding="utf-8")
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))

        auto_calibration.load_persisted()  # must not raise

        total = sum(len(auto_calibration._monthly_samples[m]) for m in range(1, 13))
        assert total == 0
        assert auto_calibration._flat_baseline is None

    def test_persist_failure_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """persist() to an unwritable path logs a warning but does not raise."""
        bad_path = "/nonexistent_root_dir_clearskies_test/calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", bad_path)

        # Must not raise.
        auto_calibration.persist()


# ===========================================================================
# Group 9: v1 → v2 migration
# ===========================================================================


class TestV1ToV2Migration:
    """load_persisted() with v1 format distributes samples into monthly buckets."""

    def test_v1_samples_distributed_by_month(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """v1 flat samples list is distributed into per-month buckets via timestamp."""
        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        # Station is UTC; use a timestamp in January 2025.
        auto_calibration.set_timezone("UTC")
        jan_ts = 1_737_000_000.0   # 2025-01-16 ~05:20 UTC
        jul_ts = 1_751_500_000.0   # 2025-07-02 ~22:06 UTC

        v1_data = {
            "samples": [
                [jan_ts, 0.91],
                [jul_ts, 0.88],
            ],
            "baseline_kcs": 0.905,
        }
        persist_path.write_text(json.dumps(v1_data), encoding="utf-8")

        auto_calibration.load_persisted()

        assert len(auto_calibration._monthly_samples[1]) == 1, (
            "January sample must be in month 1"
        )
        assert len(auto_calibration._monthly_samples[7]) == 1, (
            "July sample must be in month 7"
        )

    def test_v1_migration_immediately_persists_v2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """After v1 migration, the file is immediately rewritten in v2 format."""
        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        auto_calibration.set_timezone("UTC")
        v1_data = {"samples": [[_FROZEN_NOW - 1000, 0.90]], "baseline_kcs": 0.90}
        persist_path.write_text(json.dumps(v1_data), encoding="utf-8")

        auto_calibration.load_persisted()

        data = json.loads(persist_path.read_text(encoding="utf-8"))
        assert data.get("version") == 2, (
            "File must be rewritten in v2 format immediately after v1 migration"
        )
        assert "monthly_samples" in data

    def test_v1_migration_uses_station_timezone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """v1 migration bins samples by local month using the configured timezone.

        2025-01-31 22:00 UTC:
          - UTC timezone → January (month 1)
          - Pacific/Auckland (UTC+13) → 2025-02-01 11:00 NZDT → February (month 2)
        """
        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        # 2025-01-31 22:00:00 UTC → UTC=January; Auckland (UTC+13)=February 01.
        ts_utc = 1_738_360_800.0

        # With UTC timezone, this sample is January.
        auto_calibration.set_timezone("UTC")
        v1_data = {"samples": [[ts_utc, 0.90]], "baseline_kcs": None}
        persist_path.write_text(json.dumps(v1_data), encoding="utf-8")
        auto_calibration.load_persisted()
        jan_count_utc = len(auto_calibration._monthly_samples[1])
        feb_count_utc = len(auto_calibration._monthly_samples[2])

        auto_calibration.reset()

        # With Pacific/Auckland (UTC+13), same timestamp is February 1 locally.
        auto_calibration.set_timezone("Pacific/Auckland")
        persist_path.write_text(json.dumps(v1_data), encoding="utf-8")
        auto_calibration.load_persisted()
        jan_count_auckland = len(auto_calibration._monthly_samples[1])
        feb_count_auckland = len(auto_calibration._monthly_samples[2])

        assert jan_count_utc == 1 and feb_count_utc == 0, (
            "UTC: 2025-01-31 22:00 UTC must bin to January"
        )
        assert jan_count_auckland == 0 and feb_count_auckland == 1, (
            "Pacific/Auckland (UTC+13): 2025-01-31 22:00 UTC → 2025-02-01 11:00 → bins to February"
        )


# ===========================================================================
# Group 10: Timezone-aware month binning in process_packet()
# ===========================================================================


class TestTimezoneAwareMonthBinning:
    """process_packet() bins samples into the correct local calendar month."""

    def _patch_gates_at_ts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ts: float,
        kcs: float = 0.90,
    ) -> None:
        """Patch gates to return clean conditions at a specific timestamp."""
        from weewx_clearskies_api.sse.enrichment import input_smoother
        from weewx_clearskies_api.sse import sky_condition

        monkeypatch.setattr(auto_calibration.time, "time", lambda: ts)

        def _get_smoothed(key: str) -> float | None:
            if key == "rainRate":
                return 0.0
            if key == "pollutantPM25":
                return 5.0
            if key == "pollutantPM10":
                return 10.0
            return None

        monkeypatch.setattr(input_smoother, "get_smoothed", _get_smoothed)
        monkeypatch.setattr(sky_condition, "get_solar_elevation", lambda: 45.0)
        monkeypatch.setattr(sky_condition, "classify", lambda: "Clear")
        monkeypatch.setattr(sky_condition, "get_current_kcs", lambda: kcs)

    def test_sample_bins_to_january_in_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sample at Jan 15 noon UTC with timezone='UTC' bins to month 1."""
        from weewx_clearskies_api.sse import haze_condition
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        # 2025-01-15 12:00:00 UTC
        ts = 1_736_942_400.0
        auto_calibration.set_timezone("UTC")
        self._patch_gates_at_ts(monkeypatch, ts)
        auto_calibration.process_packet({})

        assert len(auto_calibration._monthly_samples[1]) == 1, (
            "Sample at Jan 15 12:00 UTC must bin to month 1 when timezone is UTC"
        )

    def test_late_jan_utc_sample_bins_to_feb_in_new_york(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Jan 31 23:50 UTC with timezone='America/New_York' → local Feb 1 → bins to month 2."""
        from weewx_clearskies_api.sse import haze_condition
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        # 2025-01-31 23:50:00 UTC → 2025-02-01 04:50 in UTC+5 is wrong;
        # America/New_York in winter is UTC-5 → 2025-01-31 18:50 local.
        # Actually UTC-5: 23:50 UTC - 5h = 18:50 EST = still January.
        # Need a timestamp that crosses midnight locally.
        # Jan 31 23:50 UTC-5 would be: UTC = Feb 01 04:50.
        # So use ts for 2025-02-01 04:50:00 UTC.
        ts = 1_738_382_400.0  # 2025-02-01 04:50:00 UTC → 2025-01-31 23:50 EST (Jan)
        # Hmm — let's pick something unambiguous.
        # 2025-02-01 05:00:00 UTC = 2025-02-01 00:00 EST — right at midnight, February.
        ts = 1_738_386_000.0  # 2025-02-01 05:00:00 UTC = 2025-02-01 00:00 EST

        auto_calibration.set_timezone("America/New_York")
        self._patch_gates_at_ts(monkeypatch, ts)
        auto_calibration.process_packet({})

        assert len(auto_calibration._monthly_samples[2]) == 1, (
            "2025-02-01 05:00 UTC is Feb 01 00:00 EST → must bin to month 2"
        )
        assert len(auto_calibration._monthly_samples[1]) == 0, (
            "January bucket must be empty for this timestamp in EST"
        )

    def test_same_utc_timestamp_bins_differently_by_timezone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same UTC timestamp bins to different months depending on timezone.

        2025-01-31 22:00 UTC:
          - UTC → January (month 1)
          - America/Sao_Paulo (UTC-3) → Jan 31 19:00 local → January (month 1)
          - Pacific/Auckland (UTC+13) → Feb 01 11:00 local → February (month 2)
        """
        from weewx_clearskies_api.sse import haze_condition
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        ts = 1_738_360_800.0  # 2025-01-31 22:00:00 UTC

        # UTC → January
        auto_calibration.set_timezone("UTC")
        self._patch_gates_at_ts(monkeypatch, ts)
        auto_calibration.process_packet({})
        jan_count_utc = len(auto_calibration._monthly_samples[1])
        feb_count_utc = len(auto_calibration._monthly_samples[2])
        auto_calibration.reset()

        # Pacific/Auckland (UTC+13) → Feb 01 11:00 → February
        auto_calibration.set_timezone("Pacific/Auckland")
        self._patch_gates_at_ts(monkeypatch, ts)
        auto_calibration.process_packet({})
        jan_count_auckland = len(auto_calibration._monthly_samples[1])
        feb_count_auckland = len(auto_calibration._monthly_samples[2])

        assert jan_count_utc == 1 and feb_count_utc == 0, (
            "UTC: 2025-01-31 22:00 UTC must be January"
        )
        assert jan_count_auckland == 0 and feb_count_auckland == 1, (
            "Pacific/Auckland UTC+13: 2025-01-31 22:00 UTC → Feb 01 11:00 NZDT → February"
        )


# ===========================================================================
# Group 11: OpenAQ sensor info — set/get, state, persist/load, reset (Phase 9)
# ===========================================================================

# Realistic sensor info dict matching the shape produced by find_best_pm25_sensor().
_SENSOR_INFO = {
    "sensor_id": 9901,
    "name": "Downtown LA Monitor",
    "distance_km": 2.134,
    "lat": 34.070,
    "lon": -118.244,
}


class TestOpenAQSensorInfo:
    """set_openaq_sensor / get_openaq_sensor public API contract (Phase 9 T9.8)."""

    def test_set_then_get_returns_same_dict(self) -> None:
        """set_openaq_sensor() followed by get_openaq_sensor() returns the same dict."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        result = auto_calibration.get_openaq_sensor()
        assert result == _SENSOR_INFO, (
            "get_openaq_sensor() must return the same dict that was set"
        )

    def test_get_returns_none_after_reset(self) -> None:
        """get_openaq_sensor() returns None immediately after reset() (autouse fixture)."""
        # autouse _reset_auto_cal runs reset() before this test.
        result = auto_calibration.get_openaq_sensor()
        assert result is None

    def test_set_none_clears_stored_sensor(self) -> None:
        """set_openaq_sensor(None) clears any previously stored sensor info."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        auto_calibration.set_openaq_sensor(None)
        assert auto_calibration.get_openaq_sensor() is None

    def test_overwrite_replaces_previous_sensor(self) -> None:
        """Setting a second sensor dict overwrites the first."""
        first = {"sensor_id": 1, "name": "First", "distance_km": 1.0, "lat": 34.0, "lon": -118.0}
        second = {"sensor_id": 2, "name": "Second", "distance_km": 5.0, "lat": 34.1, "lon": -118.1}
        auto_calibration.set_openaq_sensor(first)
        auto_calibration.set_openaq_sensor(second)
        assert auto_calibration.get_openaq_sensor() == second

    def test_stored_sensor_is_not_a_copy(self) -> None:
        """set_openaq_sensor stores the dict reference (not a deep copy) — caller owns it."""
        info = dict(_SENSOR_INFO)
        auto_calibration.set_openaq_sensor(info)
        assert auto_calibration.get_openaq_sensor() is info


class TestOpenAQSensorInCalibrationState:
    """get_calibration_state() exposes openaq_sensor key correctly."""

    def test_state_includes_openaq_sensor_key(self) -> None:
        """get_calibration_state() always has an 'openaq_sensor' key."""
        state = auto_calibration.get_calibration_state()
        assert "openaq_sensor" in state, (
            "get_calibration_state() must include 'openaq_sensor' key"
        )

    def test_openaq_sensor_none_when_not_set(self) -> None:
        """openaq_sensor is None in calibration state before set_openaq_sensor() is called."""
        state = auto_calibration.get_calibration_state()
        assert state["openaq_sensor"] is None

    def test_openaq_sensor_reflects_set_value(self) -> None:
        """openaq_sensor in calibration state matches the stored sensor info dict."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        state = auto_calibration.get_calibration_state()
        assert state["openaq_sensor"] == _SENSOR_INFO

    def test_openaq_sensor_sensor_id_in_state(self) -> None:
        """openaq_sensor.sensor_id in state matches what was set."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        state = auto_calibration.get_calibration_state()
        assert state["openaq_sensor"]["sensor_id"] == 9901

    def test_openaq_sensor_name_in_state(self) -> None:
        """openaq_sensor.name in state matches what was set."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        state = auto_calibration.get_calibration_state()
        assert state["openaq_sensor"]["name"] == "Downtown LA Monitor"

    def test_openaq_sensor_distance_km_in_state(self) -> None:
        """openaq_sensor.distance_km in state matches what was set."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        state = auto_calibration.get_calibration_state()
        assert state["openaq_sensor"]["distance_km"] == pytest.approx(2.134)

    def test_openaq_sensor_cleared_after_reset(self) -> None:
        """After reset(), openaq_sensor in state is None."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        auto_calibration.reset()
        state = auto_calibration.get_calibration_state()
        assert state["openaq_sensor"] is None


class TestOpenAQSensorPersistLoad:
    """persist() / load_persisted() round-trip for openaq_sensor info."""

    def test_persist_writes_openaq_sensor_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """persist() writes openaq_sensor_id, _name, _distance_km, _lat, _lon to disk."""
        import json as _json

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))

        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        auto_calibration.persist()

        assert persist_path.exists(), "persist() must write the calibration file"
        data = _json.loads(persist_path.read_text(encoding="utf-8"))

        assert data["openaq_sensor_id"] == 9901
        assert data["openaq_sensor_name"] == "Downtown LA Monitor"
        assert data["openaq_sensor_distance_km"] == pytest.approx(2.134)
        assert data["openaq_sensor_lat"] == pytest.approx(34.070)
        assert data["openaq_sensor_lon"] == pytest.approx(-118.244)

    def test_persist_omits_openaq_fields_when_no_sensor_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """persist() does not write openaq_sensor_* keys when sensor is None."""
        import json as _json

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))

        # No set_openaq_sensor() call — _openaq_sensor stays None.
        auto_calibration.persist()

        data = _json.loads(persist_path.read_text(encoding="utf-8"))
        assert "openaq_sensor_id" not in data, (
            "openaq_sensor_id must not appear in persisted data when no sensor set"
        )

    def test_persist_load_round_trip_restores_sensor_info(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Persist sensor info, reset, load from disk — sensor info is restored."""
        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        auto_calibration.persist()

        # Reset wipes all state including sensor.
        auto_calibration.reset()
        assert auto_calibration.get_openaq_sensor() is None, (
            "Sensor info must be cleared by reset() before load_persisted()"
        )

        auto_calibration.load_persisted()

        restored = auto_calibration.get_openaq_sensor()
        assert restored is not None, (
            "load_persisted() must restore sensor info from disk"
        )
        assert restored["sensor_id"] == _SENSOR_INFO["sensor_id"]
        assert restored["name"] == _SENSOR_INFO["name"]
        assert restored["distance_km"] == pytest.approx(_SENSOR_INFO["distance_km"])
        assert restored["lat"] == pytest.approx(_SENSOR_INFO["lat"])
        assert restored["lon"] == pytest.approx(_SENSOR_INFO["lon"])

    def test_load_persisted_backward_compat_v2_without_sensor_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """v2 calibration.json without openaq_sensor fields loads without crash.

        A file written before Phase 9 has no openaq_sensor_id / _name / etc.
        load_persisted() must leave _openaq_sensor as None and not raise.
        """
        import json as _json

        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        # v2 file with no openaq_sensor fields (simulates a pre-Phase-9 save).
        v2_legacy = {
            "version": 2,
            "monthly_samples": {str(m): [] for m in range(1, 13)},
            "monthly_baselines": {str(m): None for m in range(1, 13)},
            "flat_baseline": None,
            "station_type": "Vantage",
        }
        persist_path.write_text(_json.dumps(v2_legacy), encoding="utf-8")

        # Must not raise.
        auto_calibration.load_persisted()

        assert auto_calibration.get_openaq_sensor() is None, (
            "load_persisted() must leave _openaq_sensor=None for pre-Phase-9 files"
        )

    def test_load_persisted_with_malformed_sensor_id_leaves_sensor_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """openaq_sensor_id that cannot be cast to int → _openaq_sensor stays None."""
        import json as _json

        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        bad_sensor = {
            "version": 2,
            "monthly_samples": {str(m): [] for m in range(1, 13)},
            "monthly_baselines": {str(m): None for m in range(1, 13)},
            "flat_baseline": None,
            "station_type": None,
            "openaq_sensor_id": "not-an-int",  # malformed
            "openaq_sensor_name": "Broken",
            "openaq_sensor_distance_km": 5.0,
            "openaq_sensor_lat": 34.0,
            "openaq_sensor_lon": -118.0,
        }
        persist_path.write_text(_json.dumps(bad_sensor), encoding="utf-8")

        auto_calibration.load_persisted()  # must not raise

        assert auto_calibration.get_openaq_sensor() is None, (
            "Malformed openaq_sensor_id must leave _openaq_sensor as None"
        )


class TestResetClearsSensorInfo:
    """reset() clears _openaq_sensor among all other state."""

    def test_reset_clears_sensor_info(self) -> None:
        """set_openaq_sensor() followed by reset() → get_openaq_sensor() returns None."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        assert auto_calibration.get_openaq_sensor() is not None, (
            "Pre-condition: sensor must be set before reset"
        )

        auto_calibration.reset()

        assert auto_calibration.get_openaq_sensor() is None, (
            "reset() must clear _openaq_sensor to None"
        )

    def test_reset_clears_sensor_and_all_other_state_simultaneously(self) -> None:
        """reset() clears sensor AND monthly samples AND baselines in a single call."""
        auto_calibration.set_openaq_sensor(_SENSOR_INFO)
        _inject_monthly_samples(3, _CLEAN_KCS)
        auto_calibration._monthly_baselines[3] = 0.905

        auto_calibration.reset()

        assert auto_calibration.get_openaq_sensor() is None
        total_samples = sum(
            len(auto_calibration._monthly_samples[m]) for m in range(1, 13)
        )
        assert total_samples == 0
        assert all(
            auto_calibration._monthly_baselines[m] is None for m in range(1, 13)
        )
