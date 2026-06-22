"""Unit tests for weewx_clearskies_api.sse.fog_condition (ADR-069).

Validates the multi-parameter fog/mist detection algorithm:
  - Gate 1  — Rain gate (suppress during active precipitation)
  - Gate 2  — T-Td gate (ASOS standard: suppress when T-Td > 4°F)
  - Gate 3  — Fog/mist split (T-Td ≤ 2°F → "Foggy"; 2–4°F → "Misty")
  - Gate 4  — Wind gate (> 7 m/s suppresses both; 3–7 m/s downgrades fog to mist)
  - Gate 5  — PM2.5 disambiguation (PM2.5 > 35 µg/m³ → "Hazy")
  - Gate 6  — Daytime solar suppression (Kcs > 0.3 suppresses mist; fog exempt)
  - Gate 7  — Temporal coherence (15-minute rolling window; ≥ 50% non-None majority)

Module-level state is intentional in fog_condition.py; the autouse fixture calls
reset() before every test for clean isolation.

Temperature units: °F.  Wind speed: mph (converted to m/s internally).  PM2.5: µg/m³.

Implementation note: Gates 1 and 2 both call _record_history(now, label=None) before
returning None, so those None entries DO count against the temporal coherence window.
In single-call tests the window has 1 entry total, so pass-through gates produce a
1/1 = 100% non-None ratio (majority = candidate label), while suppression gates
produce 0/1 = 0% (returns None from _evaluate_coherence).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse import fog_condition


# ---------------------------------------------------------------------------
# Autouse reset fixture — every test starts from a clean slate.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fog_condition():
    """Reset fog_condition module state before and after each test."""
    fog_condition.reset()
    yield
    fog_condition.reset()


# ---------------------------------------------------------------------------
# Helper — build a history window via monkeypatched time
# ---------------------------------------------------------------------------

_BASE_TS = 1_000_000.0  # Arbitrary stable Unix timestamp for deterministic tests


def _call_detect(
    out_temp: float | None = 65.0,
    dewpoint: float | None = 64.0,
    wind_speed: float | None = 0.0,
    rain_rate: float | None = 0.0,
    kcs: float | None = None,
    is_daytime: bool = False,
    pm25: float | None = None,
) -> str | None:
    """Call detect_fog_mist with default foggy-conditions arguments.

    Defaults: T-Td = 1°F (foggy candidate), calm wind, no rain, no PM,
    nighttime (no solar suppression).  Override individual parameters as needed.
    """
    return fog_condition.detect_fog_mist(
        out_temp=out_temp,
        dewpoint=dewpoint,
        wind_speed=wind_speed,
        rain_rate=rain_rate,
        kcs=kcs,
        is_daytime=is_daytime,
        pm25=pm25,
    )


def _fill_history_with(
    monkeypatch,
    label: str | None,
    count: int,
    base_ts: float = _BASE_TS,
) -> None:
    """Directly populate _fog_history with `count` entries of the given label.

    Uses monkeypatched time.time() so entries have controlled timestamps.
    Each entry is spaced 60 seconds apart within the 900-second window.
    """
    for i in range(count):
        ts = base_ts + i * 60.0
        monkeypatch.setattr("weewx_clearskies_api.sse.fog_condition.time.time", lambda t=ts: t)
        fog_condition._record_history(ts, label=label)


# ---------------------------------------------------------------------------
# Group 1: Gate 1 — Rain gate
# ---------------------------------------------------------------------------


def test_rain_gate_suppresses_when_rain_rate_positive():
    """detect_fog_mist with rain_rate=0.5 in/hr returns None (rain gate fires)."""
    result = _call_detect(rain_rate=0.5)
    assert result is None, f"Expected None with rain_rate=0.5, got {result!r}"


def test_rain_gate_does_not_suppress_when_rain_rate_zero():
    """detect_fog_mist with rain_rate=0.0 proceeds past gate 1 (0.0 is not raining).

    T-Td=1°F, calm, nighttime → "Foggy" expected after passing all gates.
    """
    result = _call_detect(rain_rate=0.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' when rain_rate=0.0 (not raining), got {result!r}"
    )


def test_rain_gate_does_not_suppress_when_rain_rate_none():
    """detect_fog_mist with rain_rate=None proceeds past gate 1.

    None rain_rate means sensor is absent; the rain gate condition
    `rain_rate is not None and rain_rate > 0.0` evaluates False.
    T-Td=1°F → "Foggy" expected.
    """
    result = _call_detect(rain_rate=None)
    assert result == "Foggy", (
        f"Expected 'Foggy' when rain_rate=None (sensor absent), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 2: Gate 2 — T-Td gate (ASOS standard)
# ---------------------------------------------------------------------------


def test_ttd_gate_suppresses_when_out_temp_none():
    """detect_fog_mist returns None when out_temp is None (missing sensor)."""
    result = _call_detect(out_temp=None, dewpoint=65.0)
    assert result is None, f"Expected None when out_temp=None, got {result!r}"


def test_ttd_gate_suppresses_when_dewpoint_none():
    """detect_fog_mist returns None when dewpoint is None (missing sensor)."""
    result = _call_detect(out_temp=70.0, dewpoint=None)
    assert result is None, f"Expected None when dewpoint=None, got {result!r}"


def test_ttd_gate_suppresses_when_ttd_greater_than_4():
    """T-Td = 70 - 65 = 5°F > 4°F → None (ASOS standard suppression)."""
    result = _call_detect(out_temp=70.0, dewpoint=65.0)
    assert result is None, (
        f"Expected None when T-Td=5°F (> 4°F threshold), got {result!r}"
    )


def test_ttd_gate_passes_when_ttd_exactly_4():
    """T-Td = 70 - 66 = 4°F is not > 4.0, so gate 2 passes.

    T-Td=4°F falls in the "Misty" range (2 < T-Td ≤ 4).
    Calm wind, nighttime, no PM → "Misty" expected.
    """
    result = _call_detect(out_temp=70.0, dewpoint=66.0)
    assert result == "Misty", (
        f"Expected 'Misty' when T-Td=4°F (exactly at boundary, not > 4), got {result!r}"
    )


def test_ttd_gate_passes_when_ttd_3point9():
    """T-Td = 3.9°F (< 4°F) passes gate 2 into mist range."""
    result = _call_detect(out_temp=70.0, dewpoint=66.1)
    assert result == "Misty", (
        f"Expected 'Misty' when T-Td≈3.9°F, got {result!r}"
    )


def test_ttd_gate_passes_when_temps_equal():
    """T-Td = 0°F (saturated air) passes gate 2 into foggy range."""
    result = _call_detect(out_temp=65.0, dewpoint=65.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' when T-Td=0°F (fully saturated), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 3: Gate 3 — Fog/mist split
# ---------------------------------------------------------------------------


def test_gate3_foggy_candidate_when_ttd_1():
    """T-Td = 1°F (≤ 2) produces 'Foggy' candidate → 'Foggy' after coherence."""
    result = _call_detect(out_temp=66.0, dewpoint=65.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' for T-Td=1°F (≤ 2°F boundary), got {result!r}"
    )


def test_gate3_foggy_candidate_when_ttd_exactly_2():
    """T-Td = 2.0°F (exactly ≤ 2) → 'Foggy' candidate."""
    result = _call_detect(out_temp=67.0, dewpoint=65.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' for T-Td=2.0°F (exactly at boundary), got {result!r}"
    )


def test_gate3_misty_candidate_when_ttd_2point1():
    """T-Td = 2.1°F (> 2) → 'Misty' candidate."""
    result = _call_detect(out_temp=67.1, dewpoint=65.0)
    assert result == "Misty", (
        f"Expected 'Misty' for T-Td=2.1°F (just above foggy boundary), got {result!r}"
    )


def test_gate3_misty_candidate_when_ttd_exactly_4():
    """T-Td = 4.0°F (≤ 4, > 2) → 'Misty' candidate."""
    result = _call_detect(out_temp=69.0, dewpoint=65.0)
    assert result == "Misty", (
        f"Expected 'Misty' for T-Td=4.0°F (upper boundary of mist range), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 4: Gate 4 — Wind gate
# ---------------------------------------------------------------------------


def test_wind_gate_suppresses_both_when_wind_exceeds_7ms():
    """wind_speed=16 mph → wind_ms ≈ 7.153 > 7.0 m/s → None (both suppressed).

    16 mph * 0.44704 = 7.1527 m/s > 7.0 m/s threshold.
    """
    result = _call_detect(out_temp=66.0, dewpoint=65.0, wind_speed=16.0)
    assert result is None, (
        f"Expected None when wind=16 mph (≈7.15 m/s > 7.0 m/s threshold), got {result!r}"
    )


def test_wind_gate_downgrades_fog_to_mist_between_3_and_7ms():
    """wind_speed=15 mph → wind_ms ≈ 6.706 > 3.0 m/s but ≤ 7.0 m/s → fog downgraded to mist.

    15 mph * 0.44704 = 6.7056 m/s is in the 3–7 m/s band.
    T-Td=1°F would be 'Foggy' but wind downgrades it to 'Misty'.
    """
    result = _call_detect(out_temp=66.0, dewpoint=65.0, wind_speed=15.0)
    assert result == "Misty", (
        f"Expected 'Misty' after fog downgraded by wind=15 mph (≈6.71 m/s), got {result!r}"
    )


def test_wind_gate_does_not_downgrade_mist_between_3_and_7ms():
    """wind_speed=15 mph in 3–7 m/s band does not further downgrade a 'Misty' candidate.

    Only 'Foggy' is downgraded to 'Misty' in the wind band. 'Misty' stays 'Misty'.
    T-Td=3°F → 'Misty' candidate; wind=15 mph → should remain 'Misty'.
    """
    result = _call_detect(out_temp=68.0, dewpoint=65.0, wind_speed=15.0)
    assert result == "Misty", (
        f"Expected 'Misty' (unchanged mist candidate) with wind=15 mph, got {result!r}"
    )


def test_wind_gate_no_change_when_wind_below_3ms():
    """wind_speed=6 mph → wind_ms ≈ 2.682 ≤ 3.0 m/s → no change to fog candidate."""
    result = _call_detect(out_temp=66.0, dewpoint=65.0, wind_speed=6.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' unchanged for wind=6 mph (≈2.68 m/s ≤ 3 m/s), got {result!r}"
    )


def test_wind_gate_skipped_when_wind_speed_none():
    """wind_speed=None skips the wind gate entirely; fog candidate unchanged."""
    result = _call_detect(out_temp=66.0, dewpoint=65.0, wind_speed=None)
    assert result == "Foggy", (
        f"Expected 'Foggy' when wind_speed=None (gate skipped), got {result!r}"
    )


def test_wind_gate_no_change_when_wind_zero():
    """wind_speed=0 mph → wind_ms=0 ≤ 3.0 m/s → no suppression or downgrade."""
    result = _call_detect(out_temp=66.0, dewpoint=65.0, wind_speed=0.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' when wind_speed=0 mph, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 5: Gate 5 — PM2.5 disambiguation
# ---------------------------------------------------------------------------


def test_pm25_gate_overrides_foggy_with_hazy_when_pm25_above_35():
    """pm25=40.0 > 35 with T-Td=2°F (foggy candidate) → 'Hazy' override."""
    result = _call_detect(out_temp=67.0, dewpoint=65.0, pm25=40.0)
    assert result == "Hazy", (
        f"Expected 'Hazy' when pm25=40.0 overrides foggy candidate, got {result!r}"
    )


def test_pm25_gate_overrides_misty_with_hazy_when_pm25_above_35():
    """pm25=40.0 > 35 with T-Td=3°F (misty candidate) → 'Hazy' override."""
    result = _call_detect(out_temp=68.0, dewpoint=65.0, pm25=40.0)
    assert result == "Hazy", (
        f"Expected 'Hazy' when pm25=40.0 overrides misty candidate, got {result!r}"
    )


def test_pm25_gate_no_change_when_pm25_exactly_35():
    """pm25=35.0 is not > 35, so gate 5 does not override the fog candidate."""
    result = _call_detect(out_temp=66.0, dewpoint=65.0, pm25=35.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' (not Hazy) when pm25=35.0 (≤ 35 threshold), got {result!r}"
    )


def test_pm25_gate_no_change_when_pm25_none():
    """pm25=None skips gate 5; fog candidate unchanged."""
    result = _call_detect(out_temp=66.0, dewpoint=65.0, pm25=None)
    assert result == "Foggy", (
        f"Expected 'Foggy' when pm25=None (gate skipped), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 6: Gate 6 — Daytime solar suppression
# ---------------------------------------------------------------------------


def test_solar_gate_suppresses_mist_when_daytime_and_kcs_above_0point3():
    """is_daytime=True, kcs=0.5 > 0.3 with 'Misty' candidate → None (suppressed)."""
    result = _call_detect(
        out_temp=68.0, dewpoint=65.0,  # T-Td=3°F → Misty
        kcs=0.5, is_daytime=True,
    )
    assert result is None, (
        f"Expected None: daytime Kcs=0.5 must suppress mist candidate, got {result!r}"
    )


def test_solar_gate_does_not_suppress_mist_when_kcs_at_or_below_0point3():
    """is_daytime=True, kcs=0.2 ≤ 0.3 with 'Misty' candidate → NOT suppressed."""
    result = _call_detect(
        out_temp=68.0, dewpoint=65.0,  # T-Td=3°F → Misty
        kcs=0.2, is_daytime=True,
    )
    assert result == "Misty", (
        f"Expected 'Misty' when daytime Kcs=0.2 (≤ 0.3, below suppression threshold), "
        f"got {result!r}"
    )


def test_solar_gate_does_not_suppress_foggy_when_daytime_and_high_kcs():
    """is_daytime=True, kcs=0.5 with 'Foggy' candidate → NOT suppressed.

    Dense fog (T-Td ≤ 2°F) persists at sunrise and is not suppressed by solar.
    """
    result = _call_detect(
        out_temp=66.0, dewpoint=65.0,  # T-Td=1°F → Foggy
        kcs=0.5, is_daytime=True,
    )
    assert result == "Foggy", (
        f"Expected 'Foggy' (dense fog not suppressed by solar), got {result!r}"
    )


def test_solar_gate_does_not_suppress_hazy_when_daytime_and_high_kcs():
    """is_daytime=True, kcs=0.5 with 'Hazy' candidate → NOT suppressed.

    PM haze is not suppressed by solar radiation (Gate 6 only targets Misty).
    """
    result = _call_detect(
        out_temp=68.0, dewpoint=65.0,  # T-Td=3°F, PM2.5 override → Hazy
        kcs=0.5, is_daytime=True, pm25=40.0,
    )
    assert result == "Hazy", (
        f"Expected 'Hazy' (PM haze not suppressed by solar), got {result!r}"
    )


def test_solar_gate_skipped_when_nighttime():
    """is_daytime=False with kcs=0.5 and 'Misty' candidate → NOT suppressed (nighttime).

    Gate 6 only applies when is_daytime=True.
    """
    result = _call_detect(
        out_temp=68.0, dewpoint=65.0,  # T-Td=3°F → Misty
        kcs=0.5, is_daytime=False,
    )
    assert result == "Misty", (
        f"Expected 'Misty' when is_daytime=False (gate 6 skipped), got {result!r}"
    )


def test_solar_gate_skipped_when_kcs_none():
    """is_daytime=True with kcs=None and 'Misty' candidate → NOT suppressed.

    Gate 6 requires kcs to be known (not None) to suppress.
    """
    result = _call_detect(
        out_temp=68.0, dewpoint=65.0,  # T-Td=3°F → Misty
        kcs=None, is_daytime=True,
    )
    assert result == "Misty", (
        f"Expected 'Misty' when kcs=None (gate 6 skipped), got {result!r}"
    )


def test_solar_gate_suppresses_mist_at_kcs_boundary_just_above_0point3():
    """kcs=0.31 is just above the 0.3 threshold → mist suppressed."""
    result = _call_detect(
        out_temp=68.0, dewpoint=65.0,  # T-Td=3°F → Misty
        kcs=0.31, is_daytime=True,
    )
    assert result is None, (
        f"Expected None: Kcs=0.31 (just above 0.3) must suppress mist, got {result!r}"
    )


def test_solar_gate_does_not_suppress_mist_at_kcs_exactly_0point3():
    """kcs=0.3 exactly is not > 0.3; mist candidate survives gate 6."""
    result = _call_detect(
        out_temp=68.0, dewpoint=65.0,  # T-Td=3°F → Misty
        kcs=0.3, is_daytime=True,
    )
    assert result == "Misty", (
        f"Expected 'Misty' when kcs=0.3 (exactly at boundary, not > 0.3), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 7: Gate 7 — Temporal coherence (15-minute rolling window)
# ---------------------------------------------------------------------------


def test_coherence_single_foggy_detection_returns_foggy(monkeypatch):
    """Single 'Foggy' detection in empty history: 1/1 = 100% → 'Foggy' returned."""
    monkeypatch.setattr(
        "weewx_clearskies_api.sse.fog_condition.time.time",
        lambda: _BASE_TS,
    )
    result = _call_detect(out_temp=66.0, dewpoint=65.0)
    assert result == "Foggy", (
        f"Expected 'Foggy' from single detection (1/1 = 100% non-None), got {result!r}"
    )


def test_coherence_majority_foggy_returns_foggy(monkeypatch):
    """History filled with majority 'Foggy' entries → returns 'Foggy'.

    7 'Foggy' + 3 None = 70% non-None → satisfies ≥ 50% threshold.
    """
    fog_condition.reset()
    _fill_history_with(monkeypatch, "Foggy", count=7, base_ts=_BASE_TS)
    _fill_history_with(monkeypatch, None, count=3, base_ts=_BASE_TS + 7 * 60)

    # Evaluate at a timestamp within the window.
    current_ts = _BASE_TS + 10 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result == "Foggy", (
        f"Expected 'Foggy' from 70% Foggy majority, got {result!r}"
    )


def test_coherence_majority_none_returns_none(monkeypatch):
    """History filled with majority None entries (< 50% non-None) → returns None.

    3 'Foggy' + 7 None = 30% non-None → fails ≥ 50% threshold.
    """
    fog_condition.reset()
    _fill_history_with(monkeypatch, "Foggy", count=3, base_ts=_BASE_TS)
    _fill_history_with(monkeypatch, None, count=7, base_ts=_BASE_TS + 3 * 60)

    current_ts = _BASE_TS + 10 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result is None, (
        f"Expected None from 30% non-None (fails ≥ 50% threshold), got {result!r}"
    )


def test_coherence_60_percent_foggy_returns_foggy(monkeypatch):
    """60% Foggy, 40% None → ≥ 50% threshold met → 'Foggy' returned."""
    fog_condition.reset()
    _fill_history_with(monkeypatch, "Foggy", count=6, base_ts=_BASE_TS)
    _fill_history_with(monkeypatch, None, count=4, base_ts=_BASE_TS + 6 * 60)

    current_ts = _BASE_TS + 10 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result == "Foggy", (
        f"Expected 'Foggy' from 60% Foggy (≥ 50%), got {result!r}"
    )


def test_coherence_40_percent_foggy_returns_none(monkeypatch):
    """40% Foggy, 60% None → fails ≥ 50% threshold → None returned."""
    fog_condition.reset()
    _fill_history_with(monkeypatch, "Foggy", count=4, base_ts=_BASE_TS)
    _fill_history_with(monkeypatch, None, count=6, base_ts=_BASE_TS + 4 * 60)

    current_ts = _BASE_TS + 10 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result is None, (
        f"Expected None from 40% Foggy (< 50%), got {result!r}"
    )


def test_coherence_pruning_removes_entries_older_than_900s(monkeypatch):
    """Entries older than 900 seconds are pruned on each detect call.

    Record 5 'Foggy' entries at timestamps older than 900s from 'now'.
    After calling detect_fog_mist with current timestamp, those old entries
    should have been pruned.  old_base must be far enough back that ALL 5
    entries (spaced 60s apart: +0, +60, +120, +180, +240) fall before the
    cutoff at _BASE_TS - 900 = 999100.  With old_base = 998000, entries
    are at 998000..998240, all well below 999100.
    """
    fog_condition.reset()
    old_base = _BASE_TS - 2000.0  # 2000s before base — all entries outside the 900s window
    _fill_history_with(monkeypatch, "Foggy", count=5, base_ts=old_base)

    # Now call detect at _BASE_TS — old entries are outside the 900s window.
    monkeypatch.setattr(
        "weewx_clearskies_api.sse.fog_condition.time.time",
        lambda: _BASE_TS,
    )
    result = _call_detect(out_temp=66.0, dewpoint=65.0)

    # The window has only 1 entry (the current one); old entries pruned.
    # 1/1 = 100% non-None → returns "Foggy".
    assert result == "Foggy", (
        f"Expected 'Foggy' from single current entry after pruning old entries, "
        f"got {result!r}"
    )
    # Confirm deque was pruned to at most 1 entry.
    assert len(fog_condition._fog_history) == 1, (
        f"Expected 1 entry after pruning stale entries, "
        f"got {len(fog_condition._fog_history)}"
    )


def test_coherence_pruning_boundary_entry_at_exactly_900s_is_kept(monkeypatch):
    """An entry exactly 900s old is NOT pruned (cutoff is strictly <, not ≤)."""
    fog_condition.reset()
    entry_ts = _BASE_TS - 900.0  # Exactly 900s before current
    # Manually append a single entry at exactly the cutoff boundary.
    fog_condition._fog_history.append((entry_ts, "Foggy"))

    # Call _record_history at _BASE_TS; cutoff = _BASE_TS - 900.0.
    # Prune condition: _fog_history[0][0] < cutoff → entry_ts < entry_ts → False.
    fog_condition._record_history(_BASE_TS, label="Foggy")

    # Both entries should remain (the boundary entry is NOT pruned).
    assert len(fog_condition._fog_history) == 2, (
        f"Expected 2 entries (boundary entry not pruned), "
        f"got {len(fog_condition._fog_history)}"
    )


def test_coherence_tiebreak_foggy_wins_over_misty(monkeypatch):
    """Tie between equal 'Foggy' and 'Misty' counts → 'Foggy' wins (priority 3 > 2)."""
    fog_condition.reset()
    _fill_history_with(monkeypatch, "Foggy", count=3, base_ts=_BASE_TS)
    _fill_history_with(monkeypatch, "Misty", count=3, base_ts=_BASE_TS + 3 * 60)

    current_ts = _BASE_TS + 6 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result == "Foggy", (
        f"Expected 'Foggy' to win tie-break over 'Misty' (priority 3 > 2), "
        f"got {result!r}"
    )


def test_coherence_tiebreak_misty_wins_over_hazy(monkeypatch):
    """Tie between equal 'Misty' and 'Hazy' counts → 'Misty' wins (priority 2 > 1)."""
    fog_condition.reset()
    _fill_history_with(monkeypatch, "Misty", count=3, base_ts=_BASE_TS)
    _fill_history_with(monkeypatch, "Hazy", count=3, base_ts=_BASE_TS + 3 * 60)

    current_ts = _BASE_TS + 6 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result == "Misty", (
        f"Expected 'Misty' to win tie-break over 'Hazy' (priority 2 > 1), "
        f"got {result!r}"
    )


def test_coherence_empty_history_returns_none():
    """_evaluate_coherence on an empty history deque returns None."""
    fog_condition.reset()
    result = fog_condition._evaluate_coherence(_BASE_TS)
    assert result is None, (
        f"Expected None from _evaluate_coherence with empty history, got {result!r}"
    )


def test_coherence_all_none_returns_none(monkeypatch):
    """History with all None labels (no non-None entries) → _evaluate_coherence returns None."""
    fog_condition.reset()
    _fill_history_with(monkeypatch, None, count=5, base_ts=_BASE_TS)

    current_ts = _BASE_TS + 5 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result is None, (
        f"Expected None when all history entries are None, got {result!r}"
    )


def test_coherence_exactly_50_percent_non_none_satisfies_threshold(monkeypatch):
    """Exactly 50% non-None entries satisfies the ≥ 50% threshold (not strictly >).

    5 'Foggy' + 5 None = 50% non-None → should return 'Foggy'.
    """
    fog_condition.reset()
    _fill_history_with(monkeypatch, "Foggy", count=5, base_ts=_BASE_TS)
    _fill_history_with(monkeypatch, None, count=5, base_ts=_BASE_TS + 5 * 60)

    current_ts = _BASE_TS + 10 * 60
    result = fog_condition._evaluate_coherence(current_ts)
    assert result == "Foggy", (
        f"Expected 'Foggy' at exactly 50% non-None threshold, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 8: Combined scenario tests
# ---------------------------------------------------------------------------


def test_combined_dense_fog_at_night():
    """Dense fog scenario: T-Td=1°F, calm wind, no rain, no PM, nighttime → 'Foggy'."""
    result = fog_condition.detect_fog_mist(
        out_temp=66.0,
        dewpoint=65.0,
        wind_speed=0.0,
        rain_rate=0.0,
        kcs=None,
        is_daytime=False,
        pm25=None,
    )
    assert result == "Foggy", (
        f"Expected 'Foggy' for dense fog at night (all gates pass), got {result!r}"
    )


def test_combined_mist_suppressed_by_daytime_sun():
    """Light mist + daytime sunshine: T-Td=3°F, calm, kcs=0.5, daytime → None.

    Solar gate (Gate 6) suppresses the Misty candidate when kcs > 0.3.
    """
    result = fog_condition.detect_fog_mist(
        out_temp=68.0,
        dewpoint=65.0,
        wind_speed=0.0,
        rain_rate=0.0,
        kcs=0.5,
        is_daytime=True,
        pm25=None,
    )
    assert result is None, (
        f"Expected None: mist suppressed by daytime kcs=0.5, got {result!r}"
    )


def test_combined_hazy_near_saturation():
    """Near-saturation with elevated PM2.5: T-Td=3°F, pm25=50 → 'Hazy'."""
    result = fog_condition.detect_fog_mist(
        out_temp=68.0,
        dewpoint=65.0,
        wind_speed=0.0,
        rain_rate=0.0,
        kcs=None,
        is_daytime=False,
        pm25=50.0,
    )
    assert result == "Hazy", (
        f"Expected 'Hazy' for near-saturated air with pm25=50, got {result!r}"
    )


def test_combined_fog_downgraded_by_wind():
    """Fog downgraded by moderate wind: T-Td=1°F, wind=10 mph → 'Misty'.

    10 mph * 0.44704 = 4.47 m/s → in the 3–7 m/s band → fog downgraded.
    """
    result = fog_condition.detect_fog_mist(
        out_temp=66.0,
        dewpoint=65.0,
        wind_speed=10.0,
        rain_rate=0.0,
        kcs=None,
        is_daytime=False,
        pm25=None,
    )
    assert result == "Misty", (
        f"Expected 'Misty' after fog downgraded by wind=10 mph (≈4.47 m/s), got {result!r}"
    )


def test_combined_all_gates_pass_for_fog():
    """All gates pass: T-Td=1°F, calm, no rain, no PM, nighttime → 'Foggy'."""
    result = fog_condition.detect_fog_mist(
        out_temp=66.0,
        dewpoint=65.0,
        wind_speed=2.0,   # 2 mph ≈ 0.89 m/s ≤ 3 m/s → no change
        rain_rate=0.0,
        kcs=None,
        is_daytime=False,
        pm25=None,
    )
    assert result == "Foggy", (
        f"Expected 'Foggy' when all gates pass with standard fog conditions, got {result!r}"
    )


def test_combined_fog_persists_in_daytime_with_no_solar():
    """Dense fog (T-Td=1°F) at sunrise without clear sky (kcs=None) → 'Foggy'.

    Gate 6 only fires when kcs is known. Dense fog candidate is immune to
    Gate 6 anyway, but this confirms the kcs=None path.
    """
    result = fog_condition.detect_fog_mist(
        out_temp=66.0,
        dewpoint=65.0,
        wind_speed=0.0,
        rain_rate=0.0,
        kcs=None,
        is_daytime=True,
        pm25=None,
    )
    assert result == "Foggy", (
        f"Expected 'Foggy' for dense fog at sunrise with kcs=None, got {result!r}"
    )


def test_combined_rain_suppresses_all_other_conditions():
    """Rain gate fires before T-Td check: rain_rate=1.0 with T-Td=1°F → None."""
    result = fog_condition.detect_fog_mist(
        out_temp=66.0,
        dewpoint=65.0,
        wind_speed=0.0,
        rain_rate=1.0,
        kcs=None,
        is_daytime=False,
        pm25=None,
    )
    assert result is None, (
        f"Expected None: rain gate fires first even with T-Td=1°F, got {result!r}"
    )


def test_combined_wind_suppresses_both_when_above_7ms():
    """High wind suppresses both fog and mist: wind=20 mph → None.

    20 mph * 0.44704 = 8.94 m/s > 7.0 m/s → full suppression.
    """
    result = fog_condition.detect_fog_mist(
        out_temp=66.0,
        dewpoint=65.0,
        wind_speed=20.0,
        rain_rate=0.0,
        kcs=None,
        is_daytime=False,
        pm25=None,
    )
    assert result is None, (
        f"Expected None: wind=20 mph (≈8.94 m/s > 7 m/s) suppresses both fog and mist, "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 9: reset() tests
# ---------------------------------------------------------------------------


def test_reset_clears_fog_history(monkeypatch):
    """reset() clears _fog_history so that subsequent calls start from empty state."""
    # Populate history with several entries.
    _fill_history_with(monkeypatch, "Foggy", count=5, base_ts=_BASE_TS)
    assert len(fog_condition._fog_history) == 5, "Pre-condition: history should have 5 entries"

    fog_condition.reset()
    assert len(fog_condition._fog_history) == 0, (
        f"Expected empty deque after reset(), got {len(fog_condition._fog_history)} entries"
    )


def test_reset_allows_fresh_start_after_populated_history(monkeypatch):
    """After reset(), a fresh detect call works correctly on empty history."""
    # Pre-populate history with 'None' entries to simulate a suppressed window.
    _fill_history_with(monkeypatch, None, count=8, base_ts=_BASE_TS)
    # Without reset, all 8 None + 1 Foggy = 1/9 = 11% → returns None.
    monkeypatch.setattr(
        "weewx_clearskies_api.sse.fog_condition.time.time",
        lambda: _BASE_TS + 8 * 60,
    )
    result_before_reset = _call_detect(out_temp=66.0, dewpoint=65.0)
    assert result_before_reset is None, "Pre-condition: crowded-None history should suppress"

    # After reset, history is empty. Single Foggy entry → 1/1 = 100% → 'Foggy'.
    fog_condition.reset()
    monkeypatch.setattr(
        "weewx_clearskies_api.sse.fog_condition.time.time",
        lambda: _BASE_TS + 9 * 60,
    )
    result_after_reset = _call_detect(out_temp=66.0, dewpoint=65.0)
    assert result_after_reset == "Foggy", (
        f"Expected 'Foggy' after reset() clears suppressed history, got {result_after_reset!r}"
    )


# ---------------------------------------------------------------------------
# Group 10: Internal helpers
# ---------------------------------------------------------------------------


def test_tiebreak_priority_returns_3_for_foggy():
    """_tiebreak_priority returns 3 for 'Foggy' (highest priority)."""
    assert fog_condition._tiebreak_priority("Foggy") == 3


def test_tiebreak_priority_returns_2_for_misty():
    """_tiebreak_priority returns 2 for 'Misty'."""
    assert fog_condition._tiebreak_priority("Misty") == 2


def test_tiebreak_priority_returns_1_for_hazy():
    """_tiebreak_priority returns 1 for 'Hazy' (lowest known priority)."""
    assert fog_condition._tiebreak_priority("Hazy") == 1


def test_tiebreak_priority_returns_0_for_unknown():
    """_tiebreak_priority returns 0 for any unrecognised label."""
    assert fog_condition._tiebreak_priority("Unknown") == 0
    assert fog_condition._tiebreak_priority("") == 0


def test_record_history_appends_entry():
    """_record_history appends a (timestamp, label) tuple to _fog_history."""
    fog_condition.reset()
    fog_condition._record_history(_BASE_TS, label="Foggy")
    assert len(fog_condition._fog_history) == 1
    ts, lbl = fog_condition._fog_history[0]
    assert ts == _BASE_TS
    assert lbl == "Foggy"


def test_record_history_prunes_entries_older_than_900s():
    """_record_history prunes entries where timestamp < (now - 900)."""
    fog_condition.reset()
    # Add an entry that's 950 seconds old relative to now.
    fog_condition._fog_history.append((_BASE_TS - 950.0, "Foggy"))
    # Call _record_history at _BASE_TS; cutoff = _BASE_TS - 900.
    # Old entry (BASE_TS - 950) < cutoff (BASE_TS - 900) → pruned.
    fog_condition._record_history(_BASE_TS, label="Misty")
    assert len(fog_condition._fog_history) == 1, (
        f"Expected 1 entry after pruning; old entry should have been removed. "
        f"Got {len(fog_condition._fog_history)}"
    )
    assert fog_condition._fog_history[0][1] == "Misty", (
        "Expected the surviving entry to be the newly added 'Misty'"
    )
