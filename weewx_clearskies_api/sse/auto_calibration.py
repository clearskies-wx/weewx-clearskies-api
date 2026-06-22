"""Auto-calibration baseline module for haze detection (ADR-068).

Computes a station-specific clean-sky Kcs baseline from locally measured
clean-sky samples collected over a rolling 90-day (primary) or 180-day
(fallback) window.

The baseline replaces the fixed 0.90 Kcs value in haze_condition.py with a
90th–95th percentile (midpoint = 92.5th percentile) of qualifying clean-sky
Kcs samples.  This makes haze detection station-adaptive rather than using a
universal constant that may be too high or too low for a given location's
atmospheric conditions.

Clean-sky sample criteria (all must be satisfied):
  - PM2.5 < 12 µg/m³ and PM10 < 50 µg/m³ (EPA "Good" breakpoints)
  - Solar elevation > 10° (Kcs unreliable below this angle per ADR-068)
  - Sky classifier returns a clear-ish label (or None during startup)
  - No rain in the preceding 30 minutes

Baseline state transitions:
  - < 22 samples in 90d window → bootstrapping (haze uses fixed baseline)
  - 22+ samples in 90d window → calibrated (haze uses learned baseline)
  - 50+ samples → well-calibrated

Persistence: samples and baseline are saved to
/etc/weewx-clearskies/calibration.json every 10 minutes (atomic write via
tmp-rename).  On startup, load_persisted() restores the prior session's state
so the baseline survives API restarts without re-bootstrapping.

maxSolarRad computation (T6.3):
  compute_max_solar_rad() implements the Ryan-Stolzenbach clear-sky irradiance
  formula using Skyfield for solar position, matching the weewx wxformulas.py
  solar_rad_RS() computation.

Module-level state is intentional — the API is a single-process service.
Use reset() for test isolation.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from weewx_clearskies_api.sse import haze_condition, sky_condition
from weewx_clearskies_api.sse.enrichment import input_smoother

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WINDOW_DAYS_PRIMARY = 90        # primary rolling window
_WINDOW_DAYS_FALLBACK = 180      # fallback when primary has < 15 samples
_MIN_SAMPLES_ACTIVE = 22         # minimum to activate haze detection
_MIN_SAMPLES_FALLBACK = 15       # minimum in fallback window
_PERCENTILE_LOW = 90             # percentile range for baseline (low end)
_PERCENTILE_HIGH = 95            # percentile range for baseline (high end)
_PM25_CLEAN = 12.0               # EPA "Good" breakpoint µg/m³
_PM10_CLEAN = 50.0               # EPA "Good" breakpoint µg/m³
_MIN_ELEVATION = 10.0            # degrees — Kcs unreliable below this
_RAIN_HOLDOFF = 1800.0           # 30 minutes after rain (seconds)
_PERSIST_PATH = "/etc/weewx-clearskies/calibration.json"
_PERSIST_INTERVAL = 600.0        # save at most every 10 minutes

_WINDOW_PRIMARY_SECS: float = _WINDOW_DAYS_PRIMARY * 86400.0
_WINDOW_FALLBACK_SECS: float = _WINDOW_DAYS_FALLBACK * 86400.0

# ---------------------------------------------------------------------------
# Sky labels — accepted substrings for clean-sample collection.
# Labels containing these substrings are clear-ish enough to yield valid
# clean-sky Kcs samples.  Must NOT contain "Mostly Cloudy", "Cloudy",
# "Overcast", "Heavy Overcast".
# ---------------------------------------------------------------------------

_CLEAN_SKY_SUBSTRINGS: tuple[str, ...] = ("Clear", "Sunny")

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Each element: (unix_timestamp, kcs_value).
_samples: list[tuple[float, float]] = []

# Current learned baseline, or None while bootstrapping.
_current_baseline: float | None = None

# Unix timestamp of the last persist() write.
_last_persist_time: float = 0.0

# Rain tracking — same pattern as haze_condition.py.
_last_rain_time: float = 0.0
_was_raining: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure(
    *,
    percentile: float = 0.92,
    window_days: int = 90,
    min_samples: int = 22,
) -> None:
    """Apply operator-configurable calibration parameters.

    Called once at startup from __main__.py with values from api.conf
    [conditions] section.  Overwrites the module-level constants that
    drive baseline computation.

    Args:
        percentile:   Target percentile for clean-sky Kcs baseline.  A ±2.5pp
                      band is derived from this single value (e.g. 0.92 →
                      [89th, 94th] percentile range).  Must be in [0.90, 0.95].
        window_days:  Primary rolling window in days for sample collection.
                      Must be in [30, 365].  The fallback window stays at 180 d.
        min_samples:  Minimum clean-sky samples required to activate haze
                      detection.  Must be in [10, 100].
    """
    global _PERCENTILE_LOW, _PERCENTILE_HIGH, _MIN_SAMPLES_ACTIVE  # noqa: PLW0603
    global _WINDOW_DAYS_PRIMARY, _WINDOW_PRIMARY_SECS  # noqa: PLW0603
    # Map operator percentile (single value like 0.92) to a percentile range.
    # e.g. 0.92 → low = int(0.895 * 100) = 89, high = int(0.945 * 100) = 94
    _PERCENTILE_LOW = int((percentile - 0.025) * 100)
    _PERCENTILE_HIGH = int((percentile + 0.025) * 100)
    _MIN_SAMPLES_ACTIVE = min_samples
    _WINDOW_DAYS_PRIMARY = window_days
    _WINDOW_PRIMARY_SECS = window_days * 86400.0


def process_packet(packet: dict) -> None:  # type: ignore[type-arg]
    """Packet-tap processor: collect clean-sky Kcs samples and update baseline.

    Called on every loop packet (~5 seconds).  All clean-sample criteria must
    be satisfied simultaneously before a Kcs sample is appended.

    Args:
        packet: Loop packet dict (ignored for most fields — we read from the
                input_smoother and sky_condition module instead).
    """
    global _last_rain_time, _was_raining, _current_baseline  # noqa: PLW0603

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
    # Kcs is unreliable at low sun angles (Ryan-Stolzenbach underestimates
    # maxSolarRad by ~20% below 10°, inflating Kcs artificially).
    # ------------------------------------------------------------------
    solar_elevation = sky_condition.get_solar_elevation()
    if solar_elevation is None or solar_elevation <= _MIN_ELEVATION:
        return

    # ------------------------------------------------------------------
    # Gate 3: Clear-ish sky.
    # Only collect samples when the sky classifier reports clear conditions.
    # If classify() returns None (startup, insufficient data) skip — do not
    # collect samples when we cannot verify sky state.
    # ------------------------------------------------------------------
    sky_label = sky_condition.classify()
    if sky_label is None:
        return

    if not any(sub in sky_label for sub in _CLEAN_SKY_SUBSTRINGS):
        return

    # ------------------------------------------------------------------
    # Gate 4: Clean PM concentrations.
    # PM2.5 is the primary channel — must be present and below the EPA
    # "Good" breakpoint (12 µg/m³).  PM10 is checked when available but
    # is not required: many OpenAQ stations provide PM2.5 only.
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

    _samples.append((now, kcs))

    # Prune samples older than the fallback window (180 days).
    cutoff = now - _WINDOW_FALLBACK_SECS
    # _samples is appended in chronological order, so trim from the front.
    while _samples and _samples[0][0] < cutoff:
        _samples.pop(0)

    # ------------------------------------------------------------------
    # Recompute baseline and notify haze_condition if it changed.
    # ------------------------------------------------------------------
    new_baseline = compute_baseline()
    if new_baseline is not None and new_baseline != _current_baseline:
        _current_baseline = new_baseline
        haze_condition.set_baseline(new_baseline)
        logger.debug(
            "Auto-calibration baseline updated",
            extra={
                "baseline_kcs": round(new_baseline, 4),
                "sample_count": len(_samples),
            },
        )

    # ------------------------------------------------------------------
    # Periodic persistence.
    # ------------------------------------------------------------------
    if now - _last_persist_time >= _PERSIST_INTERVAL:
        persist()


def compute_baseline() -> float | None:
    """Compute the 90th–95th percentile midpoint (92.5th percentile) of clean-sky Kcs.

    Uses the primary 90-day window first.  Falls back to the 180-day window
    when the primary has fewer than _MIN_SAMPLES_ACTIVE samples (but requires
    at least _MIN_SAMPLES_FALLBACK samples in the fallback window).

    Returns:
        The baseline float, or None when insufficient samples are available.
    """
    now = time.time()

    # Primary window: 90 days.
    cutoff_primary = now - _WINDOW_PRIMARY_SECS
    primary_kcs = [kcs for ts, kcs in _samples if ts >= cutoff_primary]

    if len(primary_kcs) >= _MIN_SAMPLES_ACTIVE:
        return _percentile_midpoint(primary_kcs)

    # Fallback window: 180 days.
    cutoff_fallback = now - _WINDOW_FALLBACK_SECS
    fallback_kcs = [kcs for ts, kcs in _samples if ts >= cutoff_fallback]

    if len(fallback_kcs) >= _MIN_SAMPLES_FALLBACK:
        return _percentile_midpoint(fallback_kcs)

    return None


def get_calibration_state() -> dict:  # type: ignore[type-arg]
    """Return calibration state for admin UI and status reporting.

    Returns:
        Dict with keys:
          state:             "bootstrapping" | "calibrated" | "well-calibrated"
          sample_count_90d:  int — samples in the primary 90-day window
          sample_count_180d: int — samples in the fallback 180-day window
          baseline_kcs:      float | None — current learned baseline
          last_updated:      float | None — unix timestamp of last baseline change
    """
    now = time.time()
    cutoff_90 = now - _WINDOW_PRIMARY_SECS
    cutoff_180 = now - _WINDOW_FALLBACK_SECS

    count_90 = sum(1 for ts, _ in _samples if ts >= cutoff_90)
    count_180 = sum(1 for ts, _ in _samples if ts >= cutoff_180)

    if count_90 > 50:
        state = "well-calibrated"
    elif count_90 >= _MIN_SAMPLES_ACTIVE:
        state = "calibrated"
    else:
        state = "bootstrapping"

    return {
        "state": state,
        "sample_count_90d": count_90,
        "sample_count_180d": count_180,
        "baseline_kcs": _current_baseline,
        "last_updated": _last_persist_time if _last_persist_time > 0.0 else None,
    }


def load_persisted() -> None:
    """Load persisted calibration state from disk on startup.

    Reads /etc/weewx-clearskies/calibration.json and populates _samples and
    _current_baseline.  Prunes samples older than the 180-day fallback window.
    Calls haze_condition.set_baseline() if a baseline is present.

    On file-missing or parse error: logs a warning and starts fresh.
    """
    global _samples, _current_baseline  # noqa: PLW0603

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

    # Validate and restore samples.
    raw_samples = data.get("samples", [])
    if not isinstance(raw_samples, list):
        logger.warning(
            "Auto-calibration: persisted samples is not a list; starting fresh."
        )
        return

    now = time.time()
    cutoff = now - _WINDOW_FALLBACK_SECS
    loaded: list[tuple[float, float]] = []

    for item in raw_samples:
        try:
            ts_val, kcs_val = item
            ts = float(ts_val)
            kcs = float(kcs_val)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue  # prune stale samples
        loaded.append((ts, kcs))

    _samples = sorted(loaded, key=lambda x: x[0])

    # Restore baseline.
    raw_baseline = data.get("baseline_kcs")
    if raw_baseline is not None:
        try:
            _current_baseline = float(raw_baseline)
            haze_condition.set_baseline(_current_baseline)
            logger.info(
                "Auto-calibration: restored baseline %.4f from %s (%d samples kept).",
                _current_baseline,
                _PERSIST_PATH,
                len(_samples),
            )
        except (TypeError, ValueError):
            _current_baseline = None
    else:
        logger.info(
            "Auto-calibration: loaded %d samples from %s; no prior baseline.",
            len(_samples),
            _PERSIST_PATH,
        )


def persist() -> None:
    """Atomically save calibration state to disk.

    Writes to a .tmp file in the same directory, then replaces the target
    file atomically using os.replace().  If the directory does not exist or
    a write error occurs, logs a warning and continues — persistence failure
    is non-fatal.
    """
    global _last_persist_time  # noqa: PLW0603

    path = Path(_PERSIST_PATH)
    tmp_path = path.with_suffix(".json.tmp")

    data = {
        "samples": [[ts, kcs] for ts, kcs in _samples],
        "baseline_kcs": _current_baseline,
    }

    try:
        # Ensure directory exists.
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
        # Clean up orphaned tmp file if it was created.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def compute_max_solar_rad(
    lat: float,
    lon: float,
    altitude_m: float,
    unix_ts: float,
    atc: float = 0.80,
) -> float | None:
    """Compute clear-sky solar irradiance using the Ryan-Stolzenbach formula.

    Matches the weewx wxformulas.py solar_rad_RS() computation, using Skyfield
    for solar position (elevation and Earth-Sun distance) instead of PyEphem.

    Args:
        lat:        Station latitude in decimal degrees.
        lon:        Station longitude in decimal degrees.
        altitude_m: Station altitude in metres above sea level.
        unix_ts:    Unix timestamp for which to compute maxSolarRad.
        atc:        Atmospheric transmission coefficient (0.7–0.91).
                    Values outside this range are replaced with 0.8 per the
                    weewx formula.  Default: 0.80.

    Returns:
        Estimated clear-sky GHI in W/m², or None when the ephemeris is
        unavailable or a computation error occurs.

    Formula reference:
        Ryan & Stolzenbach (1972), MIT — "Environmental Heat Transfer."
        Matched verbatim from weewx wxformulas.py solar_rad_RS().
    """
    # Clamp atc to valid range — matches weewx behaviour exactly.
    if atc < 0.7 or atc > 0.91:
        atc = 0.8

    try:
        from skyfield.api import wgs84  # noqa: PLC0415

        from weewx_clearskies_api.services.almanac import get_ts_eph  # noqa: PLC0415

        ts, eph = get_ts_eph()

        # Build the Skyfield observer position.
        location = wgs84.latlon(lat, lon, elevation_m=altitude_m)  # type: ignore[call-arg]
        earth = eph["earth"]  # type: ignore[index]
        sun = eph["sun"]  # type: ignore[index]
        observer = earth + location  # type: ignore[operator]

        # Convert unix timestamp to a Skyfield Time object.
        dt = datetime.fromtimestamp(unix_ts, tz=UTC)
        t = ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)  # type: ignore[attr-defined]

        # Compute apparent solar position.
        apparent = observer.at(t).observe(sun).apparent()  # type: ignore[attr-defined]
        alt_obj, _az, dist_obj = apparent.altaz()  # type: ignore[attr-defined]

        elevation = float(alt_obj.degrees)  # type: ignore[attr-defined]
        distance_au = float(dist_obj.au)  # type: ignore[attr-defined]

    except RuntimeError:
        # Ephemeris not loaded — non-fatal, return None.
        logger.debug("compute_max_solar_rad: ephemeris not available")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("compute_max_solar_rad: Skyfield computation failed: %s", exc)
        return None

    # Ryan-Stolzenbach formula (verbatim from weewx wxformulas.py solar_rad_RS).
    nrel = 1367.0  # NREL solar constant, W/m²
    sinal = math.sin(math.radians(elevation))

    if sinal <= 0:
        return 0.0  # Sun below horizon

    z = altitude_m
    try:
        rm = (
            ((288.0 - 0.0065 * z) / 288.0) ** 5.256
            / (sinal + 0.15 * (elevation + 3.885) ** -1.253)
        )
        toa = nrel * sinal / (distance_au ** 2)
        sr = toa * atc ** rm
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        logger.debug("compute_max_solar_rad: formula computation failed: %s", exc)
        return None

    return sr


def reset() -> None:
    """Clear all module-level state.  For test isolation only."""
    global _samples, _current_baseline, _last_persist_time  # noqa: PLW0603
    global _last_rain_time, _was_raining  # noqa: PLW0603
    _samples = []
    _current_baseline = None
    _last_persist_time = 0.0
    _last_rain_time = 0.0
    _was_raining = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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

    # Fractional index.
    idx = (percentile / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    frac = idx - lo

    if hi >= n:
        return sorted_values[-1]

    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def _percentile_midpoint(kcs_values: list[float]) -> float:
    """Return the midpoint of the 90th and 95th percentile of the given Kcs values.

    Sorts the input in-place (ascending) and computes the 92.5th-percentile
    equivalent via linear interpolation.

    Args:
        kcs_values: List of Kcs float values (unordered).

    Returns:
        Midpoint of p90 and p95 as the station-specific clean-sky baseline.
    """
    sorted_vals = sorted(kcs_values)
    p90 = _percentile(sorted_vals, _PERCENTILE_LOW)
    p95 = _percentile(sorted_vals, _PERCENTILE_HIGH)
    return (p90 + p95) / 2.0
