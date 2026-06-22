"""Unit tests for weewx_clearskies_api.sse.auto_calibration (ADR-068).

Validates:
  - _percentile() linear interpolation helper
  - _percentile_midpoint() p90/p95 midpoint computation
  - compute_baseline() primary 90-day window, 180-day fallback, and pruning
  - get_calibration_state() state transitions and sample counts
  - configure() operator parameter mapping
  - process_packet() gate sequence via mocked dependencies
  - load_persisted() and persist() via filesystem mocks

Module-level state is intentional in auto_calibration.py; the autouse fixture
calls reset() before every test to provide clean isolation.

Window note
-----------
compute_baseline() calls time.time() internally to establish the cutoff for
the 90-day and 180-day windows.  Tests that populate _samples must inject
timestamps relative to a known "now", controlled by monkeypatching time.time.
The _inject_samples() helper adds samples at timestamps that are within the
specified window relative to a frozen "now".
"""

from __future__ import annotations

import json
import time as _time_module

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
# Constants matching the module's defaults (after reset)
# ---------------------------------------------------------------------------

_WINDOW_90D_SECS = 90 * 86400.0
_WINDOW_180D_SECS = 180 * 86400.0
_MIN_SAMPLES_ACTIVE = 22
_MIN_SAMPLES_FALLBACK = 15

# Representative "clean" Kcs values for a clear, clean-air afternoon.
_CLEAN_KCS = [
    0.88, 0.89, 0.90, 0.91, 0.92, 0.87, 0.89, 0.90, 0.91, 0.93,
    0.88, 0.89, 0.91, 0.92, 0.88, 0.90, 0.91, 0.89, 0.92, 0.90,
    0.93, 0.88,  # 22 total — meets _MIN_SAMPLES_ACTIVE
]

# Frozen "now" for tests that need deterministic windows.
_FROZEN_NOW = 1_800_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_samples(
    kcs_values: list[float],
    within_days: float,
    now: float = _FROZEN_NOW,
) -> None:
    """Directly append (timestamp, kcs) samples to auto_calibration._samples.

    Distributes samples evenly across the window so they all lie within
    `within_days` days of `now`.  Samples are appended to the internal list
    in chronological order.

    Does NOT call process_packet() — bypasses all gates so tests can exercise
    compute_baseline() in isolation.
    """
    window_secs = within_days * 86400.0
    count = len(kcs_values)
    for i, kcs in enumerate(kcs_values):
        # Spread evenly within the window; newest sample at (now - 1s).
        if count == 1:
            ts = now - 1.0
        else:
            ts = now - window_secs + (window_secs / count) * (i + 1)
        auto_calibration._samples.append((ts, kcs))


# ===========================================================================
# Group 1: _percentile() linear interpolation
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
        """Percentile 95 of [0.80, 0.85, 0.90, 0.95] → ≈ 0.9625.

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
# Group 2: _percentile_midpoint()
# ===========================================================================


class TestPercentileMidpoint:
    """_percentile_midpoint() returns the midpoint of p90 and p95."""

    def test_uniform_values_midpoint_is_value(self) -> None:
        """All identical values → p90 = p95 = value → midpoint = value."""
        values = [0.90] * 30
        result = auto_calibration._percentile_midpoint(values)
        assert result == pytest.approx(0.90)

    def test_known_values_midpoint(self) -> None:
        """Known sorted values → verify midpoint computation.

        Uses [0.80, 0.85, 0.90, 0.95] with default _PERCENTILE_LOW=90, _PERCENTILE_HIGH=95.
        p90 = 0.935 (from TestPercentileHelper test_four_values_at_percentile_90)
        p95 = 0.9425
        midpoint = (0.935 + 0.9425) / 2 = 0.93875
        """
        values = [0.80, 0.85, 0.90, 0.95]
        result = auto_calibration._percentile_midpoint(values)
        assert result == pytest.approx(0.93875)

    def test_midpoint_does_not_depend_on_input_order(self) -> None:
        """_percentile_midpoint() sorts internally — order must not affect result."""
        forward = [0.80, 0.85, 0.90, 0.95]
        backward = [0.95, 0.90, 0.85, 0.80]
        assert auto_calibration._percentile_midpoint(forward) == pytest.approx(
            auto_calibration._percentile_midpoint(backward)
        )

    def test_midpoint_with_realistic_kcs_values(self) -> None:
        """22 realistic clean-sky Kcs values produce a baseline in [0.85, 1.0]."""
        result = auto_calibration._percentile_midpoint(_CLEAN_KCS)
        assert 0.85 <= result <= 1.0, (
            f"Expected baseline in [0.85, 1.0] for realistic Kcs values, got {result}"
        )


# ===========================================================================
# Group 3: compute_baseline()
# ===========================================================================


class TestComputeBaseline:
    """compute_baseline() primary window, fallback, empty, and pruning cases."""

    def test_empty_samples_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No samples → compute_baseline() returns None."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        result = auto_calibration.compute_baseline()
        assert result is None, "Empty sample list must return None"

    def test_below_min_samples_primary_and_fallback_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """< 22 samples in 90d AND < 15 in 180d → bootstrapping, returns None."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        # Inject 10 samples within 90 days (below both thresholds).
        _inject_samples([0.90] * 10, within_days=60.0)
        result = auto_calibration.compute_baseline()
        assert result is None, (
            "10 samples in 90d window (< 22) must not activate baseline"
        )

    def test_22_samples_in_90d_returns_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """22 samples within 90 days → primary window active, returns baseline float."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        _inject_samples(_CLEAN_KCS, within_days=89.0)  # 22 samples, within 90d
        result = auto_calibration.compute_baseline()
        assert result is not None, "22 samples in 90d must activate baseline"
        assert isinstance(result, float), "compute_baseline() must return a float"
        assert 0.80 <= result <= 1.05, (
            f"Expected baseline in plausible range [0.80, 1.05], got {result}"
        )

    def test_below_22_in_90d_but_15_in_180d_uses_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """< 22 in 90d but ≥ 15 in 180d → fallback window used, returns baseline."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        # Inject 5 samples in the 90d window and 15 more in the 91-180d range.
        _inject_samples([0.90] * 5, within_days=89.0)
        # 15 additional samples between 91 and 170 days old.
        window_secs = 170 * 86400.0
        count = 15
        for i in range(count):
            ts = _FROZEN_NOW - window_secs + (window_secs / count) * (i + 1)
            auto_calibration._samples.append((ts, 0.88))
        result = auto_calibration.compute_baseline()
        assert result is not None, (
            "5 samples in 90d + 15 in 90-180d range must use fallback window"
        )

    def test_stale_samples_outside_180d_not_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Samples older than 180d are pruned by process_packet() and not counted."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        # Inject 20 samples that are 200 days old (outside 180d fallback window).
        stale_ts = _FROZEN_NOW - 200 * 86400.0
        for i in range(20):
            auto_calibration._samples.append((stale_ts + i, 0.90))
        # These are outside the 180d window — should not be counted.
        result = auto_calibration.compute_baseline()
        assert result is None, (
            "20 samples outside 180d fallback window must not produce a baseline"
        )

    def test_50_samples_produces_well_calibrated_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """50 samples within 90 days → well-calibrated state, returns baseline."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        _inject_samples([0.88 + (i % 10) * 0.005 for i in range(50)], within_days=89.0)
        result = auto_calibration.compute_baseline()
        assert result is not None, "50 samples in 90d must produce a baseline"

    def test_baseline_value_increases_with_higher_kcs_samples(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Higher Kcs values produce a higher baseline (monotone relationship)."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        auto_calibration.reset()
        _inject_samples([0.80] * 22, within_days=89.0)
        baseline_low = auto_calibration.compute_baseline()

        auto_calibration.reset()
        _inject_samples([0.95] * 22, within_days=89.0)
        baseline_high = auto_calibration.compute_baseline()

        assert baseline_low is not None and baseline_high is not None
        assert baseline_high > baseline_low, (
            "Higher Kcs samples must produce a higher computed baseline"
        )


# ===========================================================================
# Group 4: get_calibration_state()
# ===========================================================================


class TestGetCalibrationState:
    """State transitions: bootstrapping → calibrated → well-calibrated."""

    def test_no_samples_bootstrapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No samples → state='bootstrapping', counts=0, baseline_kcs=None."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        state = auto_calibration.get_calibration_state()
        assert state["state"] == "bootstrapping"
        assert state["sample_count_90d"] == 0
        assert state["sample_count_180d"] == 0
        assert state["baseline_kcs"] is None

    def test_10_samples_bootstrapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """10 samples in 90d window (< 22) → state='bootstrapping'."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        _inject_samples([0.90] * 10, within_days=89.0)
        state = auto_calibration.get_calibration_state()
        assert state["state"] == "bootstrapping"
        assert state["sample_count_90d"] == 10

    def test_22_samples_calibrated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """22 samples in 90d window → state='calibrated'."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        _inject_samples(_CLEAN_KCS, within_days=89.0)  # exactly 22 samples
        state = auto_calibration.get_calibration_state()
        assert state["state"] == "calibrated", (
            f"Expected 'calibrated' with 22 samples, got {state['state']!r}"
        )
        assert state["sample_count_90d"] == 22

    def test_51_samples_well_calibrated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """51 samples in 90d window → state='well-calibrated'."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        _inject_samples([0.90] * 51, within_days=89.0)
        state = auto_calibration.get_calibration_state()
        assert state["state"] == "well-calibrated", (
            f"Expected 'well-calibrated' with 51 samples, got {state['state']!r}"
        )
        assert state["sample_count_90d"] == 51

    def test_sample_count_180d_includes_older_samples(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sample_count_180d includes samples from the 91-180 day range."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        # 5 samples in the 90d window.
        _inject_samples([0.90] * 5, within_days=89.0)
        # 10 samples between 100 and 170 days old.
        for i in range(10):
            ts = _FROZEN_NOW - (100 + i * 7) * 86400.0
            auto_calibration._samples.append((ts, 0.88))
        state = auto_calibration.get_calibration_state()
        assert state["sample_count_90d"] == 5
        assert state["sample_count_180d"] >= 10, (
            "sample_count_180d must include samples from 90-180 day range"
        )

    def test_last_updated_none_before_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """last_updated=None when no persist() has been called (_last_persist_time=0)."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        state = auto_calibration.get_calibration_state()
        assert state["last_updated"] is None, (
            "last_updated must be None when no persist() has been called"
        )

    def test_baseline_kcs_reflects_current_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After process_packet() sets a baseline, get_calibration_state() reflects it."""
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)
        # Manually set the internal baseline (simulates process_packet() output).
        auto_calibration._current_baseline = 0.912
        state = auto_calibration.get_calibration_state()
        assert state["baseline_kcs"] == pytest.approx(0.912)


# ===========================================================================
# Group 5: configure()
# ===========================================================================


class TestConfigure:
    """configure() maps operator parameters to module constants."""

    def test_configure_percentile_0_92_maps_to_89_94(self) -> None:
        """configure(percentile=0.92) → _PERCENTILE_LOW=89, _PERCENTILE_HIGH=94.

        0.92 - 0.025 = 0.895 → int(0.895 * 100) = 89
        0.92 + 0.025 = 0.945 → int(0.945 * 100) = 94
        """
        auto_calibration.configure(percentile=0.92)
        assert auto_calibration._PERCENTILE_LOW == 89
        assert auto_calibration._PERCENTILE_HIGH == 94

    def test_configure_percentile_0_90_maps_to_87_92(self) -> None:
        """configure(percentile=0.90) → _PERCENTILE_LOW=87, _PERCENTILE_HIGH=92.

        0.90 - 0.025 = 0.875 → int(0.875 * 100) = 87
        0.90 + 0.025 = 0.925 → int(0.925 * 100) = 92
        """
        auto_calibration.configure(percentile=0.90)
        assert auto_calibration._PERCENTILE_LOW == 87
        assert auto_calibration._PERCENTILE_HIGH == 92

    def test_configure_window_days_changes_primary_window(self) -> None:
        """configure(window_days=60) → _WINDOW_DAYS_PRIMARY=60, _WINDOW_PRIMARY_SECS=5184000."""
        auto_calibration.configure(window_days=60)
        assert auto_calibration._WINDOW_DAYS_PRIMARY == 60
        assert auto_calibration._WINDOW_PRIMARY_SECS == pytest.approx(60 * 86400.0)

    def test_configure_min_samples_changes_activation_threshold(self) -> None:
        """configure(min_samples=30) → _MIN_SAMPLES_ACTIVE=30."""
        auto_calibration.configure(min_samples=30)
        assert auto_calibration._MIN_SAMPLES_ACTIVE == 30

    def test_configure_defaults_unchanged_when_not_specified(self) -> None:
        """configure() with no args applies default percentile=0.92, window=90, min=22."""
        auto_calibration.configure()
        # Default: percentile=0.92 → LOW=89, HIGH=94
        assert auto_calibration._PERCENTILE_LOW == 89
        assert auto_calibration._PERCENTILE_HIGH == 94
        assert auto_calibration._MIN_SAMPLES_ACTIVE == 22
        assert auto_calibration._WINDOW_DAYS_PRIMARY == 90

    def test_configure_window_days_affects_baseline_cutoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """configure(window_days=30) with 22 samples in 30d → baseline active.

        With default 90d window, samples 35 days old would be in the primary window.
        After configure(30), samples 35 days old are outside the new 30d window.
        """
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        # Inject 22 samples that are 35 days old — within default 90d but outside 30d.
        ts_35d_ago = _FROZEN_NOW - 35 * 86400.0
        for i in range(22):
            auto_calibration._samples.append((ts_35d_ago + i * 60, 0.90))

        # With default 90d window, these should be in the primary window.
        baseline_default = auto_calibration.compute_baseline()
        assert baseline_default is not None, "22 samples at 35d should be in 90d window"

        # After configure(30d), the same samples are outside the primary window.
        auto_calibration.configure(window_days=30)
        baseline_narrow = auto_calibration.compute_baseline()
        # 22 samples at 35d old are outside the 30d window.
        # Fallback (180d) has 22 samples, which is ≥ 15 → fallback activates.
        # The fallback window is fixed at 180d per the module.
        assert baseline_narrow is not None, (
            "22 samples at 35d are in 180d fallback window, baseline should activate via fallback"
        )


# ===========================================================================
# Group 6: process_packet() — gate sequence via mocks
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
        now: float = _FROZEN_NOW + _WINDOW_90D_SECS,  # past rain holdoff
    ) -> None:
        """Patch all external dependencies so process_packet() can run cleanly."""
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

    def test_clean_packet_collects_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All gates pass + clean PM → Kcs sample is appended to _samples."""
        self._patch_all_gates(monkeypatch)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 1, (
            "Clean packet must append exactly one Kcs sample"
        )
        assert auto_calibration._samples[0][1] == pytest.approx(0.90)

    def test_rain_gate_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rain active → Gate 1 fires, no sample collected."""
        self._patch_all_gates(monkeypatch, rain_rate=0.1)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "Active rain must suppress Kcs sample collection"
        )

    def test_low_elevation_gate_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """solar_elevation ≤ 10° → Gate 2 fires, no sample collected."""
        self._patch_all_gates(monkeypatch, elevation=5.0)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "Low solar elevation must suppress Kcs sample collection"
        )

    def test_none_elevation_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """solar_elevation=None → Gate 2 fires, no sample collected."""
        self._patch_all_gates(monkeypatch, elevation=None)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "None solar elevation must suppress Kcs sample collection"
        )

    def test_cloudy_sky_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloudy sky label → Gate 3 fires (no clean substring), no sample collected."""
        self._patch_all_gates(monkeypatch, sky_label="Cloudy")
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "Cloudy sky must suppress Kcs sample collection"
        )

    def test_none_sky_label_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sky_label=None → Gate 3 fires (cannot verify sky), no sample collected."""
        self._patch_all_gates(monkeypatch, sky_label=None)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "None sky label (startup) must suppress Kcs sample collection"
        )

    def test_dirty_pm25_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PM2.5 ≥ 12 µg/m³ → Gate 4 fires (dirty air), no sample collected."""
        self._patch_all_gates(monkeypatch, pm25=15.0)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "PM2.5 ≥ 12 µg/m³ must suppress Kcs sample collection"
        )

    def test_dirty_pm10_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PM10 ≥ 50 µg/m³ → Gate 4 fires (dirty air), no sample collected."""
        self._patch_all_gates(monkeypatch, pm10=55.0)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "PM10 ≥ 50 µg/m³ must suppress Kcs sample collection"
        )

    def test_none_pm_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pm25=None or pm10=None → Gate 4 fires, no sample collected.

        Cannot confirm clean atmosphere without both PM channels.
        """
        self._patch_all_gates(monkeypatch, pm25=None, pm10=None)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "None PM data must suppress Kcs sample collection (cannot confirm clean air)"
        )

    def test_none_kcs_suppresses_sample_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """kcs=None → no Kcs value available, sample not appended."""
        self._patch_all_gates(monkeypatch, kcs=None)
        auto_calibration.process_packet({})
        assert len(auto_calibration._samples) == 0, (
            "None Kcs must suppress sample collection"
        )

    def test_baseline_update_calls_haze_condition_set_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After 22+ samples, process_packet() calls haze_condition.set_baseline()."""
        from weewx_clearskies_api.sse import haze_condition

        set_baseline_calls: list[float] = []

        def _record_set_baseline(value: float) -> None:
            set_baseline_calls.append(value)
            haze_condition._clean_kcs_baseline = value

        monkeypatch.setattr(haze_condition, "set_baseline", _record_set_baseline)

        # Inject 21 samples directly (bypass process_packet() gates for speed).
        now = _FROZEN_NOW + _WINDOW_90D_SECS
        for i in range(21):
            auto_calibration._samples.append((now - (21 - i) * 60, 0.90))

        # The 22nd sample via process_packet() should trigger baseline update.
        self._patch_all_gates(monkeypatch, now=now)
        auto_calibration.process_packet({})

        assert len(set_baseline_calls) >= 1, (
            "process_packet() must call haze_condition.set_baseline() after 22nd sample"
        )
        assert set_baseline_calls[-1] > 0.0, "Baseline value must be positive"


# ===========================================================================
# Group 7: load_persisted() and persist() — filesystem mocks
# ===========================================================================


class TestPersistence:
    """Filesystem-based persistence via tmp file and atomic rename."""

    def test_persist_writes_valid_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """persist() writes a valid JSON file at the configured path."""
        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        auto_calibration._samples = [(_FROZEN_NOW - 100, 0.90), (_FROZEN_NOW - 50, 0.92)]
        auto_calibration._current_baseline = 0.91

        auto_calibration.persist()

        assert persist_path.exists(), "persist() must create the calibration file"
        data = json.loads(persist_path.read_text(encoding="utf-8"))
        assert "samples" in data
        assert "baseline_kcs" in data
        assert data["baseline_kcs"] == pytest.approx(0.91)
        assert len(data["samples"]) == 2

    def test_load_persisted_restores_samples_and_baseline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() restores samples and baseline from a valid JSON file."""
        from weewx_clearskies_api.sse import haze_condition

        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        # Write a fixture file with 3 recent samples and a baseline.
        samples = [
            [_FROZEN_NOW - 100 * 86400.0 + i * 3600, 0.89 + i * 0.005]
            for i in range(3)
        ]
        persist_path.write_text(
            json.dumps({"samples": samples, "baseline_kcs": 0.905}),
            encoding="utf-8",
        )

        # Track haze_condition.set_baseline() calls.
        set_baseline_calls: list[float] = []

        def _record(val: float) -> None:
            set_baseline_calls.append(val)

        monkeypatch.setattr(haze_condition, "set_baseline", _record)

        auto_calibration.load_persisted()

        assert len(auto_calibration._samples) == 3, (
            "load_persisted() must restore all valid samples"
        )
        assert auto_calibration._current_baseline == pytest.approx(0.905)
        assert len(set_baseline_calls) == 1, (
            "load_persisted() must call haze_condition.set_baseline() with restored baseline"
        )

    def test_load_persisted_prunes_stale_samples(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() discards samples older than 180 days."""
        persist_path = tmp_path / "calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        stale_ts = _FROZEN_NOW - 200 * 86400.0  # 200 days ago, outside 180d window
        fresh_ts = _FROZEN_NOW - 30 * 86400.0   # 30 days ago, within 180d window
        persist_path.write_text(
            json.dumps({
                "samples": [[stale_ts, 0.88], [fresh_ts, 0.90]],
                "baseline_kcs": None,
            }),
            encoding="utf-8",
        )

        from weewx_clearskies_api.sse import haze_condition
        monkeypatch.setattr(haze_condition, "set_baseline", lambda v: None)

        auto_calibration.load_persisted()

        assert len(auto_calibration._samples) == 1, (
            "load_persisted() must prune samples older than 180 days"
        )
        assert auto_calibration._samples[0][0] == pytest.approx(fresh_ts)

    def test_load_persisted_missing_file_starts_fresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() with a missing file → starts fresh (no samples, no baseline)."""
        persist_path = tmp_path / "nonexistent_calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        auto_calibration.load_persisted()

        assert len(auto_calibration._samples) == 0
        assert auto_calibration._current_baseline is None

    def test_load_persisted_invalid_json_starts_fresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """load_persisted() with corrupt JSON → starts fresh (no crash)."""
        persist_path = tmp_path / "calibration.json"
        persist_path.write_text("not valid json at all }{", encoding="utf-8")
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", str(persist_path))
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        auto_calibration.load_persisted()  # must not raise

        assert len(auto_calibration._samples) == 0
        assert auto_calibration._current_baseline is None

    def test_persist_failure_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """persist() to an unwritable path logs a warning but does not raise."""
        # Use a path under a nonexistent parent directory.
        bad_path = "/nonexistent_root_dir_clearskies_test/calibration.json"
        monkeypatch.setattr(auto_calibration, "_PERSIST_PATH", bad_path)
        monkeypatch.setattr(auto_calibration.time, "time", lambda: _FROZEN_NOW)

        auto_calibration._samples = [(_FROZEN_NOW - 100, 0.90)]
        # Must not raise.
        auto_calibration.persist()
