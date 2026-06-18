"""Sky condition classification from solar radiation (ADR-044).

Uses the clear sky index kc = GHI_measured / GHI_clearsky with temporal
variability analysis over a 30-minute sliding window to classify sky
conditions. maxSolarRad from weewx serves as the clear-sky reference.

When measured radiation meets or exceeds maxSolarRad, kc is pinned to
1.0 — the sensor receives at least as much energy as the clear-sky model
predicts, so nothing is blocking the sun.  This prevents false "Cloudy"
classifications near sunrise/sunset when the maxSolarRad model
underestimates actual clear-sky irradiance at low solar elevations.

Daytime detection uses whichever is larger — measured radiation or
maxSolarRad — so the rolling buffer is not cleared while the sensor
still reports meaningful solar energy (even if the model has already
crossed below the night threshold).

Thresholds derived from:
  - NWS sky condition categories (okta-based)
  - Kasten & Czeplak (1980) cloud-cover-to-kc model: kc = 1 - 0.75*(N/8)^3.4
  - ASOS 30-minute window with last-10-minute double-weighting
  - Sigma threshold ~0.05 from solar variability research

Low-sigma (uniform sky) thresholds:
  Clear          kc >= 0.85  (Davis 6450 ±5% + maxSolarRad ±4% model error
                              means a clear sky can read ~0.93 from systematic
                              bias alone; 0.95 was inside the noise floor)
  Mostly Clear   kc >= 0.70
  Partly Cloudy  kc >= 0.50
  Mostly Cloudy  kc >= 0.30
  Cloudy         kc <  0.30

High-sigma (variable/broken sky) thresholds:
  Mostly Clear   kc >= 0.85
  Partly Cloudy  kc >= 0.60
  Mostly Cloudy  kc <  0.60

Module-level state (the deque buffer) is intentional. The API is a
single-process service; the buffer must persist across requests and
packet calls. Use reset() in tests to isolate test cases.
"""

from __future__ import annotations

import math
import time
from collections import deque

# ---------------------------------------------------------------------------
# Rolling buffer configuration
# ---------------------------------------------------------------------------

# 30-minute sliding window at ~5-second loop packet intervals = ~360 entries.
# Matches ASOS operational standard for sky condition reporting.
_WINDOW_SECONDS: float = 1800.0

# Recent window for ASOS-style double-weighting (last 10 minutes).
_RECENT_SECONDS: float = 600.0

# Minimum samples before a classification is returned.
_MIN_SAMPLES: int = 36

# Night guard: below this (W/m²) there is not enough solar energy for
# meaningful classification.
_MIN_SOLAR_RAD: float = 20.0

# Pyranometer noise floor (W/m²). Below this, treat as zero.
_NOISE_FLOOR: float = 0.0

# ---------------------------------------------------------------------------
# Classification thresholds
#
# Low-sigma (uniform sky) — see module docstring for threshold rationale.
# ---------------------------------------------------------------------------

# Low sigma branch (uniform sky):
_KC_CLEAR: float = 0.85
_KC_MOSTLY_CLEAR: float = 0.70
_KC_PARTLY_CLOUDY: float = 0.50
_KC_MOSTLY_CLOUDY: float = 0.30

# High sigma branch (variable/broken sky) — shifted down because high
# variability itself signals broken clouds even at higher mean kc.
_KC_VAR_MOSTLY_CLEAR: float = 0.85
_KC_VAR_PARTLY_CLOUDY: float = 0.60

# Sigma threshold separating uniform from variable sky.
# Research uses ~0.05; we use 0.08 as a practical middle ground for
# 5-second sampling (noisier than 1-minute research data).
_SIGMA_THRESHOLD: float = 0.08

# Hysteresis band (±) applied to tier boundaries. Once classified into a
# tier, kc must move beyond the boundary ± this band to change tier.
# Prevents rapid oscillation at tier edges (observatory best practice).
_HYSTERESIS: float = 0.03

# Maximum kc (cloud-edge enhancement; Tapakis & Charalambides 2014).
_KC_MAX: float = 1.2

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_buffer: deque[tuple[float, float]] = deque()
_was_daytime: bool = False
_last_classification: str | None = None


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

    When measured radiation meets or exceeds maxSolarRad, kc is set to
    1.0 — the sensor reads at least as much energy as the clear-sky model
    predicts, so nothing is blocking the sun.
    """
    if timestamp is None:
        timestamp = time.time()

    global _was_daytime
    _rad = radiation if isinstance(radiation, (int, float)) else 0.0
    _msr = max_solar_rad if isinstance(max_solar_rad, (int, float)) else 0.0
    currently_daytime = max(_rad, _msr) >= _MIN_SOLAR_RAD
    if _was_daytime and not currently_daytime:
        _buffer.clear()
        global _last_classification
        _last_classification = None
    _was_daytime = currently_daytime

    if not currently_daytime:
        return
    if radiation is None or radiation < _NOISE_FLOOR:
        return

    if _msr > 0 and _rad < _msr:
        kc = radiation / max_solar_rad  # type: ignore[operator]
        kc = min(kc, _KC_MAX)
    else:
        kc = 1.0

    kc = max(kc, 0.0)
    _buffer.append((timestamp, kc))

    cutoff = timestamp - _WINDOW_SECONDS
    while _buffer and _buffer[0][0] < cutoff:
        _buffer.popleft()


def classify() -> str | None:
    """Classify sky condition from the rolling buffer.

    Uses ASOS-style recency weighting (last 10 minutes double-weighted)
    and sigma-first classification with hysteresis to prevent oscillation.

    Returns:
        One of: "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy",
        "Cloudy", or None when insufficient data.
    """
    if len(_buffer) < _MIN_SAMPLES:
        return None

    now = _buffer[-1][0]
    recent_cutoff = now - _RECENT_SECONDS

    # ASOS-style: last 10 minutes double-weighted
    values: list[float] = []
    for ts, kc in _buffer:
        values.append(kc)
        if ts >= recent_cutoff:
            values.append(kc)  # double-weight recent

    n = len(values)
    mean_kc = sum(values) / n
    variance = sum((v - mean_kc) ** 2 for v in values) / n
    sigma_kc = math.sqrt(variance)

    # Determine raw classification from thresholds
    raw = _classify_raw(mean_kc, sigma_kc)

    # Apply hysteresis: only change classification if kc has moved
    # convincingly past the boundary (beyond the hysteresis band)
    global _last_classification
    if _last_classification is not None and raw != _last_classification:
        raw_no_hyst = _classify_raw(mean_kc, sigma_kc)
        raw_with_hyst = _classify_with_hysteresis(mean_kc, sigma_kc, _last_classification)
        if raw_with_hyst == _last_classification:
            return _last_classification
        _last_classification = raw_no_hyst
    else:
        _last_classification = raw

    return _last_classification


def _classify_raw(mean_kc: float, sigma_kc: float) -> str:
    """Core classification without hysteresis."""
    if sigma_kc < _SIGMA_THRESHOLD:
        # Uniform sky
        if mean_kc >= _KC_CLEAR:
            return "Clear"
        if mean_kc >= _KC_MOSTLY_CLEAR:
            return "Mostly Clear"
        if mean_kc >= _KC_PARTLY_CLOUDY:
            return "Partly Cloudy"
        if mean_kc >= _KC_MOSTLY_CLOUDY:
            return "Mostly Cloudy"
        return "Cloudy"
    else:
        # Variable/broken sky
        if mean_kc >= _KC_VAR_MOSTLY_CLEAR:
            return "Mostly Clear"
        if mean_kc >= _KC_VAR_PARTLY_CLOUDY:
            return "Partly Cloudy"
        return "Mostly Cloudy"


def _classify_with_hysteresis(
    mean_kc: float, sigma_kc: float, current: str,
) -> str:
    """Classify using shifted thresholds that favor staying in current tier."""
    h = _HYSTERESIS
    if sigma_kc < _SIGMA_THRESHOLD:
        # To LEAVE current tier, kc must cross boundary ± hysteresis
        if current == "Clear":
            if mean_kc >= _KC_CLEAR - h:
                return "Clear"
        elif current == "Mostly Clear":
            if _KC_MOSTLY_CLEAR - h <= mean_kc < _KC_CLEAR + h:
                return "Mostly Clear"
        elif current == "Partly Cloudy":
            if _KC_PARTLY_CLOUDY - h <= mean_kc < _KC_MOSTLY_CLEAR + h:
                return "Partly Cloudy"
        elif current == "Mostly Cloudy":
            if _KC_MOSTLY_CLOUDY - h <= mean_kc < _KC_PARTLY_CLOUDY + h:
                return "Mostly Cloudy"
        elif current == "Cloudy":
            if mean_kc < _KC_MOSTLY_CLOUDY + h:
                return "Cloudy"
    else:
        if current == "Mostly Clear":
            if mean_kc >= _KC_VAR_MOSTLY_CLEAR - h:
                return "Mostly Clear"
        elif current == "Partly Cloudy":
            if _KC_VAR_PARTLY_CLOUDY - h <= mean_kc < _KC_VAR_MOSTLY_CLEAR + h:
                return "Partly Cloudy"
        elif current == "Mostly Cloudy":
            if mean_kc < _KC_VAR_PARTLY_CLOUDY + h:
                return "Mostly Cloudy"
    # Hysteresis didn't hold — return the raw classification
    return _classify_raw(mean_kc, sigma_kc)


def is_daytime() -> bool:
    """Return True when the buffer has a recent daytime reading."""
    if not _buffer:
        return False
    now = time.time()
    return (now - _buffer[-1][0]) < 300.0


def reset() -> None:
    """Clear the rolling buffer and reset all state. For test isolation only."""
    global _was_daytime, _last_classification
    _buffer.clear()
    _was_daytime = False
    _last_classification = None
