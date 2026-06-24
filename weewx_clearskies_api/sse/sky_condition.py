"""Sky condition classification from solar radiation (ADR-073).

Kv-first decision tree in the Duchon & O'Malley (1999) tradition —
variability-primary, clearness-secondary — using CAELUS-derived indices
(Ruiz-Arias & Gueymard 2023) at 1-minute resolution.

Four indices computed from a 30-minute rolling window of 1-minute
averaged GHI data:
  - Kcs: instantaneous clear-sky index (GHI / maxSolarRad)
  - Km:  mean normalized irradiance over the window
  - Kv:  coarse variability index (30-min) — cumulative absolute
         first-derivative of clear-sky-detrended GHI
  - Kvf: fine variability index (10-min) — same as Kv, shorter window

Primary axis: asymmetric Kv/Kvf gate (uniform vs. variable sky).
Uniform requires both the coarse 30-min window (Kv) and the fine
10-min window (Kvf) below 0.05 — smooth GHI in both time scales.
Variable triggers if either window exceeds 0.05 — cloud-edge
transitions at any scale push the sky into the variable branch.
Km then distinguishes coverage degree within each branch.

Seven display labels:
  Uniform branch:  "Clear", "Overcast", "Heavy Overcast"
  Variable branch: "Mostly Clear", "Partly Cloudy", "Mostly Cloudy", "Cloudy"
  Cloud enhancement: "Partly Cloudy"

Temporal coherence filter: a new classification must persist for 5
consecutive minutes before replacing the previous stable label.
Startup grace period is 2 minutes. Prevents rapid flicker at class
boundaries.

Data flow: 5-second LOOP packets → 1-minute bins → 30-minute ring buffer
→ index computation → Kv-first decision tree → temporal coherence filter.

Startup backfill: archive records can seed the ring buffer for immediate
(if coarser) classification on API restart.

Index computation from CAELUS (classification tree is ours):
  - maxSolarRad used as clear-sky reference (CAELUS uses ghicda)
  - Solar zenith angle computed via Skyfield (station coords from configure())
  - GHI mirroring adapted from CAELUS mirror_ghi_with_pandas() — synthetic
    pre-sunrise entries extend the Km baseline at sunrise using cos(zenith)
    interpolation.  Only affects Km; Kv/Kvf use real ring data only.
  - SZA < 85° guard: classify() returns _last_stable_label when solar
    elevation < 5°, preventing classification at low sun angles.
  - Trailing window instead of centered (necessary for real-time)
  - Streaming temporal coherence filter instead of batch patch cleaning
  - Kv/Kvf detrended by clear-sky model (CAELUS relies on centered windows
    to suppress the solar geometry signal; our trailing window requires
    explicit detrending — subtract the predicted maxSolarRad delta from
    the observed GHI delta before accumulating).  This is standard practice
    in solar variability research: Stein et al. 2012 (Sandia Variability
    Index, SAND2012-3464C) and Coimbra et al. 2013 (clear-sky index
    stationarity) establish that dividing or detrending by a clear-sky
    model isolates cloud-induced variability from deterministic solar
    geometry changes.

Reference: Ruiz-Arias & Gueymard (2023), Solar Energy 263, 111895.
CAELUS source: github.com/jararias/caelus

Module-level state is intentional — the API is a single-process service;
the buffer must persist across requests. Use reset() in tests.
"""

from __future__ import annotations

import bisect
import math
import time
from collections import deque
from datetime import UTC, datetime
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Rolling window configuration
# ---------------------------------------------------------------------------

_WINDOW_SECONDS: float = 1800.0

_MIN_SOLAR_RAD: float = 20.0
_NOISE_FLOOR: float = 0.0
_KC_MAX: float = 1.2

# ---------------------------------------------------------------------------
# Kv-first classification thresholds (ADR-073)
# ---------------------------------------------------------------------------

_CLOUDEN_MIN_KCS: float = 1.06
_CLOUDEN_MIN_KV: float = 0.20
_CLOUDEN_MIN_KVF: float = 0.20

# Primary axis: Kv uniform/variable boundary
_KV_UNIFORM: float = 0.05

# Uniform branch: clear vs. overcast vs. heavy overcast
_UNIFORM_CLEAR_MIN_KM: float = 0.85
_UNIFORM_CLEAR_MIN_KCS: float = 0.80
_UNIFORM_HEAVY_MAX_KM: float = 0.35

# Variable branch: Km thresholds for coverage degree
_VARIABLE_CLEAR_MIN_KM: float = 0.85
_VARIABLE_PARTLY_MIN_KM: float = 0.60
_VARIABLE_MOSTLY_MIN_KM: float = 0.40

# SZA proxy: maxSolarRad > threshold approximates solar zenith angle < Ndeg.
# maxSolarRad is a clear-sky irradiance estimate — it drops to zero at sunrise/
# sunset and peaks at solar noon, tracking the same geometry as SZA without
# requiring ephemeris computation in this module.
_SZA80_MSR_PROXY: float = 100.0   # maxSolarRad > 100 ≈ SZA < 80°

# SZA guard threshold (degrees elevation). When solar elevation < this value
# (SZA > 85°), classify() returns the last stable label instead of classifying.
_SZA_GUARD_ELEVATION: float = 5.0

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

# Station coordinates and cached Skyfield observer — set by configure().
# None until configure() is called with lat/lon/altitude.
_station_lat: float | None = None
_station_lon: float | None = None
_station_alt: float | None = None
_skyfield_observer: object | None = None  # skyfield VectorSum (earth + location)


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

    Returns one of: "Clear", "Mostly Clear", "Partly Cloudy",
    "Mostly Cloudy", "Cloudy", "Overcast", "Heavy Overcast",
    or None when insufficient data.

    When solar elevation < 5° (SZA > 85°) and station coordinates are
    configured, returns _last_stable_label without classifying.
    """
    # SZA guard: skip classification when sun is too low on the horizon.
    # Uses the cached Skyfield observer built at configure() time.
    if _skyfield_observer is not None:
        now_ts = _ring[-1].ts if _ring else time.time()
        elevation = _compute_solar_elevation(now_ts)
        if elevation is not None and elevation < _SZA_GUARD_ELEVATION:
            return _last_stable_label

    indices = _compute_indices()
    if indices is None:
        return _last_stable_label

    kcs, km, kmf, kv, kvf, latest_msr = indices
    raw_label = _classify_sky(kcs, km, kmf, kv, kvf, latest_msr)

    # TEMPORARY DEBUG — remove after threshold tuning
    import logging as _dbg_logging  # noqa: PLC0415
    _dbg_logging.getLogger("sky_condition").info(
        "ring=%d Kcs=%.4f Km=%.4f Kmf=%.4f Kv=%.4f Kvf=%.4f MSR=%.1f raw=%s stable=%s",
        len(_ring), kcs, km, kmf, kv, kvf, latest_msr, raw_label, _last_stable_label,
    )

    now = _ring[-1].ts if _ring else time.time()
    return _apply_coherence_filter(raw_label, now)


def configure(
    archive_interval: int,
    latitude: float | None = None,
    longitude: float | None = None,
    altitude: float | None = None,
) -> None:
    """Set the archive interval and optional station coordinates.

    Called once at startup from __main__.py after load_station_metadata().
    The is_daytime() freshness threshold scales to 5× the archive interval
    so that a station with 60-second archives uses 300 s and a station with
    300-second archives uses 1500 s.

    When latitude, longitude, and altitude are all provided, pre-builds the
    Skyfield observer position used for GHI mirroring and the SZA guard.
    If any coordinate is None, mirroring and the SZA guard are disabled.
    """
    global _archive_interval  # noqa: PLW0603
    global _station_lat, _station_lon, _station_alt, _skyfield_observer  # noqa: PLW0603
    _archive_interval = float(archive_interval)

    if latitude is not None and longitude is not None and altitude is not None:
        _station_lat = float(latitude)
        _station_lon = float(longitude)
        _station_alt = float(altitude)
        _skyfield_observer = _build_skyfield_observer(
            _station_lat, _station_lon, _station_alt
        )
    else:
        _station_lat = None
        _station_lon = None
        _station_alt = None
        _skyfield_observer = None


def get_solar_elevation() -> float | None:
    """Return current solar elevation in degrees, or None if unavailable."""
    if not _ring:
        return None
    return _compute_solar_elevation(_ring[-1].ts)


def get_current_kcs() -> float | None:
    """Return the current Kcs (GHI / maxSolarRad), or None if unavailable."""
    indices = _compute_indices()
    if indices is None:
        return None
    return indices[0]


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
    global _station_lat, _station_lon, _station_alt, _skyfield_observer  # noqa: PLW0603
    _ring.clear()
    _minute_acc.clear()
    _last_minute_ts = 0.0
    _was_daytime = False
    _classification_history.clear()
    _last_stable_label = None
    _archive_interval = 300.0
    _station_lat = None
    _station_lon = None
    _station_alt = None
    _skyfield_observer = None


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
            _last_stable_label = _classify_sky(kcs, km, kv, kvf, latest_msr)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_skyfield_observer(lat: float, lon: float, alt_m: float) -> object | None:
    """Build and return a Skyfield observer vector for the given coordinates.

    Returns None if the ephemeris is not available (e.g., during unit tests
    that do not load de421.bsp).
    """
    try:
        from weewx_clearskies_api.services.almanac import get_ts_eph  # noqa: PLC0415
        from skyfield.api import wgs84  # noqa: PLC0415

        ts, eph = get_ts_eph()
        location = wgs84.latlon(lat, lon, elevation_m=alt_m)  # type: ignore[call-arg]
        earth = eph["earth"]  # type: ignore[index]
        sun = eph["sun"]  # type: ignore[index]
        observer = earth + location  # type: ignore[operator]
        # Store sun reference alongside observer as a 2-tuple for _compute_solar_elevation.
        return (observer, sun, ts)
    except Exception:  # noqa: BLE001
        return None


def _compute_solar_elevation(unix_ts: float) -> float | None:
    """Return solar elevation in degrees for the cached observer at unix_ts.

    Returns None if the observer is not built or ephemeris computation fails.
    cos(zenith) = sin(elevation); positive elevation means sun is above horizon.
    """
    if _skyfield_observer is None:
        return None
    try:
        observer, sun, ts = _skyfield_observer  # type: ignore[misc]
        dt = datetime.fromtimestamp(unix_ts, tz=UTC)
        t = ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)  # type: ignore[attr-defined]
        apparent = observer.at(t).observe(sun).apparent()  # type: ignore[attr-defined]
        alt_obj, _az, _dist = apparent.altaz()  # type: ignore[attr-defined]
        return float(alt_obj.degrees)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def _cos_zenith_for_ts(unix_ts: float) -> float | None:
    """Return cos(zenith) = sin(elevation) for a given Unix timestamp.

    Returns None when the observer is unavailable.
    """
    elev = _compute_solar_elevation(unix_ts)
    if elev is None:
        return None
    return math.sin(math.radians(elev))


def _interp_linear(xs: list[float], ys: list[float], x: float) -> float | None:
    """Linear interpolation at x from sorted (xs, ys) pairs.

    Returns None when xs is empty or x is outside the range (no extrapolation).
    xs must be sorted ascending.
    """
    if not xs:
        return None
    if x <= xs[0]:
        return None
    if x >= xs[-1]:
        return None
    idx = bisect.bisect_right(xs, x) - 1
    x0, x1 = xs[idx], xs[idx + 1]
    y0, y1 = ys[idx], ys[idx + 1]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _mirror_for_km(
    ring_entries: list[MinuteRecord],
) -> list[tuple[float, float]]:
    """Generate (ghi, max_solar_rad) pairs for Km computation with GHI mirroring.

    At sunrise, the 30-minute trailing window contains only a few minutes of
    post-sunrise data.  Under overcast, the diffuse radiation at low sun angles
    is a disproportionately high fraction of the small clear-sky reference,
    inflating Km.  Mirroring extends the window backward using cos(zenith)
    interpolation so that Km sees a longer, more representative baseline.

    Algorithm (adapted from CAELUS mirror_ghi_with_pandas()):
    - Compute cos(zenith) for each ring entry.
    - Post-sunrise entries: cos_z > 0 (real data, sun above horizon).
    - Pre-sunrise entries: cos_z <= 0 (synthetic needed).
    - For each pre-sunrise entry, query the post-sunrise (cos_z, GHI) curve
      at -cos_z (the symmetric morning angle), then negate the result to
      maintain sign convention.
    - Mirrored entry denominator uses the maxSolarRad of the post-sunrise
      entry at the same mirrored cos_z — NOT zero — so mean(maxSolarRad) is
      not diluted by zeros.  This avoids Km instability from near-zero
      denominators while still extending the effective baseline.

    If station coordinates are not configured, or fewer than 2 post-sunrise
    entries exist, returns the real ring entries unchanged.

    Only used by _compute_indices() for Km.  Kv/Kvf always use the raw ring.
    """
    if _skyfield_observer is None:
        return [(r.ghi, r.max_solar_rad) for r in ring_entries]

    # Compute cos(zenith) for every ring entry.
    cosz_list: list[float | None] = [_cos_zenith_for_ts(r.ts) for r in ring_entries]

    # Separate post-sunrise (cos_z > 0) from pre-sunrise (cos_z <= 0).
    post_cosz: list[float] = []
    post_ghi: list[float] = []
    post_msr: list[float] = []
    pre_indices: list[int] = []

    for i, (entry, cz) in enumerate(zip(ring_entries, cosz_list)):
        if cz is None:
            # Can't determine; treat as real entry.
            continue
        if cz > 0.0:
            post_cosz.append(cz)
            post_ghi.append(entry.ghi)
            post_msr.append(entry.max_solar_rad)
        else:
            pre_indices.append(i)

    # Need at least 2 post-sunrise points for interpolation.
    if len(post_cosz) < 2 or not pre_indices:
        return [(r.ghi, r.max_solar_rad) for r in ring_entries]

    # Sort post-sunrise data by cos_z ascending for bisect interpolation.
    sorted_pairs = sorted(zip(post_cosz, post_ghi, post_msr))
    sorted_cosz = [p[0] for p in sorted_pairs]
    sorted_ghi = [p[1] for p in sorted_pairs]
    sorted_msr = [p[2] for p in sorted_pairs]

    # Build result: start with all real entries.
    result: list[tuple[float, float]] = [
        (r.ghi, r.max_solar_rad) for r in ring_entries
    ]

    # Replace pre-sunrise entries with mirrored synthetic entries where possible.
    for idx in pre_indices:
        cz = cosz_list[idx]
        if cz is None:
            continue
        # Mirror cos_z: for a pre-sunrise entry at -|cos_z|, look up the
        # post-sunrise curve at +|cos_z| (symmetric angle).
        mirror_cz = -cz  # cz <= 0, so mirror_cz >= 0

        mirrored_ghi = _interp_linear(sorted_cosz, sorted_ghi, mirror_cz)
        mirrored_msr = _interp_linear(sorted_cosz, sorted_msr, mirror_cz)

        if mirrored_ghi is None or mirrored_msr is None:
            # mirror_cz outside interpolatable range; keep real entry.
            continue

        # Negate GHI to follow CAELUS sign convention for pre-sunrise synthetic
        # data, then take absolute value for the Km ratio.  Under overcast,
        # mirrored_ghi is small (low real post-sunrise GHI), so abs keeps Km low.
        # Use the interpolated maxSolarRad (not zero) to avoid diluting the
        # mean(maxSolarRad) denominator.
        synthetic_ghi = abs(mirrored_ghi)
        synthetic_msr = abs(mirrored_msr)

        if synthetic_msr > 0:
            result[idx] = (synthetic_ghi, synthetic_msr)
        # else: maxSolarRad still zero at that angle; keep real entry.

    return result


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


def _compute_indices() -> tuple[float, float, float, float, float, float] | None:
    """Compute (Kcs, Km, Kmf, Kv, Kvf, latest_msr) from the ring buffer.

    Returns None when ring has < 3 entries (startup guard).

    Km/Kmf are computed from mirrored (ghi, max_solar_rad) pairs when station
    coordinates are available — see _mirror_for_km().  Kv and Kvf always
    use the raw ring buffer (real measurements only).
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

    # Km: mean normalized irradiance over the full ring, with GHI mirroring.
    # _mirror_for_km() returns (ghi, msr) pairs where pre-sunrise entries
    # may be replaced by synthetic mirrored values for a stable Km baseline.
    ring_list = list(_ring)
    km_pairs = _mirror_for_km(ring_list)
    km_ghi = [p[0] for p in km_pairs]
    km_msr = [p[1] for p in km_pairs]
    km_mean_msr = sum(km_msr) / len(km_msr)
    if km_mean_msr > 0:
        km = max(sum(km_ghi) / len(km_ghi) / km_mean_msr, 0.0)
    else:
        km = 0.0

    # Kv: coarse variability (30-min window).
    # Detrended by clear-sky model: subtract the predicted change (maxSolarRad
    # delta) from the observed change (GHI delta) so the natural solar geometry
    # signal cancels out.  Without this, a clear afternoon's steady GHI decline
    # produces non-zero Kv that exceeds the CLOUDLESS threshold (0.03).
    #
    # CAELUS uses centered rolling windows (batch mode) which partially suppress
    # the geometry trend.  In real-time streaming we use a trailing window; the
    # clear-sky detrending compensates for the loss of centering.
    #
    # Scientific basis: dividing (or detrending) by clear-sky irradiance to
    # isolate cloud-induced variability is standard practice in solar energy
    # research (Stein et al. 2012 — Variability Index, Sandia SAND2012-3464C;
    # Coimbra et al. 2013 — clear-sky index stationarity).
    #
    # Kv/Kvf always use the raw ring (not mirrored) — variability metrics
    # must reflect only real measurement fluctuations.

    diff_abs_all: list[float] = []
    for i in range(1, len(_ring)):
        ghi_delta = _ring[i].ghi - _ring[i - 1].ghi
        msr_delta = _ring[i].max_solar_rad - _ring[i - 1].max_solar_rad
        diff_abs_all.append(abs(ghi_delta - msr_delta))

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

    # Kmf: fine mean transmittance (10-min window).
    # Same formula as Km but over the 10-min subset. Used in the variable
    # branch where responsiveness to recent clearing/clouding matters.
    if len(fine_indices) < 2:
        kmf = km
    else:
        fine_pairs = [km_pairs[i] for i in fine_indices]
        fine_ghi = [p[0] for p in fine_pairs]
        fine_msr = [p[1] for p in fine_pairs]
        fine_mean_msr = sum(fine_msr) / len(fine_msr)
        if fine_mean_msr > 0:
            kmf = max(sum(fine_ghi) / len(fine_ghi) / fine_mean_msr, 0.0)
        else:
            kmf = 0.0

    return kcs, km, kmf, kv, kvf, latest.max_solar_rad


def _classify_sky(
    kcs: float, km: float, kmf: float, kv: float, kvf: float,
    latest_msr: float,
) -> str:
    """Classify sky condition using the Kv-first decision tree (ADR-073).

    Primary axis: asymmetric Kv/Kvf gate (uniform vs. variable sky).
    Uniform requires both the 30-min (Kv) and 10-min (Kvf) windows below
    _KV_UNIFORM; variable triggers if either exceeds the threshold.
    Secondary: Km (30-min) in uniform branch, Kmf (10-min) in variable
    branch. Cloud enhancement evaluated first as a special case.
    """
    # --- Cloud enhancement (Kcs above clear-sky + high variability) ---
    if (
        latest_msr > _SZA80_MSR_PROXY
        and kcs > _CLOUDEN_MIN_KCS
        and kv > _CLOUDEN_MIN_KV
        and kvf > _CLOUDEN_MIN_KVF
    ):
        return "Partly Cloudy"

    # --- Primary axis: asymmetric Kv/Kvf gate (uniform vs. variable sky) ---
    # Uniform requires BOTH the coarse (30-min) and fine (10-min) windows calm;
    # variable triggers if EITHER shows activity.
    if kv < _KV_UNIFORM and kvf < _KV_UNIFORM:
        # UNIFORM SKY — no cloud-edge transitions detected.
        # Km distinguishes clear (high) from overcast (moderate/low).
        if km > _UNIFORM_CLEAR_MIN_KM and kcs > _UNIFORM_CLEAR_MIN_KCS:
            return "Clear"
        if km > _UNIFORM_HEAVY_MAX_KM:
            return "Overcast"
        return "Heavy Overcast"

    # VARIABLE SKY — cloud-edge transitions present.
    # Kmf (10-min) distinguishes coverage degree — responsive to recent changes.
    if kmf > _VARIABLE_CLEAR_MIN_KM:
        return "Mostly Clear"
    if kmf > _VARIABLE_PARTLY_MIN_KM:
        return "Partly Cloudy"
    if kmf > _VARIABLE_MOSTLY_MIN_KM:
        return "Mostly Cloudy"
    return "Cloudy"


def _apply_coherence_filter(raw_label: str, now: float) -> str | None:
    """Apply temporal coherence filter to prevent rapid label flicker.

    A raw label must persist for 5 consecutive minutes before becoming
    stable. On startup, 2 consecutive minutes suffice as a grace period.
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

    if consecutive_span >= 300.0:  # 5 minutes
        _last_stable_label = latest_label
    elif _last_stable_label is None and consecutive_span >= 120.0:  # 2-min startup grace
        _last_stable_label = latest_label

    return _last_stable_label
