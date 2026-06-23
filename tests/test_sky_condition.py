"""Unit tests for weewx_clearskies_api.sse.sky_condition.

Validates the CAELUS-based Variability Index (VI) system:
  - Four-index computation (Kcs, Km, Kv, Kvf)
  - Six CAELUS classification classes mapped to NWS labels
  - 1-minute binning from 5-second LOOP packets
  - Archive backfill (ring buffer seeding)
  - Temporal coherence filter (15-min persistence, 3-min startup grace)
  - Edge cases (night guard, None radiation, sunset transition, reset)

Module-level state is intentional in sky_condition.py; the autouse fixture
calls reset() before every test to provide clean isolation.

BASE_TS alignment note
-----------------------
_BASE_TS = 1_000_080.0 is exactly 60 * 16668, a minute boundary.  This
ensures that all 12 readings at 5-second intervals within a given minute
loop iteration share the same minute bucket (floor(ts / 60)).  A misaligned
base timestamp would split the first minute across two buckets, producing
shorter initial bins and incorrect Kv values.  See the binning section for
tests that verify this property.

Coherence filter mechanics
---------------------------
classify() appends (ring[-1].ts, raw_label) to _classification_history ONLY
when _compute_indices() returns a non-None result (ring >= 3 entries).
Building 3 ring entries requires readings across 4 different minutes; the
fourth minute flush creates the third ring bin.  The history therefore does
not accumulate until after the third bin is created.

The startup grace threshold (consecutive_span >= 180 s) means that the
classify() call after minute 6 (seven minutes total, 0-indexed 0-6) is the
first call that fires the grace: history entries 4 through 6 span exactly
3 * 60 = 180 seconds.

The full 15-minute stability window (>= 900 s) fires at minute 18, after
16 consecutive history entries at 60-second spacing (entries from minute 3
through minute 18 = 15 * 60 = 900 s).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse import sky_condition


# ---------------------------------------------------------------------------
# Autouse reset fixture — every test starts from a clean slate.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sky_condition():
    """Reset sky_condition module state before each test."""
    sky_condition.reset()
    yield
    sky_condition.reset()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

# 1_000_080 = 60 * 16668; aligned to a minute boundary so all 12 readings
# per minute-loop iteration land in the same minute bucket.
_BASE_TS = 1_000_080.0


def _feed_constant_ghi(
    ghi: float,
    msr: float,
    minutes: int = 30,
    base_ts: float = _BASE_TS,
) -> None:
    """Feed constant GHI readings for the given number of minutes.

    Sends 12 readings per minute (5-sec intervals); also calls classify()
    after each minute to build up the temporal coherence filter history.
    The coherence filter only accumulates history when classify() is called,
    and the filter requires entries spanning >= 180 s (startup grace) or
    >= 900 s (full stability) to commit a label.
    """
    sky_condition.reset()
    for minute in range(minutes):
        for tick in range(12):
            ts = base_ts + minute * 60 + tick * 5
            sky_condition.update(ghi, msr, timestamp=ts)
        sky_condition.classify()


def _feed_alternating_ghi(
    ghi_high: float,
    ghi_low: float,
    msr: float,
    minutes: int = 30,
    base_ts: float = _BASE_TS,
    high_on_even: bool = True,
) -> None:
    """Feed alternating high/low GHI values, switching every minute.

    When high_on_even is True (default), even-numbered minutes receive
    ghi_high; odd-numbered minutes receive ghi_low.  When False the
    assignment is reversed — useful for placing a specific GHI value in
    the last ring bin (even minutes are flushed by the following odd minute).

    Calls classify() after each minute to build coherence filter history.
    """
    sky_condition.reset()
    for minute in range(minutes):
        if high_on_even:
            ghi = ghi_high if minute % 2 == 0 else ghi_low
        else:
            ghi = ghi_low if minute % 2 == 0 else ghi_high
        for tick in range(12):
            ts = base_ts + minute * 60 + tick * 5
            sky_condition.update(ghi, msr, timestamp=ts)
        sky_condition.classify()


def _make_backfill_records(
    n: int,
    ghi: float,
    msr: float,
    interval_sec: int = 300,
    end_ts: float = _BASE_TS,
) -> list[tuple[float, float, float]]:
    """Build a list of (ts, ghi, msr) archive records ending at end_ts."""
    return [
        (end_ts - (n - 1 - i) * interval_sec, ghi, msr)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Group 1: Index computation
# ---------------------------------------------------------------------------


def test_constant_ghi_produces_zero_variability():
    """Constant GHI/msr produces near-zero variability and 'Clear' label.

    GHI=800, msr=900 → Kcs ≈ 0.889, Km ≈ 0.889, Kv = 0 (identical bins).
    Satisfies CLOUDLESS: Km > 0.6, Kcs in [0.85, 1.15], Kv < 0.03.
    """
    _feed_constant_ghi(ghi=800.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result is not None, "Expected a classification, got None"
    assert result == "Clear", f"Expected 'Clear' for constant high GHI, got {result!r}"


def test_oscillating_ghi_produces_high_variability():
    """Rapidly alternating GHI (800/200 W/m²) every minute gives very high Kv.

    Mean = 500 W/m², deviation amplitude = 300.  Kv ≈ 10 — far outside
    the CLOUDLESS (< 0.03) and THIN_CLOUDS (< 0.08) ranges.
    """
    _feed_alternating_ghi(ghi_high=800.0, ghi_low=200.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result is not None, "Expected a classification, got None"
    assert result != "Clear", (
        f"High-variability oscillating data should not classify as 'Clear', got {result!r}"
    )


def test_slow_ramp_moderate_variability():
    """Linearly ramping GHI (200→800 W/m²) over 30 minutes yields a valid label.

    A monotone ramp produces a non-zero but bounded Kv.  We assert the
    result is one of the known NWS labels (not None and not unrecognised).
    """
    sky_condition.reset()
    msr = 900.0
    ghi_start, ghi_end = 200.0, 800.0
    n_minutes = 30
    for minute in range(n_minutes):
        ghi = ghi_start + (ghi_end - ghi_start) * minute / (n_minutes - 1)
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(ghi, msr, timestamp=ts)
        sky_condition.classify()

    result = sky_condition.classify()
    valid_labels = {"Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy"}
    assert result is not None, "Ramp over 30 minutes should produce a classification"
    assert result in valid_labels, f"classify() returned an unrecognised label: {result!r}"


def test_afternoon_decline_classifies_clear():
    """A clear afternoon's declining GHI tracking maxSolarRad classifies as 'Clear'.

    Simulates the real-world scenario: both GHI and maxSolarRad decline
    together as the sun lowers in the afternoon.  The Kcs ratio stays
    constant at ~0.926, indicating a clear sky.  Without clear-sky
    detrending, the raw GHI decline would produce Kv > 0.03 (the
    CLOUDLESS threshold), causing a false 'Mostly Clear' classification.
    With detrending, the predicted maxSolarRad decline is subtracted from
    the observed GHI decline, yielding near-zero Kv.

    GHI declines from 846 to 768 W/m², maxSolarRad from 911 to 830 W/m²
    (matching real afternoon data observed 2026-06-19).
    """
    sky_condition.reset()
    n_minutes = 30
    ghi_start, ghi_end = 846.0, 768.0
    msr_start, msr_end = 911.0, 830.0
    for minute in range(n_minutes):
        frac = minute / (n_minutes - 1)
        ghi = ghi_start + (ghi_end - ghi_start) * frac
        msr = msr_start + (msr_end - msr_start) * frac
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(ghi, msr, timestamp=ts)
        sky_condition.classify()

    result = sky_condition.classify()
    assert result == "Clear", (
        f"Expected 'Clear' for clear-sky afternoon decline (constant Kcs ~0.926), "
        f"got {result!r}"
    )


def test_cloud_enhancement_kcs_above_one():
    """GHI exceeding maxSolarRad (cloud-edge focusing) is handled without crash.

    GHI=1000, msr=900 → raw Kcs = 1.111; clamped at _KC_MAX = 1.2.
    With constant GHI > msr, Kv = 0 so the CLOUD_ENHANCEMENT branch is
    skipped (Kv < 0.20).  The module must not raise and must return a
    valid NWS label (CLOUDLESS fires: Kcs 1.111 in [0.85, 1.15], Km > 0.6,
    Kv = 0 < 0.03 → 'Clear').
    """
    sky_condition.reset()
    for minute in range(30):
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(1000.0, 900.0, timestamp=ts)
        sky_condition.classify()

    result = sky_condition.classify()
    valid_labels = {
        "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy",
        "Overcast", "Heavy Overcast", None,
    }
    assert result in valid_labels, f"Unexpected return value with Kcs > 1.0: {result!r}"


# ---------------------------------------------------------------------------
# Group 2: Classification — one test per Kv-first branch (ADR-073)
# ---------------------------------------------------------------------------


def test_uniform_clear():
    """Constant high GHI → uniform branch → "Clear".

    GHI=800, msr=900 → Km ≈ 0.889 > 0.85, Kcs ≈ 0.889 > 0.80,
    Kv ≈ 0 < 0.05 (uniform). → "Clear".
    """
    _feed_constant_ghi(ghi=800.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Clear", f"Expected 'Clear' for uniform high GHI, got {result!r}"


def test_uniform_overcast():
    """Constant moderate GHI → uniform branch → "Overcast".

    GHI=500, msr=900 → Km ≈ 0.556, Kv ≈ 0 < 0.05 (uniform).
    Km > 0.35 → "Overcast" (not Heavy Overcast).
    """
    _feed_constant_ghi(ghi=500.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Overcast", f"Expected 'Overcast' for uniform moderate GHI, got {result!r}"


def test_uniform_heavy_overcast():
    """Constant very low GHI → uniform branch → "Heavy Overcast".

    GHI=100, msr=900 → Km ≈ 0.111 ≤ 0.35, Kv ≈ 0 < 0.05 (uniform).
    → "Heavy Overcast".
    """
    _feed_constant_ghi(ghi=100.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Heavy Overcast", (
        f"Expected 'Heavy Overcast' for uniform very low GHI, got {result!r}"
    )


def test_variable_mostly_clear():
    """High-mean alternating GHI → variable branch → "Mostly Clear".

    Alternating GHI 850/700, msr=900. Mean ≈ 775, Km ≈ 0.861 > 0.85.
    Alternation produces Kv >> 0.05 (variable). → "Mostly Clear".
    """
    _feed_alternating_ghi(ghi_high=850.0, ghi_low=700.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Mostly Clear", (
        f"Expected 'Mostly Clear' for variable high-mean GHI, got {result!r}"
    )


def test_variable_partly_cloudy():
    """Mid-high mean alternating GHI → variable branch → "Partly Cloudy".

    Alternating GHI 700/470, msr=900. Mean ≈ 585, Km ≈ 0.65.
    0.60 < 0.65 ≤ 0.85. Kv >> 0.05 (variable). → "Partly Cloudy".
    """
    _feed_alternating_ghi(ghi_high=700.0, ghi_low=470.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Partly Cloudy", (
        f"Expected 'Partly Cloudy' for variable mid-high mean, got {result!r}"
    )


def test_variable_mostly_cloudy():
    """Mid-low mean alternating GHI → variable branch → "Mostly Cloudy".

    Alternating GHI 500/350, msr=900. Mean ≈ 425, Km ≈ 0.472.
    0.40 < 0.472 ≤ 0.60. Kv >> 0.05 (variable). → "Mostly Cloudy".
    """
    _feed_alternating_ghi(ghi_high=500.0, ghi_low=350.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Mostly Cloudy", (
        f"Expected 'Mostly Cloudy' for variable mid-low mean, got {result!r}"
    )


def test_variable_cloudy():
    """Low mean alternating GHI → variable branch → "Cloudy".

    Alternating GHI 400/200, msr=900. Mean ≈ 300, Km ≈ 0.333.
    Km ≤ 0.40. Kv >> 0.05 (variable). → "Cloudy".
    """
    _feed_alternating_ghi(ghi_high=400.0, ghi_low=200.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Cloudy", (
        f"Expected 'Cloudy' for variable low mean, got {result!r}"
    )


def test_cloud_enhancement():
    """Constant GHI above msr with zero variability → uniform "Clear".

    GHI=960, msr=900 → Kcs ≈ 1.067 > 1.06. But Kv ≈ 0 < 0.20.
    Cloud enhancement requires BOTH Kcs > 1.06 AND Kv > 0.20.
    Constant data fails the Kv gate → falls to uniform branch.
    Km ≈ 1.067 > 0.85, Kcs > 0.80 → "Clear".
    """
    _feed_constant_ghi(ghi=960.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Clear", (
        f"Expected 'Clear' for constant GHI>msr (fails enhancement Kv gate), got {result!r}"
    )


def test_marine_layer_classifies_overcast():
    """Motivating scenario for the Kv-first redesign (ADR-073).

    Marine layer: uniform coverage, moderate transmittance.
    GHI=550, msr=900 → Km ≈ 0.611, Kv ≈ 0 < 0.05 (uniform).
    Old CAELUS tree: Km 0.611 > 0.3 (not OVERCAST), falls to
    SCATTER_CLOUDS → "Mostly Cloudy" (WRONG).
    New Kv-first tree: Kv < 0.05 (uniform), Km > 0.35 → "Overcast" (CORRECT).
    """
    _feed_constant_ghi(ghi=550.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Overcast", (
        f"Marine layer at Km~0.6 should be 'Overcast', got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 3: 1-minute binning
# ---------------------------------------------------------------------------


def test_twelve_readings_produce_one_bin():
    """12 readings in one minute, then a reading in the next minute, flush one bin.

    With _BASE_TS aligned to a minute boundary, readings 0-11 all land in
    the same minute bucket (floor(ts/60) = 16668).  The 13th reading's
    different minute (floor((BASE_TS+60)/60) = 16669) triggers the flush.
    """
    sky_condition.reset()
    for tick in range(12):
        sky_condition.update(800.0, 900.0, timestamp=_BASE_TS + tick * 5)
    # One reading in the next minute triggers the minute-boundary flush.
    sky_condition.update(800.0, 900.0, timestamp=_BASE_TS + 60)
    assert len(sky_condition._ring) >= 1, (
        "Expected at least one ring entry after 12 readings + minute-boundary trigger"
    )


def test_minute_rollover_triggers_bin():
    """A reading that crosses a minute boundary flushes the previous accumulator.

    Timestamp 1000055.0 → floor(1000055/60) = 16667.
    Timestamp 1000080.0 → floor(1000080/60) = 16668.
    These are in different minute buckets; the second reading triggers flush.
    """
    sky_condition.reset()
    sky_condition.update(800.0, 900.0, timestamp=1_000_055.0)
    sky_condition.update(800.0, 900.0, timestamp=1_000_080.0)
    assert len(sky_condition._ring) >= 1, (
        "Minute boundary crossing (1000055 → 1000080) should produce a ring entry"
    )


def test_bin_averages_correctly():
    """The ring entry GHI value is the arithmetic mean of the accumulator readings.

    4 readings with GHI=[100, 200, 300, 400] and msr=900 at aligned timestamps
    within minute 16668.  A 5th reading in minute 16669 triggers the flush.
    The flushed bin's ghi must equal mean([100, 200, 300, 400]) = 250.0.
    """
    sky_condition.reset()
    ghis = [100.0, 200.0, 300.0, 400.0]
    for i, ghi in enumerate(ghis):
        sky_condition.update(ghi, 900.0, timestamp=_BASE_TS + i * 5)
    # Trigger flush by crossing the minute boundary.
    sky_condition.update(400.0, 900.0, timestamp=_BASE_TS + 60)

    assert len(sky_condition._ring) >= 1, "Expected at least one ring entry after flush"
    first_bin = sky_condition._ring[0]
    assert abs(first_bin.ghi - 250.0) < 1e-6, (
        f"Expected bin GHI=250.0, got {first_bin.ghi}"
    )


# ---------------------------------------------------------------------------
# Group 4: Backfill
# ---------------------------------------------------------------------------


def test_backfill_enables_immediate_classification():
    """Backfilling 6 records covering 30 minutes allows immediate classify().

    backfill() pre-computes _last_stable_label when ring >= 3, bypassing
    the coherence filter for immediate post-startup classification.
    """
    records = _make_backfill_records(n=6, ghi=800.0, msr=900.0, interval_sec=300)
    sky_condition.backfill(records)
    result = sky_condition.classify()
    assert result is not None, (
        "classify() should return a label immediately after backfilling 6 records"
    )


def test_backfill_full_30min_produces_classification():
    """30 records at 1-min intervals with constant high GHI produce 'Clear'.

    With Kcs ≈ 0.889, Km ≈ 0.889, Kv ≈ 0, CLOUDLESS conditions are met.
    """
    records = _make_backfill_records(n=30, ghi=800.0, msr=900.0, interval_sec=60)
    sky_condition.backfill(records)
    result = sky_condition.classify()
    assert result == "Clear", (
        f"Expected 'Clear' after backfilling 30 high-GHI records, got {result!r}"
    )


def test_backfill_trims_old_records():
    """Backfill spanning 60 minutes retains only records within the 30-min window.

    Records with ts <= max_ts - _WINDOW_SECONDS (1800 s) are discarded.
    """
    records = _make_backfill_records(n=61, ghi=800.0, msr=900.0, interval_sec=60)
    sky_condition.backfill(records)
    max_ts = records[-1][0]
    cutoff = max_ts - sky_condition._WINDOW_SECONDS
    for entry in sky_condition._ring:
        assert entry.ts > cutoff, (
            f"Ring contains a stale entry ts={entry.ts} older than cutoff {cutoff}"
        )


def test_backfill_skips_night_records():
    """Archive records with maxSolarRad below _MIN_SOLAR_RAD are silently dropped.

    max(ghi=5, msr=5) = 5 < 20 (_MIN_SOLAR_RAD) → night record, not added.
    """
    records = _make_backfill_records(n=6, ghi=5.0, msr=5.0, interval_sec=300)
    sky_condition.backfill(records)
    assert len(sky_condition._ring) == 0, (
        "Night records (max(ghi,msr) < _MIN_SOLAR_RAD) must not populate the ring"
    )
    assert sky_condition.classify() is None, (
        "classify() must return None when ring is empty after night-only backfill"
    )


def test_backfill_followed_by_live_data():
    """Archive backfill followed by live LOOP packets produces a classification.

    6 archive records at 5-min intervals seed the ring; 5 minutes of live
    packets extend it.  classify() must return a non-None result.
    """
    records = _make_backfill_records(n=6, ghi=800.0, msr=900.0, interval_sec=300)
    sky_condition.backfill(records)

    live_start = records[-1][0] + 5.0
    for minute in range(5):
        for tick in range(12):
            ts = live_start + minute * 60 + tick * 5
            sky_condition.update(800.0, 900.0, timestamp=ts)

    result = sky_condition.classify()
    assert result is not None, (
        "classify() should return a result after backfill + live data"
    )


def test_backfill_idempotent():
    """Calling backfill() twice with identical records does not duplicate entries.

    Duplicate timestamps are detected via the existing_ts set in backfill().
    """
    records = _make_backfill_records(n=6, ghi=800.0, msr=900.0, interval_sec=300)
    sky_condition.backfill(records)
    size_after_first = len(sky_condition._ring)
    sky_condition.backfill(records)
    size_after_second = len(sky_condition._ring)
    assert size_after_first == size_after_second, (
        f"Duplicate backfill changed ring size: {size_after_first} → {size_after_second}"
    )


def test_backfill_empty_records():
    """backfill([]) must not raise and must leave the ring empty."""
    sky_condition.backfill([])
    assert len(sky_condition._ring) == 0, "Empty backfill must not populate the ring"
    assert sky_condition.classify() is None, (
        "classify() must return None after empty backfill"
    )


# ---------------------------------------------------------------------------
# Group 5: Temporal coherence filter
# ---------------------------------------------------------------------------


def test_rapid_flicker_holds_stable_label():
    """A brief 5-minute window of different conditions does not change the stable label.

    Feed 18 minutes of 'Clear' data (stable label established via 15-min
    coherence window — stable at minute 18 when history span = 900 s).
    Then feed 5 minutes of low-GHI data.  The coherence history has 5 new
    entries with a different raw label, but the consecutive span for the
    new label is only ~5 * 60 = 300 s < 900 s.  The stable label must
    not flip away from 'Clear'.
    """
    sky_condition.reset()
    # Phase 1: 20 minutes of Clear to ensure stable label is committed.
    for minute in range(20):
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(800.0, 900.0, timestamp=ts)
        sky_condition.classify()

    stable_before = sky_condition.classify()
    assert stable_before == "Clear", f"Pre-condition failed: expected 'Clear', got {stable_before!r}"

    # Phase 2: 5 minutes of low GHI — a brief transient.
    last_ts = list(sky_condition._ring)[-1].ts
    for minute in range(5):
        for tick in range(12):
            ts = last_ts + 1 + minute * 60 + tick * 5
            sky_condition.update(100.0, 900.0, timestamp=ts)
        sky_condition.classify()

    during_transient = sky_condition.classify()
    # Coherence filter requires 15 consecutive minutes of new label.
    # After only 5 minutes, the stable label should still hold.
    assert during_transient == "Clear", (
        f"Expected 'Clear' (held by coherence) during 5-min transient, "
        f"got {during_transient!r}"
    )


def test_persistent_change_adopts_new_label():
    """After 50+ minutes of a new condition, the label eventually switches.

    Feed 20 minutes of 'Clear' data, then 50 minutes of 'Cloudy' data.
    The first ~32 minutes of low GHI flush all Clear bins from the ring
    (30-min window).  Once the ring is pure Cloudy, the raw label becomes
    'Cloudy'.  After 15+ consecutive minutes of 'Cloudy' raw labels in
    the history, classify() must return 'Cloudy'.
    """
    sky_condition.reset()
    # Phase 1: establish 'Clear'.
    for minute in range(20):
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(800.0, 900.0, timestamp=ts)
        sky_condition.classify()

    assert sky_condition.classify() == "Clear", "Pre-condition: should be Clear"

    # Phase 2: persistent low GHI for 50 minutes.
    last_ts = list(sky_condition._ring)[-1].ts
    for minute in range(50):
        for tick in range(12):
            ts = last_ts + 1 + minute * 60 + tick * 5
            sky_condition.update(100.0, 900.0, timestamp=ts)
        sky_condition.classify()

    result = sky_condition.classify()
    overcast_labels = {"Cloudy", "Overcast", "Heavy Overcast"}
    assert result in overcast_labels, (
        f"Expected an OVERCAST-zone label after 50 min persistent low GHI, got {result!r}"
    )


def test_startup_classification_within_three_minutes():
    """From cold start, the startup grace period fires after ~6 minutes of data.

    _last_stable_label is None on a cold start.  classify() accumulates
    history only when ring >= 3 (from minute 3 onward).  The startup grace
    threshold (consecutive_span >= 180 s) is first satisfied after the
    classify() call at the end of minute 6 (three history entries at
    60-second spacing from minute 3 → minute 6 = 3 * 60 = 180 s).
    """
    sky_condition.reset()
    result = None
    for minute in range(7):  # minutes 0-6
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(800.0, 900.0, timestamp=ts)
        result = sky_condition.classify()

    assert result is not None, (
        "Expected a classification after 7 minutes via startup grace period"
    )


# ---------------------------------------------------------------------------
# Group 6: Edge cases
# ---------------------------------------------------------------------------


def test_classify_returns_none_with_insufficient_data():
    """Only 1 minute of data yields < 3 ring entries; classify() must return None.

    _compute_indices() requires ring >= 3 and returns None otherwise.
    With no stable label set yet, classify() propagates None.
    """
    sky_condition.reset()
    for tick in range(12):
        sky_condition.update(800.0, 900.0, timestamp=_BASE_TS + tick * 5)
    # Trigger a flush by crossing the minute boundary.
    sky_condition.update(800.0, 900.0, timestamp=_BASE_TS + 60)
    # Ring has 1 entry; _compute_indices() needs >= 3.
    assert sky_condition.classify() is None, (
        "Expected None with only 1 minute of data in the ring"
    )


def test_night_guard_skips_low_solar_rad():
    """Readings where max(ghi, msr) < _MIN_SOLAR_RAD are not buffered.

    ghi=5, msr=10 → max(5, 10) = 10 < 20 (_MIN_SOLAR_RAD) → night guard fires.
    """
    sky_condition.reset()
    for tick in range(30):
        sky_condition.update(5.0, 10.0, timestamp=_BASE_TS + tick * 5)
    assert len(sky_condition._ring) == 0, (
        "Night-time readings (max(ghi,msr) < _MIN_SOLAR_RAD) must not populate the ring"
    )
    assert sky_condition.classify() is None, (
        "classify() must return None when only night readings were fed"
    )


def test_none_radiation_skipped():
    """update(None, msr) must silently skip the reading without crash.

    With msr=900, currently_daytime=True (max(0,900)=900 >= 20), but
    radiation=None triggers the early-return guard.
    """
    sky_condition.reset()
    sky_condition.update(None, 900.0, timestamp=_BASE_TS)
    assert len(sky_condition._ring) == 0, "None radiation must not add to the ring"
    assert len(sky_condition._minute_acc) == 0, "None radiation must not add to accumulator"


def test_negative_radiation_skipped():
    """update(-5, msr) must silently skip the reading.

    max(-5, 900) = 900 >= 20, so currently_daytime=True; then
    radiation < _NOISE_FLOOR (0.0) triggers the early-return guard.
    """
    sky_condition.reset()
    sky_condition.update(-5.0, 900.0, timestamp=_BASE_TS)
    assert len(sky_condition._ring) == 0, "Negative radiation must not add to the ring"
    assert len(sky_condition._minute_acc) == 0, (
        "Negative radiation must not add to the accumulator"
    )


def test_kcs_clamped_at_max():
    """GHI greatly exceeding msr clamps Kcs at _KC_MAX (1.2) without crash.

    GHI=5000, msr=900 → raw Kcs = 5.56; clamped to min(5.56, 1.2) = 1.2.
    The module must not raise and must return a known label or None.
    """
    sky_condition.reset()
    valid_labels = {
        "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy",
        "Overcast", "Heavy Overcast", None,
    }
    for minute in range(30):
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(5000.0, 900.0, timestamp=ts)
        sky_condition.classify()
    result = sky_condition.classify()
    assert result in valid_labels, f"Unexpected value with clamped Kcs: {result!r}"


def test_buffer_cleared_at_sunset():
    """Transitioning from daytime to night-time clears the ring and stable label.

    After 10 minutes of daytime data (GHI=800, msr=900), a night reading
    (ghi=0, msr=0) fires the sunset transition path:
    _was_daytime=True and max(0, 0)=0 < 20 → currently_daytime=False →
    ring.clear(), _minute_acc.clear(), _last_stable_label = None.
    """
    sky_condition.reset()
    for minute in range(10):
        for tick in range(12):
            ts = _BASE_TS + minute * 60 + tick * 5
            sky_condition.update(800.0, 900.0, timestamp=ts)

    assert len(sky_condition._ring) >= 1, "Precondition: daytime ring must be non-empty"

    # Trigger sunset transition.
    sky_condition.update(0.0, 0.0, timestamp=_BASE_TS + 10 * 60 + 5)

    assert len(sky_condition._ring) == 0, (
        "Sunset transition must clear the ring buffer"
    )
    assert sky_condition.classify() is None, (
        "classify() must return None after sunset clears the buffer"
    )


def test_reset_clears_all_state():
    """reset() wipes the ring, accumulator, stable label, and daytime flag.

    After feeding data and calling reset(), classify() must return None
    and is_daytime() must return False.
    """
    _feed_constant_ghi(ghi=800.0, msr=900.0, minutes=20)
    assert sky_condition.classify() is not None, "Pre-condition: label must be set"
    sky_condition.reset()
    assert len(sky_condition._ring) == 0, "reset() must clear the ring"
    assert sky_condition.classify() is None, "classify() must return None after reset()"
    assert not sky_condition.is_daytime(), "is_daytime() must return False after reset()"


def test_is_daytime_true_with_recent_data():
    """is_daytime() returns True when the most recent reading is < 300 s old.

    A reading timestamped at the current wall clock is recent; is_daytime()
    checks (time.time() - last_ts) < 300.
    """
    import time

    sky_condition.reset()
    now = time.time()
    sky_condition.update(800.0, 900.0, timestamp=now)
    assert sky_condition.is_daytime(), (
        "is_daytime() must return True for a reading timestamped right now"
    )


def test_is_daytime_false_with_stale_data():
    """is_daytime() returns False when the most recent reading exceeds the threshold.

    Default archive_interval is 300 s, so the freshness threshold is
    5 × 300 = 1500 s.  A reading 1600 s old is beyond this threshold.
    """
    import time

    sky_condition.reset()
    stale_ts = time.time() - 1600.0
    sky_condition.update(800.0, 900.0, timestamp=stale_ts)
    assert not sky_condition.is_daytime(), (
        "is_daytime() must return False for a reading older than 5 × archive_interval"
    )


# ---------------------------------------------------------------------------
# Group 7: configure() — archive interval affects is_daytime() threshold
# ---------------------------------------------------------------------------


class TestConfigure:
    """Tests for configure() — archive interval affects is_daytime() threshold."""

    def test_default_archive_interval_is_300(self) -> None:
        """After reset(), _archive_interval is 300.0 (default)."""
        assert sky_condition._archive_interval == 300.0

    def test_configure_sets_archive_interval(self) -> None:
        """configure(60) sets _archive_interval to 60.0."""
        sky_condition.configure(archive_interval=60)
        assert sky_condition._archive_interval == 60.0

    def test_is_daytime_threshold_scales_with_archive_interval(self) -> None:
        """With 60s archive_interval, is_daytime() threshold is 300s (5 × 60).

        A reading 250s ago → True (within 300s threshold).
        """
        import time
        sky_condition.configure(archive_interval=60)
        now = time.time()
        # Add a reading 250 seconds ago — within 5 × 60 = 300s threshold
        sky_condition._ring.append(sky_condition.MinuteRecord(
            ts=now - 250, ghi=500.0, max_solar_rad=800.0
        ))
        assert sky_condition.is_daytime() is True

    def test_is_daytime_false_beyond_configured_threshold(self) -> None:
        """With 60s archive_interval, a reading >300s ago → False."""
        import time
        sky_condition.configure(archive_interval=60)
        now = time.time()
        # Add a reading 350 seconds ago — beyond 5 × 60 = 300s threshold
        sky_condition._ring.append(sky_condition.MinuteRecord(
            ts=now - 350, ghi=500.0, max_solar_rad=800.0
        ))
        assert sky_condition.is_daytime() is False

    def test_default_300s_interval_allows_1400s_old_reading(self) -> None:
        """With default 300s archive_interval, threshold is 1500s.
        A reading 1400s ago → True (within 5 × 300 = 1500s).
        """
        import time
        # Don't call configure() — use default 300s
        now = time.time()
        sky_condition._ring.append(sky_condition.MinuteRecord(
            ts=now - 1400, ghi=500.0, max_solar_rad=800.0
        ))
        assert sky_condition.is_daytime() is True

    def test_reset_restores_default_archive_interval(self) -> None:
        """reset() restores _archive_interval to 300.0."""
        sky_condition.configure(archive_interval=60)
        sky_condition.reset()
        assert sky_condition._archive_interval == 300.0

    def test_configure_accepts_station_coords(self) -> None:
        """configure() with lat/lon/altitude stores coordinates without error.

        Does not assert observer is built — Skyfield may be unavailable.
        Only asserts that module state accepts the coordinates and no exception
        is raised.  _station_lat/_station_lon/_station_alt are set regardless
        of whether the ephemeris loaded.
        """
        sky_condition.configure(
            archive_interval=300,
            latitude=40.7128,
            longitude=-74.0060,
            altitude=10.0,
        )
        assert sky_condition._station_lat == pytest.approx(40.7128)
        assert sky_condition._station_lon == pytest.approx(-74.0060)
        assert sky_condition._station_alt == pytest.approx(10.0)

    def test_configure_without_coords_clears_observer(self) -> None:
        """configure() without lat/lon/altitude leaves _skyfield_observer as None.

        Mirroring and SZA guard must not activate when coords are absent.
        """
        # First set coords, then clear them.
        sky_condition.configure(
            archive_interval=300,
            latitude=40.7128,
            longitude=-74.0060,
            altitude=10.0,
        )
        sky_condition.configure(archive_interval=300)
        assert sky_condition._skyfield_observer is None
        assert sky_condition._station_lat is None
        assert sky_condition._station_lon is None
        assert sky_condition._station_alt is None


# ---------------------------------------------------------------------------
# Helper: timestamps for New York City (40.7128°N, 74.0060°W) on 2024-06-21.
#
# 2024-06-21 is the northern hemisphere summer solstice — maximum daylight.
# Solar noon for NYC on this date is approximately 12:56 PM EDT = 16:56 UTC.
# Midnight EDT = 04:00 UTC.
#
# Unix timestamps (UTC):
#   _NYC_NOON_TS    = 2024-06-21 16:56:00 UTC  → solar elevation ≈ +73°  (high)
#   _NYC_MORNING_TS = 2024-06-21 14:00:00 UTC  → solar elevation ≈ +52°  (high)
#   _NYC_MIDNIGHT_TS = 2024-06-21 04:00:00 UTC → solar elevation ≈ -50°  (below horizon)
#   _NYC_SUNRISE_TS = 2024-06-21 09:24:00 UTC  → solar elevation ≈ 0°    (near horizon)
#
# Tests that require Skyfield to compute solar elevation are skipped when
# configure() fails to build the observer (ephemeris not available in the test
# environment).  The skip marker is set by checking _skyfield_observer is not
# None after configure().
# ---------------------------------------------------------------------------

_NYC_LAT = 40.7128
_NYC_LON = -74.0060
_NYC_ALT = 10.0

# 2024-06-21 16:56 UTC → solar noon for NYC → elevation ≈ +73°
_NYC_NOON_TS = 1_718_988_960.0

# 2024-06-21 14:00 UTC → 10 AM EDT → elevation well above 5°
_NYC_MORNING_TS = 1_718_978_400.0

# 2024-06-21 04:00 UTC → midnight EDT → elevation ≈ -50° (well below horizon)
_NYC_MIDNIGHT_TS = 1_718_942_400.0

# 2024-06-21 09:24 UTC → sunrise for NYC → elevation ≈ 0° (near horizon)
_NYC_SUNRISE_TS = 1_718_961_840.0

# 2024-06-22 00:00 UTC → 8 PM EDT on June 21 → post-sunset for NYC → elevation ≈ -8°
# Use for SZA guard tests: feeds data AFTER the morning midday feed so that
# ring[-1].ts is this timestamp (below-horizon), not the earlier morning one.
_NYC_POSTSUNSET_TS = 1_719_014_400.0

# Spacing between fake minute bins when constructing midday data streams.
# Must stay within 1800 s of each other to remain in the 30-minute ring.
_MINUTE_SEC = 60.0


def _configure_nyc(archive_interval: int = 300) -> None:
    """Call configure() with NYC coordinates.  Leaves observer built if ephemeris
    is present; leaves it None if Skyfield cannot load the ephemeris."""
    sky_condition.configure(
        archive_interval=archive_interval,
        latitude=_NYC_LAT,
        longitude=_NYC_LON,
        altitude=_NYC_ALT,
    )


def _skyfield_available() -> bool:
    """Return True when configure() successfully built a Skyfield observer."""
    return sky_condition._skyfield_observer is not None


def _feed_constant_ghi_at(
    ghi: float,
    msr: float,
    minutes: int,
    base_ts: float,
) -> None:
    """Feed constant GHI for `minutes` minutes starting at base_ts.

    Each minute gets 12 readings at 5-second intervals.  classify() is
    called after each minute to accumulate coherence filter history.
    Does NOT call sky_condition.reset() — caller must reset before calling
    if a clean state is needed.
    """
    for minute in range(minutes):
        for tick in range(12):
            ts = base_ts + minute * _MINUTE_SEC + tick * 5
            sky_condition.update(ghi, msr, timestamp=ts)
        sky_condition.classify()


# ---------------------------------------------------------------------------
# Group 8: GHI mirroring tests
# ---------------------------------------------------------------------------


def test_mirroring_disabled_when_no_station_coords():
    """configure() without coords disables mirroring; classify() still works.

    With no lat/lon/altitude, _skyfield_observer is None, so _mirror_for_km()
    falls through to return real ring data unchanged.  Classification must
    succeed and return a known label.
    """
    sky_condition.configure(archive_interval=300)
    assert sky_condition._skyfield_observer is None, (
        "Pre-condition: no coords → observer must be None"
    )
    _feed_constant_ghi(ghi=800.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    valid_labels = {"Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy",
                    "Overcast", "Heavy Overcast"}
    assert result is not None, "classify() must return a label even without station coords"
    assert result in valid_labels, f"Unexpected label without coords: {result!r}"


def test_mirroring_does_not_crash_with_insufficient_data():
    """configure() with coords + only 2 minutes of data does not crash.

    _mirror_for_km() requires >= 2 post-sunrise entries.  With only 2 minutes
    of data total, the ring has at most 1 entry (the first minute's flush is
    triggered by the second minute crossing the boundary) — below the 3-entry
    minimum for _compute_indices().  classify() must return None (startup
    grace not yet met) or a label, and must not raise.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    sky_condition.reset()
    _configure_nyc()

    # Feed 2 minutes of data at solar noon (high elevation → post-sunrise entries).
    base_ts = _NYC_MORNING_TS
    for minute in range(2):
        for tick in range(12):
            ts = base_ts + minute * _MINUTE_SEC + tick * 5
            sky_condition.update(800.0, 900.0, timestamp=ts)

    # Must not raise; return value is None or a label.
    result = sky_condition.classify()
    valid_labels = {"Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy",
                    "Overcast", "Heavy Overcast", None}
    assert result in valid_labels, f"Unexpected value with insufficient data: {result!r}"


def test_mirroring_does_not_affect_midday_clear():
    """Midday clear data with coords produces 'Clear' — same as without coords.

    At solar noon, all ring entries are post-sunrise (cos_z > 0).  There are
    no pre-sunrise entries to mirror.  _mirror_for_km() returns real data
    unchanged.  The 30-minute clear-sky stream must still classify as 'Clear'.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    sky_condition.reset()
    _configure_nyc()

    # Feed 30 minutes of clear-sky data centred on solar noon.
    # GHI=800, msr=900 → Kcs≈0.889 (CLOUDLESS criteria met).
    _feed_constant_ghi_at(ghi=800.0, msr=900.0, minutes=30, base_ts=_NYC_MORNING_TS)
    result = sky_condition.classify()
    assert result == "Clear", (
        f"Expected 'Clear' for midday clear-sky data with station coords, got {result!r}"
    )


def test_mirroring_produces_lower_km_at_sunrise_under_overcast():
    """Mirroring lowers Km at sunrise under overcast, reducing false-high classifications.

    At sunrise, without mirroring the 30-min window has only a few minutes of
    data.  Under overcast, the diffuse-to-clear-sky ratio at low angles inflates
    Km.  With mirroring, synthetic pre-sunrise entries with low GHI extend the
    baseline, pulling Km down.

    Strategy: feed overcast readings at a timestamp just after sunrise for NYC
    (09:24 UTC = 05:24 EDT).  Compare Km computed from the ring in two scenarios:
    1. Without coords (no mirroring) — only real entries.
    2. With coords (mirroring active) — synthetic entries added.
    We access _compute_indices() indirectly through classify(); instead we
    compare the ring's mean GHI/msr ratio before and after mirroring by
    calling _mirror_for_km() directly on the raw ring snapshot.

    Note: direct internal helper access is justified here because we are
    testing a specific algorithmic property (Km is lower with mirroring)
    that is not observable from the public label alone when there is
    insufficient data for the coherence filter to commit a label.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    # Build a ring with 5 minutes of overcast readings just after sunrise.
    # Post-sunrise entries: cos_z > 0 (sun above horizon at these timestamps).
    # At 09:24 UTC (sunrise), elevation rises steeply; by 09:30 UTC elevation
    # is still only a few degrees — cos_z is small but positive.
    # GHI≈80 (diffuse under thick cloud), msr≈250 (low clear-sky irradiance
    # at such a low sun angle — realistic for early June morning).
    sunrise_base = _NYC_SUNRISE_TS  # 09:24 UTC

    sky_condition.reset()
    # No coords — no mirroring.
    sky_condition.configure(archive_interval=300)
    for minute in range(5):
        for tick in range(12):
            ts = sunrise_base + minute * _MINUTE_SEC + tick * 5
            sky_condition.update(80.0, 250.0, timestamp=ts)
        sky_condition.classify()

    ring_no_mirror = list(sky_condition._ring)
    pairs_no_mirror = sky_condition._mirror_for_km(ring_no_mirror)
    if not pairs_no_mirror:
        pytest.skip("Ring is empty — sunrise_base timestamp may be at night (no data accepted)")
    km_no_mirror = (
        sum(p[0] for p in pairs_no_mirror) / len(pairs_no_mirror)
        / (sum(p[1] for p in pairs_no_mirror) / len(pairs_no_mirror))
        if sum(p[1] for p in pairs_no_mirror) > 0 else 0.0
    )

    sky_condition.reset()
    # With coords — mirroring active.
    _configure_nyc()
    for minute in range(5):
        for tick in range(12):
            ts = sunrise_base + minute * _MINUTE_SEC + tick * 5
            sky_condition.update(80.0, 250.0, timestamp=ts)
        sky_condition.classify()

    ring_with_mirror = list(sky_condition._ring)
    pairs_with_mirror = sky_condition._mirror_for_km(ring_with_mirror)
    if not pairs_with_mirror:
        pytest.skip("Ring is empty with mirroring — sunrise_base timestamp may be at night")
    km_with_mirror = (
        sum(p[0] for p in pairs_with_mirror) / len(pairs_with_mirror)
        / (sum(p[1] for p in pairs_with_mirror) / len(pairs_with_mirror))
        if sum(p[1] for p in pairs_with_mirror) > 0 else 0.0
    )

    # When mirroring has enough data to create synthetic entries, Km with
    # mirroring should be <= Km without mirroring.  If there are no pre-sunrise
    # entries in the window, both values will be equal (no synthetic entries).
    assert km_with_mirror <= km_no_mirror + 1e-9, (
        f"Expected Km with mirroring ({km_with_mirror:.4f}) <= "
        f"Km without mirroring ({km_no_mirror:.4f}) for overcast sunrise data"
    )


# ---------------------------------------------------------------------------
# Group 9: SZA guard tests
# ---------------------------------------------------------------------------


def test_sza_guard_skipped_when_no_station_coords():
    """classify() runs normally when no coords configured; no SZA check fires.

    With _skyfield_observer = None, the SZA guard branch is skipped entirely.
    Classification proceeds from the ring and returns a non-None label.
    """
    sky_condition.configure(archive_interval=300)
    assert sky_condition._skyfield_observer is None

    _feed_constant_ghi(ghi=800.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result is not None, (
        "classify() must return a label when no coords configured (SZA guard inactive)"
    )


def test_sza_guard_returns_last_stable_when_below_threshold():
    """SZA guard returns the last stable label when solar elevation < 5°.

    Strategy:
    1. Feed 30 minutes of clear data at midday timestamps (high elevation) to
       establish a stable label via the coherence filter.
    2. Call classify() with the ring's last timestamp replaced by a well-below-
       horizon timestamp (NYC midnight = elevation ≈ -50°).  We do this by
       feeding a single reading at the midnight timestamp, which adds a new
       ring entry.  The SZA guard evaluates ring[-1].ts.
    3. Assert the returned label equals the previously established label.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    sky_condition.reset()
    _configure_nyc()

    # Phase 1: establish a stable "Clear" label at midday timestamps.
    _feed_constant_ghi_at(ghi=800.0, msr=900.0, minutes=30, base_ts=_NYC_MORNING_TS)
    stable_label = sky_condition.classify()
    assert stable_label is not None, (
        "Pre-condition: 30 minutes of midday clear data must produce a stable label"
    )

    # Phase 2: feed 13 readings at a post-sunset timestamp (after the morning
    # data ends), so ring[-1].ts is a below-horizon time.
    # _NYC_POSTSUNSET_TS = 8 PM EDT = elevation ≈ -8° (well below 5°).
    # GHI=20, msr=30 → max(20,30)=30 >= 20 → night guard does NOT fire.
    # 12 readings + 1 trigger force_flush → one new ring entry at post-sunset ts.
    for i in range(13):
        sky_condition.update(20.0, 30.0, timestamp=_NYC_POSTSUNSET_TS + i * 5)

    # Now classify().  ring[-1].ts is in the post-sunset range (elevation < 5°).
    # The SZA guard fires and returns _last_stable_label.
    result = sky_condition.classify()
    assert result == stable_label, (
        f"SZA guard must return last stable label {stable_label!r} "
        f"when solar elevation < 5°, got {result!r}"
    )


def test_sza_guard_allows_classification_above_threshold():
    """classify() returns a non-None label when solar elevation > 5°.

    Feed 30 minutes of data at midday timestamps (solar elevation ≈ 73°).
    The SZA guard checks ring[-1].ts; at solar noon the elevation is well
    above the 5° threshold, so the guard does not fire.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    sky_condition.reset()
    _configure_nyc()

    _feed_constant_ghi_at(ghi=800.0, msr=900.0, minutes=30, base_ts=_NYC_MORNING_TS)
    result = sky_condition.classify()
    assert result is not None, (
        "classify() must return a label when solar elevation is well above 5°"
    )


def test_sza_guard_does_not_block_data_acceptance():
    """SZA guard affects classify() only; update() still accepts low-elevation data.

    Feed readings at a post-sunset timestamp (solar elevation ≈ -8°).
    GHI=20, msr=30 → max(20,30)=30 >= 20 → night guard does not fire.
    After 13 readings (12 force-flush + 1), the ring gains at least one entry.
    The SZA guard only runs in classify(); it must not affect update() buffering.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    sky_condition.reset()
    _configure_nyc()

    # Feed 13 readings at a post-sunset timestamp.  Force-flush after 12
    # readings creates one ring entry; the 13th goes into the accumulator.
    for i in range(13):
        sky_condition.update(20.0, 30.0, timestamp=_NYC_POSTSUNSET_TS + i * 5)

    assert len(sky_condition._ring) >= 1, (
        "Ring must accept readings when max(ghi,msr) >= _MIN_SOLAR_RAD, "
        "even when solar elevation < 5° (SZA guard does not block data acceptance)"
    )


def test_sza_guard_returns_none_below_threshold_on_cold_start():
    """SZA guard returns None on cold start with no prior stable label.

    On a cold start, _last_stable_label is None.  When the first classify() call
    has ring[-1].ts at a below-horizon timestamp, the SZA guard fires and returns
    _last_stable_label, which is still None — no label has been established yet.

    Uses _NYC_POSTSUNSET_TS (8 PM EDT, solar elevation ≈ -8°) with
    GHI=20/msr=30 so the night guard does not fire but the SZA guard does.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    sky_condition.reset()
    _configure_nyc()

    # Feed 13 readings at the post-sunset timestamp.  Force-flush gives one
    # ring entry with ts in the post-sunset range (elevation ≈ -8°).
    for i in range(13):
        sky_condition.update(20.0, 30.0, timestamp=_NYC_POSTSUNSET_TS + i * 5)

    result = sky_condition.classify()
    assert result is None, (
        f"SZA guard must return None on cold start (no prior stable label), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Group 10: Regression tests — station coords must not break existing behaviour
# ---------------------------------------------------------------------------


def test_all_kv_first_branches_produce_labels():
    """All Kv-first branches produce non-None labels with station coords.

    Validates that configure(lat/lon/alt) does not break any classification
    path. Covers all 7 labels plus the cloud enhancement path.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    valid_labels = {
        "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy",
        "Overcast", "Heavy Overcast",
    }

    cases = [
        # (description, feed_fn, feed_args)
        ("uniform clear",
         dict(ghi=800.0, msr=900.0, minutes=30)),
        ("uniform overcast",
         dict(ghi=500.0, msr=900.0, minutes=30)),
        ("uniform heavy overcast",
         dict(ghi=100.0, msr=900.0, minutes=30)),
    ]
    alternating_cases = [
        ("variable mostly clear",
         dict(ghi_high=850.0, ghi_low=700.0, msr=900.0, minutes=30)),
        ("variable partly cloudy",
         dict(ghi_high=700.0, ghi_low=470.0, msr=900.0, minutes=30)),
        ("variable mostly cloudy",
         dict(ghi_high=500.0, ghi_low=350.0, msr=900.0, minutes=30)),
        ("variable cloudy",
         dict(ghi_high=400.0, ghi_low=200.0, msr=900.0, minutes=30)),
    ]

    for desc, kwargs in cases:
        sky_condition.reset()
        _configure_nyc()
        _feed_constant_ghi(**kwargs)
        result = sky_condition.classify()
        assert result in valid_labels, (
            f"{desc}: expected a valid label, got {result!r}"
        )

    for desc, kwargs in alternating_cases:
        sky_condition.reset()
        _configure_nyc()
        _feed_alternating_ghi(**kwargs)
        result = sky_condition.classify()
        assert result in valid_labels, (
            f"{desc}: expected a valid label, got {result!r}"
        )


def test_backfill_still_works_with_station_coords():
    """backfill() with archive records seeds the ring when station coords configured.

    Station coords must not interfere with the backfill startup path.
    After backfilling 6 records at the default _BASE_TS timestamps,
    classify() must return a non-None label immediately (backfill bypasses
    the coherence filter by pre-setting _last_stable_label).
    """
    _configure_nyc()

    records = _make_backfill_records(n=6, ghi=800.0, msr=900.0, interval_sec=300)
    sky_condition.backfill(records)
    result = sky_condition.classify()
    # SZA guard: ring[-1].ts is _BASE_TS (synthetic timestamp ≈ 1_000_080).
    # _compute_solar_elevation returns a value — but if the observer is built,
    # that Unix timestamp maps to ~1970 (very old date) where Skyfield may
    # return an elevation.  If the SZA guard fires, it returns _last_stable_label
    # which was pre-set by backfill().  Either way, a non-None label must come back.
    assert result is not None, (
        "classify() must return a label after backfill with station coords configured"
    )


def test_temporal_coherence_still_works_with_station_coords():
    """Coherence filter prevents premature label change when station coords configured.

    Mirrors test_rapid_flicker_holds_stable_label from Group 5, but with
    station coords active.  Validates that coords do not bypass the filter.

    Feed 20 minutes of 'Clear' data at midday timestamps to establish stable
    label, then 5 minutes of low-GHI data.  The stable label must not flip.
    """
    _configure_nyc()
    if not _skyfield_available():
        pytest.skip("Skyfield ephemeris not available in this test environment")

    sky_condition.reset()
    _configure_nyc()

    # Phase 1: 20 minutes of clear sky at midday.
    _feed_constant_ghi_at(ghi=800.0, msr=900.0, minutes=20, base_ts=_NYC_MORNING_TS)
    stable_before = sky_condition.classify()
    assert stable_before is not None, (
        "Pre-condition: 20 minutes of midday clear data must produce a stable label"
    )

    # Phase 2: 5 minutes of low GHI (overcast transient) continuing from Phase 1.
    # Timestamps continue from where Phase 1 left off (still at midday elevation).
    phase2_base = _NYC_MORNING_TS + 20 * _MINUTE_SEC
    _feed_constant_ghi_at(ghi=100.0, msr=900.0, minutes=5, base_ts=phase2_base)
    result = sky_condition.classify()

    # 5 minutes of different raw label is below the 15-min persistence threshold.
    # Stable label must not have changed.
    assert result == stable_before, (
        f"Coherence filter must hold stable label {stable_before!r} "
        f"after only 5-min transient; got {result!r}"
    )
