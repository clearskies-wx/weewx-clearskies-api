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
    valid_labels = {"Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy", None}
    assert result in valid_labels, f"Unexpected return value with Kcs > 1.0: {result!r}"


# ---------------------------------------------------------------------------
# Group 2: Classification — one test per CAELUS class
# ---------------------------------------------------------------------------


def test_cloudless_classification():
    """Stable constant high GHI for 30 minutes classifies as 'Clear'.

    GHI=800, msr=900 → Kcs ≈ 0.889 ∈ [0.85, 1.15], Km ≈ 0.889 > 0.6,
    Kv = 0 < 0.03, msr=900 > 200 (SZA75 proxy for midday).
    30 minutes of classify() calls builds 27+ history entries spanning
    > 900 s, satisfying the full 15-minute stability window.
    """
    _feed_constant_ghi(ghi=800.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Clear", f"Expected 'Clear' for CLOUDLESS conditions, got {result!r}"


def test_overcast_classification():
    """Constant low GHI for 30 minutes classifies as 'Cloudy'.

    GHI=100, msr=900 → Km ≈ 0.111 < 0.3 (_OVERCAST_MAX_KM), Kv = 0 < 0.10.
    OVERCAST branch fires and maps to NWS 'Cloudy'.
    """
    _feed_constant_ghi(ghi=100.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Cloudy", f"Expected 'Cloudy' for OVERCAST conditions, got {result!r}"


def test_thin_clouds_classification():
    """Small GHI oscillation around a high mean classifies as 'Mostly Clear'.

    Alternating GHI between 501.5 and 498.5 W/m², msr=900.
    Mean = 500 W/m², deviation amplitude = 1.5 W/m².
    Kv ≈ 1.5 / 30 = 0.050 ∈ [0.03, 0.08).
    Km ≈ 500 / 900 ≈ 0.556 > 0.5.
    → THIN_CLOUDS → 'Mostly Clear'.
    """
    _feed_alternating_ghi(ghi_high=501.5, ghi_low=498.5, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Mostly Clear", (
        f"Expected 'Mostly Clear' for THIN_CLOUDS conditions, got {result!r}"
    )


def test_thick_clouds_classification():
    """Small GHI oscillation around a moderate-low mean classifies as 'Mostly Cloudy'.

    Alternating GHI between 316.5 and 313.5 W/m², msr=900.
    Mean = 315 W/m², deviation amplitude = 1.5 W/m².
    Kv ≈ 1.5 / 30 = 0.050 ∈ [0.04, 0.16).
    Km ≈ 315 / 900 ≈ 0.350, in (0.3, 0.4) — OVERCAST excluded (Km > 0.3).
    → THICK_CLOUDS → 'Mostly Cloudy'.
    """
    _feed_alternating_ghi(ghi_high=316.5, ghi_low=313.5, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Mostly Cloudy", (
        f"Expected 'Mostly Cloudy' for THICK_CLOUDS conditions, got {result!r}"
    )


def test_scatter_clouds_classification():
    """Large GHI oscillation around a mid mean falls through to 'Partly Cloudy'.

    Alternating GHI between 600 and 300 W/m², msr=900.
    Mean = 450 W/m², Km ≈ 0.50.  Deviation amplitude = 150 W/m².
    Kv ≈ 150 / 30 = 5.0 — far outside THIN_CLOUDS (< 0.08) and
    THICK_CLOUDS (< 0.16) ranges.  Falls through to SCATTER_CLOUDS
    default → 'Partly Cloudy'.
    """
    _feed_alternating_ghi(ghi_high=600.0, ghi_low=300.0, msr=900.0, minutes=30)
    result = sky_condition.classify()
    assert result == "Partly Cloudy", (
        f"Expected 'Partly Cloudy' for SCATTER_CLOUDS conditions, got {result!r}"
    )


def test_cloud_enhancement_classification():
    """High GHI with high variability triggers CLOUD_ENHANCEMENT → 'Partly Cloudy'.

    Even minutes → GHI=1000 W/m², msr=900; odd minutes → GHI=400 W/m².
    The last ring entry corresponds to minute 28 (even=1000):
      Kcs = 1000 / 900 ≈ 1.111 > 1.06 (_CLOUDEN_MIN_KCS)
    Deviation amplitude = 300 W/m².
    Kv ≈ 300 / 30 = 10 > 0.20 (_CLOUDEN_MIN_KV).
    Kvf ≈ 11 > 0.20 (_CLOUDEN_MIN_KVF).
    msr=900 > 100 (_SZA80_MSR_PROXY).
    → CLOUD_ENHANCEMENT → 'Partly Cloudy'.

    Note: minute 29 (the last loop iteration) is odd; its readings sit in
    the accumulator unflushed.  The last flushed ring bin is minute 28
    (even → GHI=1000), giving Kcs > 1.06 for the CLOUD_ENHANCEMENT check.
    """
    _feed_alternating_ghi(
        ghi_high=1000.0,
        ghi_low=400.0,
        msr=900.0,
        minutes=30,
        high_on_even=True,  # minute 28 (last flushed) is even → ghi=1000
    )
    result = sky_condition.classify()
    assert result == "Partly Cloudy", (
        f"Expected 'Partly Cloudy' for CLOUD_ENHANCEMENT conditions, got {result!r}"
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
    assert result == "Cloudy", (
        f"Expected 'Cloudy' after 50 min persistent low GHI, got {result!r}"
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
    valid_labels = {"Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy", None}
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
    """is_daytime() returns False when the most recent reading is > 300 s old.

    A reading 10 minutes in the past is 600 s old, well beyond the 300-second
    threshold in is_daytime().
    """
    import time

    sky_condition.reset()
    stale_ts = time.time() - 600.0
    sky_condition.update(800.0, 900.0, timestamp=stale_ts)
    assert not sky_condition.is_daytime(), (
        "is_daytime() must return False for a reading timestamped 10 minutes ago"
    )
