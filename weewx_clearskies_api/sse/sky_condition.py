"""Sky condition classification from solar radiation (ADR-044, amended).

Uses the Variability Index (VI) system adapted from CAELUS (Ruiz-Arias &
Gueymard 2023) to classify sky conditions from GHI (measured radiation)
and a clear-sky reference (maxSolarRad from weewx).

Four indices computed from a 30-minute rolling window of 1-minute
averaged GHI data:
  - Kcs: instantaneous clear-sky index (GHI / maxSolarRad)
  - Km:  mean normalized irradiance over the window
  - Kv:  coarse variability index (30-min) — cumulative absolute
         first-derivative of GHI deviation from rolling mean
  - Kvf: fine variability index (10-min) — same as Kv, shorter window

Six CAELUS classes mapped to NWS display vocabulary:
  CLOUDLESS          → "Clear"
  THIN_CLOUDS        → "Mostly Clear"
  SCATTER_CLOUDS     → "Partly Cloudy"
  THICK_CLOUDS       → "Mostly Cloudy"
  OVERCAST           → "Cloudy"
  CLOUD_ENHANCEMENT  → "Partly Cloudy"

Temporal coherence filter: a new classification must persist for 15
consecutive minutes before replacing the previous stable label.
Prevents rapid flicker at class boundaries.

Data flow: 5-second LOOP packets → 1-minute bins → 30-minute ring buffer
→ index computation → CAELUS decision tree → temporal coherence filter.

Startup backfill: archive records can seed the ring buffer for immediate
(if coarser) classification on API restart.

Deviations from CAELUS:
  - maxSolarRad used as clear-sky reference (CAELUS uses ghicda)
  - Solar zenith angle approximated via maxSolarRad thresholds
  - GHI mirroring omitted (_MIN_SOLAR_RAD guard excludes low-sun periods)
  - Trailing window instead of centered (necessary for real-time)
  - Streaming temporal coherence filter instead of batch patch cleaning

Reference: Ruiz-Arias & Gueymard (2023), Solar Energy 263, 111895.
CAELUS source: github.com/jararias/caelus

Module-level state is intentional — the API is a single-process service;
the buffer must persist across requests. Use reset() in tests.
"""

from __future__ import annotations

import time
from collections import deque
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Rolling window configuration
# ---------------------------------------------------------------------------

_WINDOW_SECONDS: float = 1800.0

_MIN_SOLAR_RAD: float = 20.0
_NOISE_FLOOR: float = 0.0
_KC_MAX: float = 1.2

# ---------------------------------------------------------------------------
# CAELUS thresholds (Table 3, Ruiz-Arias & Gueymard 2023)
# ---------------------------------------------------------------------------

_CLOUDEN_MIN_KCS: float = 1.06
_CLOUDEN_MIN_KV: float = 0.20
_CLOUDEN_MIN_KVF: float = 0.20
_CLOUDLESS_MIN_KM: float = 0.6
_CLOUDLESS_MIN_KCS: float = 0.85
_CLOUDLESS_MAX_KCS: float = 1.15
_CLOUDLESS_MAX_KV: float = 0.03
_THINCLOUDS_MIN_KM: float = 0.5
_THINCLOUDS_MIN_KV: float = 0.03
_THINCLOUDS_MAX_KV: float = 0.08
_THICKCLOUDS_MAX_KM: float = 0.4
_THICKCLOUDS_MIN_KV: float = 0.04
_THICKCLOUDS_MAX_KV: float = 0.16
_OVERCAST_MAX_KM: float = 0.3
_OVERCAST_MAX_KV: float = 0.10

# SZA proxy: maxSolarRad > threshold approximates solar zenith angle < Ndeg.
# maxSolarRad is a clear-sky irradiance estimate — it drops to zero at sunrise/
# sunset and peaks at solar noon, tracking the same geometry as SZA without
# requiring ephemeris computation in this module.
_SZA80_MSR_PROXY: float = 100.0   # maxSolarRad > 100 ≈ SZA < 80°
_SZA75_MSR_PROXY: float = 200.0   # maxSolarRad > 200 ≈ SZA < 75°

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------


class MinuteRecord(NamedTuple):
    ts: float
    ghi: float
    max_solar_rad: float


# Tier 1: sub-minute accumulator — raw (ts, GHI, maxSolarRad) readings.
_minute_acc: list[tuple[float, float, float]] = []
_last_minute_ts: float = 0.0

# Tier 2: ring buffer of 1-minute averages, max 30 entries.
_ring: deque[MinuteRecord] = deque()

_was_daytime: bool = False

# Temporal coherence filter state.
_classification_history: deque[tuple[float, str]] = deque()  # (ts, label)
_last_stable_label: str | None = None

# Archive interval — set by configure() at startup; default matches weewx default.
_archive_interval: float = 300.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def update(
    radiation: float | None,
    max_solar_rad: float | None,
    timestamp: float | None = None,
) -> None:
    """Add a new reading to the rolling buffer.

    Silently skips the reading when neither radiation nor maxSolarRad
    reaches _MIN_SOLAR_RAD (night/twilight), or radiation is None/< 0.
    """
    if timestamp is None:
        timestamp = time.time()

    global _was_daytime, _last_minute_ts, _last_stable_label

    _rad = radiation if isinstance(radiation, (int, float)) else 0.0
    _msr = max_solar_rad if isinstance(max_solar_rad, (int, float)) else 0.0
    currently_daytime = max(_rad, _msr) >= _MIN_SOLAR_RAD

    if _was_daytime and not currently_daytime:
        _ring.clear()
        _minute_acc.clear()
        _last_minute_ts = 0.0
        _last_stable_label = None
        _classification_history.clear()

    _was_daytime = currently_daytime

    if not currently_daytime:
        return
    if radiation is None or radiation < _NOISE_FLOOR:
        return

    _maybe_flush_minute(timestamp)

    _minute_acc.append((timestamp, float(radiation), float(_msr)))
    _last_minute_ts = timestamp

    _trim_ring(timestamp)


def classify() -> str | None:
    """Classify sky condition from the ring buffer.

    Returns one of: "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy",
    "Cloudy", or None when insufficient data.
    """
    indices = _compute_indices()
    if indices is None:
        return _last_stable_label

    kcs, km, kv, kvf, latest_msr = indices
    raw_label = _classify_caelus(kcs, km, kv, kvf, latest_msr)

    now = _ring[-1].ts if _ring else time.time()
    return _apply_coherence_filter(raw_label, now)


def configure(archive_interval: int) -> None:
    """Set the archive interval for freshness thresholds.

    Called once at startup from __main__.py after load_station_metadata().
    The is_daytime() freshness threshold scales to 5× the archive interval
    so that a station with 60-second archives uses 300 s and a station with
    300-second archives uses 1500 s.
    """
    global _archive_interval  # noqa: PLW0603
    _archive_interval = float(archive_interval)


def is_daytime() -> bool:
    """Return True when the buffer has a recent daytime reading."""
    if not _ring and not _minute_acc:
        return False
    if _ring:
        last_ts = _ring[-1].ts
    else:
        last_ts = _minute_acc[-1][0]
    return (time.time() - last_ts) < _archive_interval * 5.0


def reset() -> None:
    """Clear all state. For test isolation only."""
    global _was_daytime, _last_minute_ts, _last_stable_label, _archive_interval
    _ring.clear()
    _minute_acc.clear()
    _last_minute_ts = 0.0
    _was_daytime = False
    _classification_history.clear()
    _last_stable_label = None
    _archive_interval = 300.0


def backfill(records: list[tuple[float, float, float]]) -> None:
    """Seed the ring buffer from archive records for immediate classification.

    Each record is (timestamp, radiation, maxSolarRad) from the weewx archive.
    Archive records are already averaged over the archive interval — each
    becomes one ring entry directly (no further binning needed).
    """
    if not records:
        return

    sorted_records = sorted(records, key=lambda r: r[0])
    if not sorted_records:
        return

    max_ts = sorted_records[-1][0]
    cutoff = max_ts - _WINDOW_SECONDS

    existing_ts = {entry.ts for entry in _ring}

    for ts, radiation, msr in sorted_records:
        if ts <= cutoff:
            continue
        if radiation is None:
            continue
        if radiation < 0:
            continue
        if radiation < _NOISE_FLOOR:
            continue
        if max(radiation, msr) < _MIN_SOLAR_RAD:
            continue
        if ts in existing_ts:
            continue
        _ring.append(MinuteRecord(ts=ts, ghi=float(radiation), max_solar_rad=float(msr)))
        existing_ts.add(ts)

    # Pre-classify so classify() returns a result immediately after backfill.
    # Archive data is pre-averaged — the coherence filter's stability requirement
    # doesn't apply to historical data.
    global _last_stable_label
    if len(_ring) >= 3:
        indices = _compute_indices()
        if indices is not None:
            kcs, km, kv, kvf, latest_msr = indices
            _last_stable_label = _classify_caelus(kcs, km, kv, kvf, latest_msr)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _maybe_flush_minute(timestamp: float) -> None:
    """Flush the sub-minute accumulator to the ring if a minute boundary passed."""
    if not _minute_acc:
        return

    new_minute = int(timestamp / 60) != int(_minute_acc[0][0] / 60)
    force_flush = len(_minute_acc) >= 12

    if new_minute or force_flush:
        n = len(_minute_acc)
        avg_ghi = sum(r[1] for r in _minute_acc) / n
        avg_msr = sum(r[2] for r in _minute_acc) / n
        bin_ts = _minute_acc[-1][0]
        _ring.append(MinuteRecord(ts=bin_ts, ghi=avg_ghi, max_solar_rad=avg_msr))
        _minute_acc.clear()


def _trim_ring(timestamp: float) -> None:
    """Remove ring entries older than the 30-minute window."""
    cutoff = timestamp - _WINDOW_SECONDS
    while _ring and _ring[0].ts < cutoff:
        _ring.popleft()


def _compute_indices() -> tuple[float, float, float, float, float] | None:
    """Compute (Kcs, Km, Kv, Kvf, latest_msr) from the ring buffer.

    Returns None when ring has < 3 entries (startup guard).
    """
    if len(_ring) < 3:
        return None

    latest = _ring[-1]

    # Kcs: instantaneous clear-sky index from the latest minute bin.
    if latest.max_solar_rad > 0:
        kcs = min(latest.ghi / latest.max_solar_rad, _KC_MAX)
        kcs = max(kcs, 0.0)
    else:
        kcs = 0.0

    # Km: mean normalized irradiance over the full ring.
    all_ghi = [r.ghi for r in _ring]
    all_msr = [r.max_solar_rad for r in _ring]
    mean_msr = sum(all_msr) / len(all_msr)
    if mean_msr > 0:
        km = max(sum(all_ghi) / len(all_ghi) / mean_msr, 0.0)
    else:
        km = 0.0

    # Kv: coarse variability (30-min window).
    mean_ghi = sum(all_ghi) / len(all_ghi)
    deviations = [g - mean_ghi for g in all_ghi]

    diff_abs_all: list[float] = []
    for i in range(1, len(_ring)):
        diff_abs_all.append(abs(deviations[i] - deviations[i - 1]))

    ring_list = list(_ring)
    # Use actual time span as denominator so mixed-resolution backfill data
    # (e.g., 5-minute archive intervals) produces correct Kv values.
    ring_span = max(ring_list[-1].ts - ring_list[0].ts, 60.0)
    kv = sum(diff_abs_all) / ring_span

    # Kvf: fine variability (10-min window, using 30-min deviation series).
    fine_cutoff = latest.ts - 600.0
    fine_indices = [i for i, r in enumerate(ring_list) if r.ts >= fine_cutoff]

    if len(fine_indices) < 2:
        kvf = 0.0
    else:
        first_fine_idx = fine_indices[0]
        fine_diff_abs = [
            diff_abs_all[i - 1]
            for i in fine_indices
            if i > 0 and i - 1 < len(diff_abs_all)
        ]
        fine_span = max(ring_list[-1].ts - ring_list[first_fine_idx].ts, 60.0)
        kvf = sum(fine_diff_abs) / fine_span if fine_diff_abs else 0.0

    return kcs, km, kv, kvf, latest.max_solar_rad


def _classify_caelus(
    kcs: float, km: float, kv: float, kvf: float, latest_msr: float,
) -> str:
    """Classify sky condition using the CAELUS decision tree.

    Returns one of: "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy",
    "Cloudy". First-match wins.
    """
    # CLOUD_ENHANCEMENT: sun visible but nearby clouds cause GHI > clear-sky.
    if (
        latest_msr > _SZA80_MSR_PROXY
        and kcs > _CLOUDEN_MIN_KCS
        and kv > _CLOUDEN_MIN_KV
        and kvf > _CLOUDEN_MIN_KVF
    ):
        return "Partly Cloudy"

    # CLOUDLESS: stable, high clear-sky index with low variability.
    # Thresholds relax slightly at high SZA (morning/evening low sun).
    if latest_msr > _SZA75_MSR_PROXY:
        cloudless = (
            km > _CLOUDLESS_MIN_KM
            and kcs > _CLOUDLESS_MIN_KCS
            and kcs < _CLOUDLESS_MAX_KCS
            and kv < _CLOUDLESS_MAX_KV
        )
    else:
        cloudless = (
            km > _CLOUDLESS_MIN_KM
            and kcs > 0.80
            and kcs < 1.20
            and kv < _CLOUDLESS_MAX_KV
        )
    if cloudless:
        return "Clear"

    # OVERCAST: uniformly low irradiance, low variability.
    if km < _OVERCAST_MAX_KM and kv < _OVERCAST_MAX_KV:
        return "Cloudy"

    # Remaining cases fall into the "cloudy zone" — classify by Km and Kv.

    # THIN_CLOUDS: moderate irradiance, moderate variability.
    if (
        km > _THINCLOUDS_MIN_KM
        and kv >= _THINCLOUDS_MIN_KV
        and kv < _THINCLOUDS_MAX_KV
    ):
        return "Mostly Clear"

    # THICK_CLOUDS: low irradiance, moderate variability.
    if (
        km < _THICKCLOUDS_MAX_KM
        and kv >= _THICKCLOUDS_MIN_KV
        and kv < _THICKCLOUDS_MAX_KV
    ):
        return "Mostly Cloudy"

    # SCATTER_CLOUDS: everything else — broken or variable cloud deck.
    return "Partly Cloudy"


def _apply_coherence_filter(raw_label: str, now: float) -> str | None:
    """Apply temporal coherence filter to prevent rapid label flicker.

    A raw label must persist for 15 consecutive minutes before becoming
    stable. On startup, 3 consecutive minutes suffice as a grace period.
    """
    global _last_stable_label

    _classification_history.append((now, raw_label))

    # Trim history to last 30 minutes.
    cutoff = now - _WINDOW_SECONDS
    while _classification_history and _classification_history[0][0] < cutoff:
        _classification_history.popleft()

    # Walk backwards through history counting consecutive matching minutes.
    consecutive_span = 0.0
    history_list = list(_classification_history)
    if not history_list:
        return _last_stable_label

    latest_label = history_list[-1][1]
    first_matching_ts = history_list[-1][0]
    for ts, label in reversed(history_list):
        if label == latest_label:
            first_matching_ts = ts
        else:
            break

    consecutive_span = history_list[-1][0] - first_matching_ts

    if consecutive_span >= 900.0:  # 15 minutes
        _last_stable_label = latest_label
    elif _last_stable_label is None and consecutive_span >= 180.0:  # 3-min startup grace
        _last_stable_label = latest_label

    return _last_stable_label
