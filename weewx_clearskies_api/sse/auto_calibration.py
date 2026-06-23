"""Auto-calibration baseline module for haze detection (ADR-068).

Computes station-specific clean-sky Kcs baselines using a monthly-normals
model: 12 independent per-month baselines, each derived from the 92nd
percentile of clean-sky samples collected over a 3-year rolling window.

Monthly normals replace the previous flat 90-day rolling window.  Each
calendar month accumulates its own sample pool.  The current month's
baseline is used when at least 30 samples exist for that month; otherwise
the flat fallback (all months pooled) is used.

Clean-sky sample criteria (all must be satisfied):
  - PM2.5 < 12 µg/m³ and PM10 < 50 µg/m³ (EPA "Good" breakpoints)
  - Solar elevation > 10° (Kcs unreliable below this angle per ADR-068)
  - Sky classifier returns a clear-ish label (no None during startup)
  - No rain in the preceding 30 minutes

Baseline state:
  - "no-data": zero samples collected, no flat fallback
  - "bootstrapping": samples exist but no month has >= 30 samples yet
  - "partial": 1-11 months have a per-month baseline
  - "fully-calibrated": all 12 months have a per-month baseline

Persistence: samples and baselines saved to
/etc/weewx-clearskies/calibration.json every 10 minutes (atomic write via
tmp-rename).  On startup, load_persisted() restores prior session state.
v1 flat-sample format is migrated to v2 monthly format automatically.

Module-level state is intentional — the API is a single-process service.
Use reset() for test isolation.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from weewx_clearskies_api.sse import haze_condition, sky_condition
from weewx_clearskies_api.sse.enrichment import input_smoother

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (fixed — not operator-configurable)
# ---------------------------------------------------------------------------

_WINDOW_YEARS = 3
_WINDOW_SECS = _WINDOW_YEARS * 365.25 * 86400.0   # ~94,672,800 seconds
_MIN_SAMPLES_PER_MONTH = 30
_PERCENTILE = 92
_DRIFT_THRESHOLD = 0.05
_DRIFT_SAMPLE_COUNT = 10

# Clean-sky sample criteria (unchanged from v1)
_PM25_CLEAN = 12.0
_PM10_CLEAN = 50.0
_MIN_ELEVATION = 10.0
_RAIN_HOLDOFF = 1800.0
_PERSIST_PATH = "/etc/weewx-clearskies/calibration.json"
_PERSIST_INTERVAL = 600.0

_CLEAN_SKY_SUBSTRINGS: tuple[str, ...] = ("Clear", "Sunny")

_MONTH_NAMES: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Per-month sample lists.  Each element: (unix_timestamp, kcs_value).
_monthly_samples: dict[int, list[tuple[float, float]]] = {m: [] for m in range(1, 13)}

# Per-month baselines.  None until >= _MIN_SAMPLES_PER_MONTH collected.
_monthly_baselines: dict[int, float | None] = {m: None for m in range(1, 13)}

# Flat baseline: 92nd percentile of all months pooled.  Used when the
# current month has no per-month baseline yet.
_flat_baseline: float | None = None

# Station type recorded at load time (for drift detection).
_station_type_at_load: str | None = None

# IANA timezone name for correct month binning (e.g. "America/New_York").
_timezone_name: str = "UTC"

# Whether the station has a radiation sensor.  Defaults True; re-evaluated
# in process_packet() if sky_condition reports Kcs data while this is False.
_has_radiation: bool = True

# Persistence and rain tracking.
_last_persist_time: float = 0.0
_last_rain_time: float = 0.0
_was_raining: bool = False

# Selected OpenAQ sensor info (set after successful bootstrap or operator override).
# Keys: sensor_id, name, distance_km, lat, lon.
_openaq_sensor: dict | None = None


# ---------------------------------------------------------------------------
# Public API — setters
# ---------------------------------------------------------------------------


def set_timezone(tz_name: str) -> None:
    """Set the station timezone used for month binning."""
    global _timezone_name  # noqa: PLW0603
    _timezone_name = tz_name


def set_station_type(station_type: str | None) -> None:
    """Record the station hardware type at load time."""
    global _station_type_at_load  # noqa: PLW0603
    _station_type_at_load = station_type


def set_has_radiation(has: bool) -> None:
    """Set whether the station has a pyranometer."""
    global _has_radiation  # noqa: PLW0603
    _has_radiation = has


def set_openaq_sensor(info: dict | None) -> None:
    """Store the selected OpenAQ sensor info.

    Args:
        info: Dict with keys sensor_id, name, distance_km, lat, lon; or None
              to clear the stored sensor.
    """
    global _openaq_sensor  # noqa: PLW0603
    _openaq_sensor = info


def get_openaq_sensor() -> dict | None:
    """Return the stored OpenAQ sensor info, or None if no sensor selected."""
    return _openaq_sensor


def check_station_type_change(current_type: str | None) -> bool:
    """Return True if hardware type changed since load time.

    Both values must be non-None for a change to be detected.  Logs a
    WARNING when a change is found (station type affects Kcs calibration).

    Args:
        current_type: Hardware type string from current StationInfo.

    Returns:
        True if station type changed; False otherwise.
    """
    if _station_type_at_load is not None and current_type is not None:
        if _station_type_at_load != current_type:
            logger.warning(
                "Auto-calibration: station type changed from %r to %r. "
                "Existing baselines may not be valid for the new hardware.",
                _station_type_at_load,
                current_type,
            )
            return True
    return False


# ---------------------------------------------------------------------------
# Public API — sample collection
# ---------------------------------------------------------------------------


def process_packet(packet: dict) -> None:  # type: ignore[type-arg]
    """Packet-tap processor: collect clean-sky Kcs samples and update baselines.

    Called on every loop packet (~5 seconds).  All clean-sample criteria must
    be satisfied simultaneously before a Kcs sample is appended.

    Args:
        packet: Loop packet dict (unused directly — we read from
                input_smoother and sky_condition instead).
    """
    global _last_rain_time, _was_raining, _has_radiation  # noqa: PLW0603

    # ------------------------------------------------------------------
    # Radiation sensor gate.
    # If set_has_radiation(False) was called at startup, re-evaluate by
    # checking whether sky_condition now has Kcs data (sensor was added
    # without a restart).  Only return early if still no data available.
    # ------------------------------------------------------------------
    if not _has_radiation:
        if sky_condition.get_current_kcs() is not None:
            _has_radiation = True
            logger.info(
                "Auto-calibration: radiation sensor detected, enabling sample collection"
            )
        else:
            return

    now = time.time()

    # ------------------------------------------------------------------
    # Gate 1: Rain holdoff.
    # Rain scavenges aerosols — 30 min post-rain must pass before we trust
    # Kcs as representative of a clean atmosphere.
    # ------------------------------------------------------------------
    rain_rate = input_smoother.get_smoothed("rainRate")
    currently_raining = rain_rate is not None and rain_rate > 0.0

    if currently_raining:
        _last_rain_time = now
        _was_raining = True
        return

    if _was_raining and not currently_raining:
        _last_rain_time = now
        _was_raining = False

    if (now - _last_rain_time) < _RAIN_HOLDOFF:
        return

    # ------------------------------------------------------------------
    # Gate 2: Solar elevation.
    # Kcs is unreliable at low sun angles.
    # ------------------------------------------------------------------
    solar_elevation = sky_condition.get_solar_elevation()
    if solar_elevation is None or solar_elevation <= _MIN_ELEVATION:
        return

    # ------------------------------------------------------------------
    # Gate 3: Clear-ish sky.
    # Only collect samples when the sky classifier reports clear conditions.
    # Skip when classify() returns None (startup) — cannot verify sky state.
    # ------------------------------------------------------------------
    sky_label = sky_condition.classify()
    if sky_label is None:
        return

    if not any(sub in sky_label for sub in _CLEAN_SKY_SUBSTRINGS):
        return

    # ------------------------------------------------------------------
    # Gate 4: Clean PM concentrations.
    # PM2.5 must be present and below EPA "Good" breakpoint (12 µg/m³).
    # PM10 checked when available; not required (many stations PM2.5 only).
    # ------------------------------------------------------------------
    pm25 = input_smoother.get_smoothed("pollutantPM25")
    pm10 = input_smoother.get_smoothed("pollutantPM10")

    if pm25 is None:
        return

    if pm25 >= _PM25_CLEAN:
        return

    if pm10 is not None and pm10 >= _PM10_CLEAN:
        return

    # ------------------------------------------------------------------
    # All criteria met — collect Kcs sample.
    # ------------------------------------------------------------------
    kcs = sky_condition.get_current_kcs()
    if kcs is None:
        return

    # Bin sample into local calendar month.
    local_month = datetime.fromtimestamp(now, tz=ZoneInfo(_timezone_name)).month
    _monthly_samples[local_month].append((now, kcs))

    # Prune samples > 3 years old from ALL 12 months.
    cutoff = now - _WINDOW_SECS
    for m in range(1, 13):
        while _monthly_samples[m] and _monthly_samples[m][0][0] < cutoff:
            _monthly_samples[m].pop(0)

    # Recompute this month's baseline.
    prev_baseline = _monthly_baselines.get(local_month)
    _monthly_baselines[local_month] = compute_monthly_baseline(local_month)

    # Recompute flat fallback baseline.
    _flat_baseline_update()

    # Notify haze_condition if the effective baseline changed.
    current = get_current_baseline()
    if current is not None:
        haze_condition.set_baseline(current)

    new_monthly = _monthly_baselines.get(local_month)
    if new_monthly != prev_baseline and new_monthly is not None:
        logger.debug(
            "Auto-calibration: month %d (%s) baseline updated to %.4f "
            "(%d samples in window)",
            local_month,
            _MONTH_NAMES[local_month - 1],
            new_monthly,
            len(_monthly_samples[local_month]),
        )

    # Periodic persistence.
    if now - _last_persist_time >= _PERSIST_INTERVAL:
        persist()


# ---------------------------------------------------------------------------
# Public API — baseline queries
# ---------------------------------------------------------------------------


def compute_monthly_baseline(month: int) -> float | None:
    """Compute the 92nd-percentile Kcs baseline for one calendar month.

    Args:
        month: Calendar month (1–12).

    Returns:
        Baseline float, or None when fewer than 30 samples exist for the month.
    """
    samples = _monthly_samples.get(month, [])
    if len(samples) < _MIN_SAMPLES_PER_MONTH:
        return None
    sorted_kcs = sorted(kcs for _, kcs in samples)
    return _percentile(sorted_kcs, _PERCENTILE)


def get_current_baseline() -> float | None:
    """Return the most applicable baseline for right now.

    Prefers the per-month baseline for the current local calendar month.
    Falls back to the flat (all-month-pooled) baseline when the current
    month has fewer than _MIN_SAMPLES_PER_MONTH samples.

    Returns:
        Baseline float, or None when no data is available at all.
    """
    local_month = datetime.fromtimestamp(time.time(), tz=ZoneInfo(_timezone_name)).month
    monthly = _monthly_baselines.get(local_month)
    if monthly is not None:
        return monthly
    return _flat_baseline


def get_calibration_state() -> dict:  # type: ignore[type-arg]
    """Return full calibration state for admin UI and status reporting.

    Returns:
        Dict with keys:
          months_calibrated: int — count of months with a per-month baseline
          per_month:         list of 12 dicts (month, name, sample_count,
                             baseline_kcs, is_calibrated)
          flat_baseline:     float | None — all-month-pooled fallback baseline
          overall_state:     "no-data" | "bootstrapping" | "partial" |
                             "fully-calibrated"
          drift_warnings:    list of drift warning dicts (may be empty)
          station_type:      str | None — hardware type recorded at load time
    """
    calibrated_count = sum(
        1 for m in range(1, 13) if _monthly_baselines[m] is not None
    )
    total_samples = sum(len(_monthly_samples[m]) for m in range(1, 13))

    if calibrated_count == 12:
        state = "fully-calibrated"
    elif calibrated_count > 0:
        state = "partial"
    elif total_samples > 0:
        state = "bootstrapping"
    else:
        state = "no-data"

    return {
        "months_calibrated": calibrated_count,
        "per_month": [
            {
                "month": m,
                "name": _MONTH_NAMES[m - 1],
                "sample_count": len(_monthly_samples[m]),
                "baseline_kcs": _monthly_baselines[m],
                "is_calibrated": _monthly_baselines[m] is not None,
            }
            for m in range(1, 13)
        ],
        "flat_baseline": _flat_baseline,
        "overall_state": state,
        "drift_warnings": [
            w for m in range(1, 13) if (w := _check_drift(m)) is not None
        ],
        "station_type": _station_type_at_load,
        "openaq_sensor": _openaq_sensor,
    }


# ---------------------------------------------------------------------------
# Public API — persistence
# ---------------------------------------------------------------------------


def load_persisted() -> None:
    """Load persisted calibration state from disk on startup.

    Reads /etc/weewx-clearskies/calibration.json and populates module state.
    Handles v1 (flat samples list) and v2 (monthly samples) formats.

    v1 migration: flat samples list is distributed into months by timestamp.
    After migration the v2 format is written immediately.

    On file-missing or parse error: logs a warning and starts fresh.
    """
    global _monthly_samples, _monthly_baselines, _flat_baseline  # noqa: PLW0603
    global _station_type_at_load, _openaq_sensor  # noqa: PLW0603

    path = Path(_PERSIST_PATH)
    if not path.exists():
        logger.info(
            "Auto-calibration: no persisted state found at %s; starting fresh.",
            _PERSIST_PATH,
        )
        return

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Auto-calibration: failed to load persisted state from %s: %s; "
            "starting fresh.",
            _PERSIST_PATH,
            exc,
        )
        return

    version = data.get("version")

    if version == 2:
        # ------------------------------------------------------------------
        # v2 format: per-month samples and baselines.
        # ------------------------------------------------------------------
        raw_monthly = data.get("monthly_samples", {})
        for m in range(1, 13):
            key = str(m)
            raw_list = raw_monthly.get(key, [])
            loaded: list[tuple[float, float]] = []
            for item in raw_list:
                try:
                    ts, kcs = float(item[0]), float(item[1])
                    loaded.append((ts, kcs))
                except (TypeError, ValueError, IndexError):
                    continue
            _monthly_samples[m] = sorted(loaded, key=lambda x: x[0])

        raw_baselines = data.get("monthly_baselines", {})
        for m in range(1, 13):
            key = str(m)
            raw_b = raw_baselines.get(key)
            if raw_b is not None:
                try:
                    _monthly_baselines[m] = float(raw_b)
                except (TypeError, ValueError):
                    _monthly_baselines[m] = None
            else:
                _monthly_baselines[m] = None

        raw_flat = data.get("flat_baseline")
        if raw_flat is not None:
            try:
                _flat_baseline = float(raw_flat)
            except (TypeError, ValueError):
                _flat_baseline = None

        raw_st = data.get("station_type")
        if raw_st is not None:
            _station_type_at_load = str(raw_st)

        # Load persisted OpenAQ sensor info (added in Phase 9).
        # Old calibration.json files without these keys leave _openaq_sensor = None.
        raw_sensor_id = data.get("openaq_sensor_id")
        if raw_sensor_id is not None:
            try:
                _openaq_sensor = {
                    "sensor_id": int(raw_sensor_id),
                    "name": str(data.get("openaq_sensor_name", "")),
                    "distance_km": float(data.get("openaq_sensor_distance_km", 0)),
                    "lat": float(data.get("openaq_sensor_lat", 0)),
                    "lon": float(data.get("openaq_sensor_lon", 0)),
                }
            except (TypeError, ValueError):
                _openaq_sensor = None

    else:
        # ------------------------------------------------------------------
        # v1 format: flat list of [ts, kcs] pairs.  Migrate to monthly.
        # ------------------------------------------------------------------
        raw_samples = data.get("samples", [])
        if not isinstance(raw_samples, list):
            logger.warning(
                "Auto-calibration: persisted samples is not a list; starting fresh."
            )
            return

        migrated_count = 0
        for item in raw_samples:
            try:
                ts, kcs = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            local_month = datetime.fromtimestamp(
                ts, tz=ZoneInfo(_timezone_name)
            ).month
            _monthly_samples[local_month].append((ts, kcs))
            migrated_count += 1

        for m in range(1, 13):
            _monthly_samples[m].sort(key=lambda x: x[0])

        logger.info(
            "Auto-calibration: migrated v1 data to v2 format "
            "(%d samples distributed across months).",
            migrated_count,
        )

    # ------------------------------------------------------------------
    # Post-load: prune, recompute, notify.
    # ------------------------------------------------------------------
    now = time.time()
    cutoff = now - _WINDOW_SECS
    for m in range(1, 13):
        while _monthly_samples[m] and _monthly_samples[m][0][0] < cutoff:
            _monthly_samples[m].pop(0)

    for m in range(1, 13):
        _monthly_baselines[m] = compute_monthly_baseline(m)

    _flat_baseline_update()

    current = get_current_baseline()
    if current is not None:
        haze_condition.set_baseline(current)
        logger.info(
            "Auto-calibration: restored baseline %.4f from %s.",
            current,
            _PERSIST_PATH,
        )
    else:
        total = sum(len(_monthly_samples[m]) for m in range(1, 13))
        logger.info(
            "Auto-calibration: loaded %d samples from %s; no baseline yet.",
            total,
            _PERSIST_PATH,
        )

    # If v1 was migrated, write v2 immediately.
    if version != 2:
        persist()


def persist() -> None:
    """Atomically save calibration state to disk in v2 format.

    Writes to a .tmp file then replaces the target atomically via os.replace().
    Persistence failure is non-fatal: logs a warning and continues.
    """
    global _last_persist_time  # noqa: PLW0603

    path = Path(_PERSIST_PATH)
    tmp_path = path.with_suffix(".json.tmp")

    data: dict = {
        "version": 2,
        "monthly_samples": {
            str(m): [[ts, kcs] for ts, kcs in _monthly_samples[m]]
            for m in range(1, 13)
        },
        "monthly_baselines": {
            str(m): _monthly_baselines[m]
            for m in range(1, 13)
        },
        "flat_baseline": _flat_baseline,
        "station_type": _station_type_at_load,
    }
    if _openaq_sensor:
        data["openaq_sensor_id"] = _openaq_sensor.get("sensor_id")
        data["openaq_sensor_name"] = _openaq_sensor.get("name")
        data["openaq_sensor_distance_km"] = _openaq_sensor.get("distance_km")
        data["openaq_sensor_lat"] = _openaq_sensor.get("lat")
        data["openaq_sensor_lon"] = _openaq_sensor.get("lon")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        os.replace(str(tmp_path), str(path))
        _last_persist_time = time.time()
    except OSError as exc:
        logger.warning(
            "Auto-calibration: failed to persist state to %s: %s",
            _PERSIST_PATH,
            exc,
        )
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def reset() -> None:
    """Clear all module-level state.  For test isolation only."""
    global _monthly_samples, _monthly_baselines, _flat_baseline  # noqa: PLW0603
    global _station_type_at_load, _has_radiation, _timezone_name  # noqa: PLW0603
    global _last_persist_time, _last_rain_time, _was_raining  # noqa: PLW0603
    global _openaq_sensor  # noqa: PLW0603
    _monthly_samples = {m: [] for m in range(1, 13)}
    _monthly_baselines = {m: None for m in range(1, 13)}
    _flat_baseline = None
    _station_type_at_load = None
    _has_radiation = True
    _timezone_name = "UTC"
    _last_persist_time = 0.0
    _last_rain_time = 0.0
    _was_raining = False
    _openaq_sensor = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _flat_baseline_update() -> None:
    """Recompute _flat_baseline from all months' samples pooled together."""
    global _flat_baseline  # noqa: PLW0603
    _flat_baseline = _compute_flat_baseline()


def _compute_flat_baseline() -> float | None:
    """Pool all months' samples and return the 92nd-percentile Kcs.

    Returns None when fewer than 30 total samples exist across all months.
    """
    all_kcs = sorted(kcs for m in range(1, 13) for _, kcs in _monthly_samples[m])
    if len(all_kcs) < _MIN_SAMPLES_PER_MONTH:
        return None
    return _percentile(all_kcs, _PERCENTILE)


def _check_drift(month: int) -> dict | None:  # type: ignore[type-arg]
    """Check whether recent real-time samples diverge from the monthly baseline.

    Only triggers on real-time samples added AFTER the baseline was last
    computed — not on bootstrap samples that were used to compute the baseline.
    Uses a relative threshold (15% of baseline) so it scales with the value.

    Args:
        month: Calendar month (1–12).

    Returns:
        Warning dict with keys month/baseline/recent_mean/divergence,
        or None when no drift is detected or not enough recent data.
    """
    samples = _monthly_samples.get(month, [])
    baseline = _monthly_baselines.get(month)
    if baseline is None or baseline <= 0 or len(samples) < _DRIFT_SAMPLE_COUNT * 2:
        return None
    recent = [kcs for _, kcs in samples[-_DRIFT_SAMPLE_COUNT:]]
    recent_mean = sum(recent) / len(recent)
    divergence = abs(recent_mean - baseline)
    relative_divergence = divergence / baseline
    if relative_divergence > 0.15:
        return {
            "month": month,
            "baseline": round(baseline, 4),
            "recent_mean": round(recent_mean, 4),
            "divergence": round(divergence, 4),
        }
    return None


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Compute the k-th percentile of a sorted list using linear interpolation.

    Standard interpolation: for N values sorted ascending, the k-th percentile
    is at fractional index i = (k / 100) * (N - 1).  Linear interpolation
    is used between adjacent values.

    Args:
        sorted_values: List of floats sorted in ascending order.
        percentile:    Target percentile in the range [0, 100].

    Returns:
        Interpolated percentile value.
    """
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]

    idx = (percentile / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    frac = idx - lo

    if hi >= n:
        return sorted_values[-1]

    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])
