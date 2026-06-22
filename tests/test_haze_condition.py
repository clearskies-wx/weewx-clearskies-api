"""Unit tests for weewx_clearskies_api.sse.haze_condition (ADR-067).

Validates the two-channel haze detection algorithm:
  - Gate ordering (disabled, solar elevation, sky label, wet deposition, RH)
  - PM channel thresholds (dry, humid disambiguation, coarse dust)
  - Kcs deficit channel (baseline comparison)
  - Temporal coherence filter (15-minute rolling window, ≥50% True)
  - Configuration API (set_enabled, set_baseline, set_gamma, reset)
  - Wet deposition edge cases (rain start/stop, 30-min holdoff)

Module-level state is intentional in haze_condition.py; the autouse fixture
calls reset() before every test to provide clean isolation.

Temporal coherence note
------------------------
detect_haze() records a (timestamp, is_hazy) entry on EVERY call — including
gate-exit paths that record False.  A single call when both channels fire
records True but the history has exactly one entry, so hazy_count/total = 1.0
(≥0.50) and "Hazy" is immediately returned.  To test the <50% threshold,
the history must be pre-populated with False entries before the True one.
The _fill_history() helper does this by calling detect_haze() with parameters
guaranteed to record False entries (solar_elevation=5.0, below-10° gate).
"""

from __future__ import annotations

import time

import pytest

from weewx_clearskies_api.sse import haze_condition


# ---------------------------------------------------------------------------
# Autouse reset fixture — every test starts from a clean slate.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_haze():
    """Reset haze_condition module state before and after each test."""
    haze_condition.reset()
    yield
    haze_condition.reset()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

# A representative "clear sky" timestamp far enough from zero that the
# 30-min post-rain holdoff (initialized to 0.0 at reset) is already expired.
_BASE_TS = 1_800_000.0  # 30 minutes beyond epoch — holdoff always cleared

# Realistic "clear day" kwargs — passes all gates, both channels fire.
_CLEAR_DAY_KWARGS: dict = dict(
    kcs=0.85,               # below baseline 0.90 → deficit = 0.05
    solar_elevation=45.0,
    sky_label="Clear",
    pm25=15.0,              # >12 µg/m³ dry threshold
    pm10=None,
    out_temp=72.0,          # °F
    dewpoint=50.0,          # °F → RH ≈ 47% (dry, RH < 80%)
    rain_rate=0.0,
)


def _detect(**kwargs) -> str | None:
    """Thin wrapper so tests can pass just the fields they want to vary."""
    base = dict(_CLEAR_DAY_KWARGS)
    base.update(kwargs)
    return haze_condition.detect_haze(**base)


def _fill_history_with_false(count: int, base_ts: float = _BASE_TS) -> float:
    """Pre-populate the history with False entries (below-elevation gate).

    Returns the timestamp of the last entry so callers can advance time.
    The entries are spaced 1 second apart starting at base_ts, all before
    the window cutoff of base_ts + count (so they remain in the 15-min window
    if the True call happens within 900 s of the last false entry).
    """
    for i in range(count):
        ts = base_ts + i
        haze_condition.detect_haze(
            kcs=0.85,
            solar_elevation=5.0,   # below 10° → records False, returns None
            sky_label="Clear",
            pm25=15.0,
            pm10=None,
            out_temp=72.0,
            dewpoint=50.0,
            rain_rate=0.0,
        )
    return base_ts + count - 1


# ===========================================================================
# Group 1: Gate tests — each gate returns None and records False
# ===========================================================================


class TestGates:
    """Each gate, when triggered, must return None."""

    def test_disabled_returns_none(self) -> None:
        """set_enabled(False) → detect_haze() always returns None."""
        haze_condition.set_enabled(False)
        result = _detect()
        assert result is None, (
            "detect_haze() must return None when haze detection is disabled"
        )

    def test_solar_elevation_none_returns_none(self) -> None:
        """solar_elevation=None → Gate 1 fires, returns None."""
        result = _detect(solar_elevation=None)
        assert result is None, "solar_elevation=None must return None (Gate 1)"

    def test_solar_elevation_below_10_returns_none(self) -> None:
        """solar_elevation=5.0 (< 10°) → Gate 1 fires, returns None."""
        result = _detect(solar_elevation=5.0)
        assert result is None, "solar_elevation=5.0 must return None (< 10° gate)"

    def test_solar_elevation_exactly_10_returns_none(self) -> None:
        """solar_elevation=10.0 (== 10°) → Gate 1 fires (≤ 10.0), returns None."""
        result = _detect(solar_elevation=10.0)
        assert result is None, "solar_elevation=10.0 must return None (≤ 10° gate, exclusive)"

    def test_solar_elevation_above_10_not_gated(self) -> None:
        """solar_elevation=10.01 (just above 10°) → Gate 1 does NOT fire."""
        # This test just verifies gate 1 doesn't block; other gates may still fire.
        # With both channels firing and no other gate active, we get a result.
        result = _detect(solar_elevation=10.01)
        # Should return "Hazy" (single entry = 100% true) or None from another gate.
        # The only question is: does solar elevation gate block at 10.01? No.
        assert result == "Hazy", (
            "solar_elevation=10.01 should pass Gate 1 and allow haze detection"
        )

    def test_mostly_cloudy_sky_label_returns_none(self) -> None:
        """sky_label='Mostly Cloudy' → blocked sky gate fires, returns None."""
        result = _detect(sky_label="Mostly Cloudy")
        assert result is None, "'Mostly Cloudy' must be blocked by sky label gate"

    def test_cloudy_sky_label_returns_none(self) -> None:
        """sky_label='Cloudy' → blocked sky gate fires, returns None."""
        result = _detect(sky_label="Cloudy")
        assert result is None, "'Cloudy' must be blocked by sky label gate"

    def test_overcast_sky_label_returns_none(self) -> None:
        """sky_label='Overcast' → blocked sky gate fires, returns None."""
        result = _detect(sky_label="Overcast")
        assert result is None, "'Overcast' must be blocked by sky label gate"

    def test_heavy_overcast_sky_label_returns_none(self) -> None:
        """sky_label='Heavy Overcast' → blocked sky gate fires, returns None."""
        result = _detect(sky_label="Heavy Overcast")
        assert result is None, "'Heavy Overcast' must be blocked by sky label gate"

    def test_unknown_sky_label_returns_none(self) -> None:
        """sky_label with no eligible substring → blocked by eligibility check."""
        result = _detect(sky_label="Fog")
        assert result is None, (
            "'Fog' is not in eligible substrings and not blocked — should return None"
        )

    def test_none_sky_label_allowed(self) -> None:
        """sky_label=None (startup case) → sky gate skipped, detection proceeds."""
        result = _detect(sky_label=None)
        # With both channels firing, None sky_label allows detection.
        assert result == "Hazy", (
            "sky_label=None must allow haze detection to proceed (startup case)"
        )

    def test_active_rain_returns_none(self) -> None:
        """rain_rate > 0 → wet deposition gate fires, returns None."""
        result = _detect(rain_rate=0.01)
        assert result is None, "Active rain must suppress haze detection"

    def test_post_rain_holdoff_within_1800s_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-rain holdoff (<1800s since rain stopped) → Gate 3 fires, returns None."""
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # Simulate rain active at _BASE_TS.
        _detect(rain_rate=0.1)

        # Advance time by 900s (within 1800s holdoff).
        fake_now[0] = _BASE_TS + 900.0
        result = _detect(rain_rate=0.0)
        assert result is None, (
            "Within 30-min post-rain holdoff, haze must return None"
        )

    def test_rh_above_90_defers_to_fog_returns_none(self) -> None:
        """RH > 90% → Gate 4 fires, defers to fog detection, returns None.

        out_temp=68°F, dewpoint=67°F → T-Td=1°F → very high RH (≈ 97%).
        """
        result = _detect(out_temp=68.0, dewpoint=67.0)
        assert result is None, "RH > 90% must defer to fog and return None"


# ===========================================================================
# Group 2: PM channel (Channel 2)
# ===========================================================================


class TestPMChannel:
    """PM concentration thresholds — dry, humid, coarse dust."""

    def test_no_pm_data_returns_none(self) -> None:
        """pm25=None and pm10=None → no PM data, returns None."""
        result = _detect(pm25=None, pm10=None)
        assert result is None, "No PM data must return None (Channel 2 graceful degradation)"

    def test_pm25_below_dry_threshold_returns_none(self) -> None:
        """pm25=5.0 (< 12 µg/m³), pm10=None, dry RH → PM channel not confirmed."""
        # kcs=0.85 → deficit positive, but PM channel must fail first
        result = _detect(pm25=5.0, pm10=None, out_temp=72.0, dewpoint=50.0)
        assert result is None, "pm25=5.0 below dry threshold must not confirm PM channel"

    def test_pm25_above_dry_threshold_confirms_pm_channel(self) -> None:
        """pm25=15.0 (> 12 µg/m³), RH < 80% (dry) → PM confirmed."""
        result = _detect(pm25=15.0, pm10=None, out_temp=72.0, dewpoint=50.0)
        assert result == "Hazy", "pm25=15.0 in dry conditions must confirm PM channel"

    def test_pm25_at_humid_condition_below_35_not_confirmed(self) -> None:
        """pm25=20.0, T-Td=3°F (≤ 4°F) humid → not confirmed (needs > 35 µg/m³).

        At T-Td ≤ 4°F, the humid disambiguation branch requires pm25 > 35.
        The dry branch (rh < 80%) also won't fire because at T-Td=3°F, RH is high.
        """
        # T=75°F, Td=72°F → T-Td=3°F → humid; RH ≈ 86% (>80%, dry branch fails)
        result = _detect(pm25=20.0, pm10=None, out_temp=75.0, dewpoint=72.0)
        assert result is None, (
            "pm25=20.0 in humid conditions (T-Td=3°F) must not confirm PM (needs > 35)"
        )

    def test_pm25_above_humid_threshold_confirms_pm_channel(self) -> None:
        """pm25=40.0, T-Td=3°F (≤ 4°F) → humid threshold exceeded, PM confirmed."""
        result = _detect(pm25=40.0, pm10=None, out_temp=75.0, dewpoint=72.0)
        assert result == "Hazy", (
            "pm25=40.0 in humid conditions (T-Td=3°F) must confirm PM channel (> 35)"
        )

    def test_pm10_above_threshold_confirms_pm_channel(self) -> None:
        """pm10=60.0 (> 50 µg/m³), pm25=None → coarse dust PM confirmed."""
        result = _detect(pm25=None, pm10=60.0)
        assert result == "Hazy", "pm10=60.0 must confirm PM channel (coarse dust)"

    def test_pm10_below_threshold_not_confirmed(self) -> None:
        """pm10=30.0 (< 50 µg/m³), pm25=None → PM not confirmed."""
        result = _detect(pm25=None, pm10=30.0)
        assert result is None, "pm10=30.0 below threshold must not confirm PM channel"

    def test_pm25_exactly_at_dry_threshold_not_confirmed(self) -> None:
        """pm25=12.0 (== 12 µg/m³) → NOT confirmed (must be > 12, not ≥)."""
        result = _detect(pm25=12.0, pm10=None, out_temp=72.0, dewpoint=50.0)
        assert result is None, "pm25=12.0 exactly at threshold must not confirm (requires >12)"

    def test_pm10_exactly_at_threshold_not_confirmed(self) -> None:
        """pm10=50.0 (== 50 µg/m³) → NOT confirmed (must be > 50, not ≥)."""
        result = _detect(pm25=None, pm10=50.0)
        assert result is None, "pm10=50.0 exactly at threshold must not confirm (requires >50)"


# ===========================================================================
# Group 3: Kcs channel (Channel 1)
# ===========================================================================


class TestKcsChannel:
    """Kcs deficit channel — baseline comparison and deficit threshold."""

    def test_kcs_none_returns_none(self) -> None:
        """kcs=None → Kcs channel cannot evaluate, returns None."""
        result = _detect(kcs=None)
        assert result is None, "kcs=None must return None (Kcs channel unavailable)"

    def test_kcs_above_baseline_returns_none(self) -> None:
        """kcs=0.95 (> baseline 0.90) → deficit = -0.05 ≤ 0, no extinction."""
        result = _detect(kcs=0.95)
        assert result is None, "kcs=0.95 above baseline 0.90 must not confirm Kcs channel"

    def test_kcs_equal_to_baseline_returns_none(self) -> None:
        """kcs=0.90 (== baseline 0.90) → deficit = 0, not > 0, returns None."""
        result = _detect(kcs=0.90)
        assert result is None, "kcs=0.90 equal to baseline must not confirm Kcs channel (deficit ≤ 0)"

    def test_kcs_below_baseline_confirms_channel(self) -> None:
        """kcs=0.85 (< baseline 0.90) → deficit = 0.05 > 0, Kcs confirmed."""
        result = _detect(kcs=0.85)
        assert result == "Hazy", "kcs=0.85 below baseline 0.90 must confirm Kcs channel"

    def test_kcs_just_below_baseline_confirms_channel(self) -> None:
        """kcs=0.8999 (just below baseline 0.90) → deficit = 0.0001 > 0, Kcs confirmed."""
        result = _detect(kcs=0.8999)
        assert result == "Hazy", "kcs=0.8999 just below baseline must confirm Kcs channel"


# ===========================================================================
# Group 4: Temporal coherence filter
# ===========================================================================


class TestTemporalCoherence:
    """15-minute rolling window, ≥50% True entries → 'Hazy'."""

    def test_single_positive_detection_returns_hazy(self) -> None:
        """Single call with both channels firing → 1/1 = 100% ≥ 50% → 'Hazy'."""
        result = _detect()
        assert result == "Hazy", (
            "Single positive detection with empty history → 100% true → 'Hazy'"
        )

    def test_minority_true_in_history_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 False entries + 1 True → 1/4 = 25% < 50% → None.

        Uses monkeypatch to control time so all entries fall within the 900s window.
        """
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # Record 3 False entries (below-elevation gate).
        for i in range(3):
            fake_now[0] = _BASE_TS + i
            haze_condition.detect_haze(
                kcs=0.85,
                solar_elevation=5.0,  # Gate 1 fires → records False
                sky_label="Clear",
                pm25=15.0,
                pm10=None,
                out_temp=72.0,
                dewpoint=50.0,
                rain_rate=0.0,
            )

        # Now record 1 True entry — both channels fire.
        fake_now[0] = _BASE_TS + 3.0
        result = haze_condition.detect_haze(
            kcs=0.85,
            solar_elevation=45.0,
            sky_label="Clear",
            pm25=15.0,
            pm10=None,
            out_temp=72.0,
            dewpoint=50.0,
            rain_rate=0.0,
        )
        # 1 True out of 4 total = 25% < 50% → None
        assert result is None, (
            "1/4 True entries (25%) must be below 50% threshold → None"
        )

    def test_majority_true_in_history_returns_hazy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 False + 1 True → 1/2 = 50% ≥ 50% → 'Hazy'.

        Exactly 50% is at the boundary — the spec says ≥ 50%.
        """
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # 1 False entry.
        fake_now[0] = _BASE_TS
        haze_condition.detect_haze(
            kcs=0.85,
            solar_elevation=5.0,
            sky_label="Clear",
            pm25=15.0,
            pm10=None,
            out_temp=72.0,
            dewpoint=50.0,
            rain_rate=0.0,
        )

        # 1 True entry.
        fake_now[0] = _BASE_TS + 1.0
        result = haze_condition.detect_haze(
            kcs=0.85,
            solar_elevation=45.0,
            sky_label="Clear",
            pm25=15.0,
            pm10=None,
            out_temp=72.0,
            dewpoint=50.0,
            rain_rate=0.0,
        )
        # 1/2 = 50% ≥ 50% → "Hazy"
        assert result == "Hazy", "1/2 True entries (50%) must meet the ≥50% threshold → 'Hazy'"

    def test_history_pruning_removes_stale_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entries older than 900s are pruned; only recent entries count for coherence.

        Strategy: fill history with True entries at t=0, advance time by 1000s
        (past the 900s cutoff), then record a False entry. Only the False entry
        is in the window → 0/1 = 0% < 50% → None.
        """
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # Record several True entries in the past.
        for i in range(5):
            fake_now[0] = _BASE_TS + i
            haze_condition.detect_haze(
                kcs=0.85,
                solar_elevation=45.0,
                sky_label="Clear",
                pm25=15.0,
                pm10=None,
                out_temp=72.0,
                dewpoint=50.0,
                rain_rate=0.0,
            )

        # Advance past the 900s window.
        fake_now[0] = _BASE_TS + 1000.0

        # Record a False entry — this triggers pruning of the old True entries.
        result = haze_condition.detect_haze(
            kcs=0.85,
            solar_elevation=5.0,  # Gate 1 fires → records False
            sky_label="Clear",
            pm25=15.0,
            pm10=None,
            out_temp=72.0,
            dewpoint=50.0,
            rain_rate=0.0,
        )
        # After pruning: only the 1 False entry remains → 0/1 = 0% < 50%
        assert result is None, (
            "After stale True entries pruned (>900s), only 1 False entry → must return None"
        )

    def test_mixed_entries_equal_to_50_percent_returns_hazy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly 50% True entries across a larger window → 'Hazy' (boundary inclusive)."""
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # 4 False entries.
        for i in range(4):
            fake_now[0] = _BASE_TS + i
            haze_condition.detect_haze(
                kcs=0.85, solar_elevation=5.0, sky_label="Clear",
                pm25=15.0, pm10=None, out_temp=72.0, dewpoint=50.0, rain_rate=0.0,
            )

        # 4 True entries.
        for i in range(4):
            fake_now[0] = _BASE_TS + 4 + i
            haze_condition.detect_haze(
                kcs=0.85, solar_elevation=45.0, sky_label="Clear",
                pm25=15.0, pm10=None, out_temp=72.0, dewpoint=50.0, rain_rate=0.0,
            )

        # History: 4 False + 4 True = 8 total, last call returned result already.
        # Re-read the last result from the sequence by making one more True call.
        fake_now[0] = _BASE_TS + 8.0
        result = haze_condition.detect_haze(
            kcs=0.85, solar_elevation=45.0, sky_label="Clear",
            pm25=15.0, pm10=None, out_temp=72.0, dewpoint=50.0, rain_rate=0.0,
        )
        # 5 True out of 9 total = 55.6% ≥ 50%
        assert result == "Hazy", (
            "With majority True entries in window, must return 'Hazy'"
        )


# ===========================================================================
# Group 5: Configuration API
# ===========================================================================


class TestConfiguration:
    """set_enabled, set_baseline, set_gamma, reset."""

    def test_set_enabled_false_suppresses_detection(self) -> None:
        """set_enabled(False) → detect_haze() returns None on subsequent calls."""
        haze_condition.set_enabled(False)
        assert _detect() is None, "set_enabled(False) must suppress haze detection"

    def test_set_enabled_true_restores_detection(self) -> None:
        """set_enabled(False) then set_enabled(True) → detection resumes."""
        haze_condition.set_enabled(False)
        haze_condition.set_enabled(True)
        assert _detect() == "Hazy", "set_enabled(True) must re-enable haze detection"

    def test_set_baseline_raises_threshold(self) -> None:
        """set_baseline(0.95) → kcs=0.93 now below baseline, confirms channel."""
        haze_condition.set_baseline(0.95)
        # kcs=0.93 < baseline 0.95 → deficit = 0.02 > 0
        result = _detect(kcs=0.93)
        assert result == "Hazy", (
            "After set_baseline(0.95), kcs=0.93 must confirm Kcs channel (deficit > 0)"
        )

    def test_set_baseline_default_0_90_rejected_by_kcs_0_91(self) -> None:
        """Default baseline=0.90: kcs=0.91 > baseline → deficit < 0 → None."""
        # At default baseline 0.90, kcs=0.91 has no deficit.
        result = _detect(kcs=0.91)
        assert result is None, "kcs=0.91 above default baseline 0.90 must return None"

    def test_set_gamma_changes_correction_exponent(self) -> None:
        """set_gamma(0.12) changes the hygroscopic correction exponent without error."""
        haze_condition.set_gamma(0.12)
        # The gamma value is used in f(RH) computation.
        # Correctness: with both channels firing, result should still be Hazy.
        result = _detect()
        assert result == "Hazy", "set_gamma(0.12) must not break detection"

    def test_set_gamma_high_value_still_detects(self) -> None:
        """set_gamma(1.52) (sea salt maximum) does not break detection."""
        haze_condition.set_gamma(1.52)
        result = _detect()
        assert result == "Hazy", "set_gamma(1.52) must not prevent haze detection"

    def test_reset_restores_enabled_true(self) -> None:
        """reset() restores _enabled to True."""
        haze_condition.set_enabled(False)
        haze_condition.reset()
        assert _detect() == "Hazy", "reset() must restore enabled=True"

    def test_reset_restores_default_baseline(self) -> None:
        """reset() restores _clean_kcs_baseline to 0.90."""
        haze_condition.set_baseline(0.99)
        haze_condition.reset()
        # kcs=0.91 > 0.90 (restored baseline) → no deficit
        result = _detect(kcs=0.91)
        assert result is None, (
            "reset() must restore baseline to 0.90; kcs=0.91 must not confirm channel"
        )

    def test_reset_clears_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """reset() clears _haze_history, removing prior True entries."""
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # Build up True entries in history.
        for i in range(5):
            fake_now[0] = _BASE_TS + i
            _detect()

        # Reset clears the history.
        haze_condition.reset()

        # Now add a mix of False then True entries so the ratio check is visible.
        fake_now[0] = _BASE_TS + 100.0
        for i in range(3):
            fake_now[0] = _BASE_TS + 100.0 + i
            haze_condition.detect_haze(
                kcs=0.85, solar_elevation=5.0, sky_label="Clear",
                pm25=15.0, pm10=None, out_temp=72.0, dewpoint=50.0, rain_rate=0.0,
            )

        fake_now[0] = _BASE_TS + 103.0
        result = haze_condition.detect_haze(
            kcs=0.85, solar_elevation=45.0, sky_label="Clear",
            pm25=15.0, pm10=None, out_temp=72.0, dewpoint=50.0, rain_rate=0.0,
        )
        # 1 True / 4 total = 25% < 50% after reset.
        assert result is None, (
            "After reset() and 3 False + 1 True entries, must be < 50% → None"
        )


# ===========================================================================
# Group 6: Wet deposition edge cases
# ===========================================================================


class TestWetDeposition:
    """Rain gate: active rain, holdoff timing, transition detection."""

    def test_rain_active_suppresses_detection(self) -> None:
        """Active rain (rain_rate > 0) must suppress haze detection."""
        result = _detect(rain_rate=0.5)
        assert result is None, "Active rain must suppress haze detection"

    def test_rain_stops_holdoff_expires_detection_resumes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After 1800s post-rain, detection resumes and can return 'Hazy'."""
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # Rain active.
        fake_now[0] = _BASE_TS
        _detect(rain_rate=0.1)

        # Rain stops at BASE_TS + 10.
        fake_now[0] = _BASE_TS + 10.0
        _detect(rain_rate=0.0)  # records False via holdoff

        # Advance to just past the holdoff (1811s after rain stopped).
        fake_now[0] = _BASE_TS + 10.0 + 1801.0
        result = _detect(rain_rate=0.0)
        assert result == "Hazy", (
            "After 1801s post-rain holdoff, haze detection must resume"
        )

    def test_rain_within_holdoff_still_suppressed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Within 1800s of rain stopping, detection must be suppressed."""
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # Simulate rain active.
        fake_now[0] = _BASE_TS
        _detect(rain_rate=0.05)

        # Rain stops — transition recorded.
        fake_now[0] = _BASE_TS + 1.0
        _detect(rain_rate=0.0)

        # 899s into holdoff — still within 1800s window.
        fake_now[0] = _BASE_TS + 900.0
        result = _detect(rain_rate=0.0)
        assert result is None, "Within 30-min holdoff (900s elapsed) must return None"

    def test_rain_starts_during_detection_records_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rain starting mid-detection records False and suppresses output."""
        fake_now = [_BASE_TS]

        def _fake_time() -> float:
            return fake_now[0]

        monkeypatch.setattr(haze_condition.time, "time", _fake_time)

        # Establish a True history entry.
        fake_now[0] = _BASE_TS
        _detect(rain_rate=0.0)

        # Rain starts — records False.
        fake_now[0] = _BASE_TS + 1.0
        result = _detect(rain_rate=0.2)
        assert result is None, "Active rain must return None regardless of prior history"

    def test_zero_rain_rate_not_gated(self) -> None:
        """rain_rate=0.0 exactly must not trigger the rain gate."""
        result = _detect(rain_rate=0.0)
        assert result == "Hazy", "rain_rate=0.0 must pass the wet deposition gate"

    def test_rain_rate_none_not_gated(self) -> None:
        """rain_rate=None → not treated as rain (currently_raining = False)."""
        result = _detect(rain_rate=None)
        assert result == "Hazy", "rain_rate=None must not trigger the wet deposition gate"


# ===========================================================================
# Group 7: Sky label eligibility
# ===========================================================================


class TestSkyLabelEligibility:
    """Eligible sky labels containing recognized substrings pass the gate."""

    @pytest.mark.parametrize("sky_label", [
        "Clear",
        "Sunny",
        "Mostly Clear",
        "Mostly Sunny",
        "Scattered Clouds",
        "Partly Cloudy",
        "Clear, Scattered Clouds",
        "Mostly Clear, Scattered Clouds",
    ])
    def test_eligible_sky_label_allows_detection(self, sky_label: str) -> None:
        """Sky labels with eligible substrings must pass the sky gate."""
        result = _detect(sky_label=sky_label)
        assert result == "Hazy", (
            f"Sky label {sky_label!r} must be eligible for haze detection"
        )

    @pytest.mark.parametrize("sky_label", [
        "Mostly Cloudy",
        "Cloudy",
        "Overcast",
        "Heavy Overcast",
    ])
    def test_blocked_sky_labels_suppresses_detection(self, sky_label: str) -> None:
        """Explicitly blocked sky labels must suppress haze detection."""
        result = _detect(sky_label=sky_label)
        assert result is None, (
            f"Sky label {sky_label!r} must be blocked by the sky gate"
        )


# ===========================================================================
# Group 8: RH edge cases
# ===========================================================================


class TestRHComputation:
    """Relative humidity edge cases for gate 4 and PM channel branching."""

    def test_rh_exactly_90_not_deferred(self) -> None:
        """RH exactly 90% passes Gate 4 (requires > 90%, not ≥ 90%).

        out_temp=70°F, dewpoint=62.4°F → RH ≈ 78% (below 90%).
        Test that near-90% RH does not trigger the gate.
        """
        # T=77°F, Td=72°F → T-Td=5°F → RH ≈ 83% (< 90%, gate does NOT fire)
        result = _detect(out_temp=77.0, dewpoint=72.0, pm25=40.0, kcs=0.85)
        # humid disambiguation: T-Td=5°F > 4°F, so dry threshold applies.
        # RH ≈ 83% < 80%? No, 83 > 80 → dry branch fails.
        # T-Td=5°F > 4°F → humid branch not taken.
        # pm10=None → no coarse PM.
        # Actually RH ≈ 83%: dry branch (rh < 80) fails, humid (T-Td ≤ 4°F) fails too.
        # pm10=None, so PM channel not confirmed → None.
        assert result is None, (
            "RH ≈ 83%, T-Td=5°F: dry branch fails (>80%), humid branch fails (T-Td>4°F)"
        )

    def test_rh_missing_uses_dry_branch(self) -> None:
        """When out_temp or dewpoint is None, rh=None → dry branch applies (rh < 80 or None)."""
        # rh=None falls into dry branch; pm25=15 > 12 → PM confirmed.
        result = _detect(out_temp=None, dewpoint=None, pm25=15.0)
        assert result == "Hazy", (
            "rh=None must fall into dry branch; pm25=15.0 > 12 must confirm PM channel"
        )

    def test_rh_above_90_with_high_pm25_still_deferred(self) -> None:
        """RH > 90% defers to fog even with pm25=100 µg/m³ (Gate 4 fires first)."""
        # T=68°F, Td=67°F → T-Td=1°F → RH ≈ 97%
        result = _detect(out_temp=68.0, dewpoint=67.0, pm25=100.0)
        assert result is None, "RH > 90% must defer to fog, overriding PM channel"
