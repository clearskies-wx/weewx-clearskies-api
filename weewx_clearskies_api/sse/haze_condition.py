"""Haze detection module (ADR-067).

Two-channel confirmation algorithm: both the pyranometer Kcs deficit channel
and the PM concentration channel must fire before a "Hazy" label is emitted.

Channel 1 — Kcs deficit:
  Current Kcs is compared against a station-specific clean-sky baseline.
  A positive deficit (baseline - kcs > 0) indicates aerosol extinction beyond
  what a clean atmosphere produces.  The deficit threshold is adjusted by an
  f(RH) hygroscopic correction factor.

Channel 2 — PM confirmation:
  PM2.5 or PM10 from an observed-data AQI provider must exceed concentration
  thresholds that depend on current RH.  This prevents the Kcs deficit from
  triggering on cirrus or other non-aerosol optical effects.

Gates applied before channel evaluation:
  - Solar elevation gate (el > 10°): Kcs is unreliable at low sun angles.
  - Clear-sky-only constraint: haze is invalid under thick cloud cover.
  - Wet deposition gate: suppress during rain and 30 min post-rain.
  - RH > 90% gate: defer to fog/mist detection (Phase 5).

Temporal coherence filter:
  A 5-minute rolling window (deque of (timestamp, bool) pairs). Haze is
  only reported when ≥ 50% of entries in the window show is_hazy=True.
  Prevents label flicker at aerosol-concentration boundaries.

Phase milestones (completed):
  Phase 6: set_baseline() is called by the auto-calibration module (ADR-068)
            with a learned monthly-normal clean-sky Kcs percentile.
  Phase 8: set_gamma() is wired to the [conditions] gamma key in api.conf.

Module-level state is intentional — the API is a single-process service.
Use reset() for test isolation.
"""

from __future__ import annotations

import time
from collections import deque
from math import exp

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Haze detection enabled flag.  Set to False via set_enabled() when the
# operator sets [conditions] haze_detection = false in api.conf.
_enabled: bool = True

# Temporary fixed clean-sky baseline.  CLOUDLESS class Kcs > 0.85; 0.90 is a
# conservative estimate that sits above the cloudless threshold.  Phase 6 will
# replace this with a station-specific learned value (90th–95th percentile of
# qualifying clean-sky Kcs samples over a 90-day rolling window).
_clean_kcs_baseline: float = 0.90

# Hygroscopic correction exponent.  γ = 0.45 is the "composition-unknown"
# default per Hanel (1976) and Tang (1996).  Phase 8 will make this
# operator-configurable per region via the admin UI.
_gamma: float = 0.45

# Reference RH for the f(RH) correction (dry baseline, dimensionless fraction).
_RH_REF: float = 0.40

# Minimum Kcs deficit at reference RH before haze is considered.
# 0.03 = 3% below baseline.  Above consumer pyranometer noise floor
# (Lindfors 2013: ±20% uncertainty for pyranometer-derived AOD vs AERONET).
# f(RH) scales this up at high humidity: at RH=80% threshold≈0.049,
# at RH=89% threshold≈0.064 (with default γ=0.45).
_DEFICIT_THRESHOLD: float = 0.03

# RH-graduated PM confirmation thresholds (µg/m³).
# Research basis: CMA dry haze (~54 µg/m³ PM2.5 for vis < 10 km),
# IMPROVE extinction ratio (coarse 0.6 m²/g vs fine 3-4 m²/g),
# WMO dusty-air definition (50-200 µg/m³ PM10).
# See docs/reference/haze-detection-research.md.
_PM_THRESHOLDS_DRY: tuple[float, float] = (50.0, 100.0)      # PM2.5, PM10 at RH < 60%
_PM_THRESHOLDS_MODERATE: tuple[float, float] = (35.0, 75.0)   # PM2.5, PM10 at RH 60-80%
_PM_THRESHOLDS_HUMID: tuple[float, float] = (25.0, 50.0)      # PM2.5, PM10 at RH 80-90%

# Rolling history of (unix_timestamp, is_hazy) pairs for the 5-minute
# temporal coherence filter.  Entries older than 300 s are pruned on each
# call to detect_haze().
_haze_history: deque[tuple[float, bool]] = deque()

# Unix timestamp of the last rain cessation (i.e., the moment rain_rate first
# dropped to zero after a non-zero period).  Initialised to 0.0 so that the
# 30-minute wet-deposition holdoff is effectively expired at startup.
_last_rain_end: float = 0.0

# Track whether it was raining on the previous call so we can detect
# the rain→no-rain transition.
_was_raining: bool = False

# ---------------------------------------------------------------------------
# Sky labels that are compatible with a haze detection (clear-ish sky).
# Haze is a clear-sky modifier; it is invalid under opaque cloud decks.
# ---------------------------------------------------------------------------

_BLOCKED_SKY_LABELS: frozenset[str] = frozenset(
    {"Mostly Cloudy", "Cloudy", "Overcast", "Heavy Overcast"}
)

_HAZE_ELIGIBLE_SKY_SUBSTRINGS: tuple[str, ...] = (
    "Clear",
    "Sunny",
    "Mostly Clear",
    "Mostly Sunny",
    "Partly Cloudy",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_enabled(value: bool) -> None:
    """Enable or disable haze detection.

    Called from __main__.py when the operator sets
    [conditions] haze_detection = false in api.conf.
    """
    global _enabled  # noqa: PLW0603
    _enabled = value


def detect_haze(
    *,
    kcs: float | None,
    solar_elevation: float | None,
    sky_label: str | None,
    pm25: float | None,
    pm10: float | None,
    out_temp: float | None,
    dewpoint: float | None,
    rain_rate: float | None,
) -> str | None:
    """Return 'Hazy' if haze is detected, or None.

    Implements the two-channel confirmation algorithm from API-MANUAL §8.

    Both channels must fire for a 'Hazy' label to be emitted:
      Channel 1: Kcs deficit below the auto-calibrated clean-sky baseline.
      Channel 2: PM2.5 or PM10 exceeds the RH-conditioned threshold.

    Args:
        kcs:              Current clear-sky index (GHI / maxSolarRad).
        solar_elevation:  Solar elevation angle in degrees.
        sky_label:        Current sky classification from sky_condition.classify().
        pm25:             PM2.5 concentration in µg/m³ (smoothed).
        pm10:             PM10 concentration in µg/m³ (smoothed).
        out_temp:         Dry-bulb temperature in °F (US units from smoother).
        dewpoint:         Dewpoint temperature in °F (US units from smoother).
        rain_rate:        Rain rate in inch/hour (smoothed).

    Returns:
        'Hazy' if both channels confirm and temporal coherence is satisfied,
        None otherwise.
    """
    if not _enabled:
        return None

    global _last_rain_end, _was_raining  # noqa: PLW0603

    now = time.time()

    # ------------------------------------------------------------------
    # Gate 1: Solar elevation — Kcs is unreliable at low sun angles.
    # The Ryan-Stolzenbach model underestimates maxSolarRad by ~20% below
    # 10° elevation, making Kcs deficit comparisons unreliable.
    # ------------------------------------------------------------------
    if solar_elevation is None or solar_elevation <= 10.0:
        _record_history(now, is_hazy=False)
        return None

    # ------------------------------------------------------------------
    # Gate 2: Clear-sky-only constraint.
    # Haze is a clear-sky modifier. Block on opaque cloud labels.
    # When sky_label is None (startup), allow the detection to proceed —
    # the PM + Kcs channels will gate appropriately.
    # ------------------------------------------------------------------
    if sky_label is not None:
        if sky_label in _BLOCKED_SKY_LABELS:
            _record_history(now, is_hazy=False)
            return None
        # Verify at least one haze-eligible substring is present.
        # This catches any future sky labels not in the blocked set.
        if not any(sub in sky_label for sub in _HAZE_ELIGIBLE_SKY_SUBSTRINGS):
            _record_history(now, is_hazy=False)
            return None

    # ------------------------------------------------------------------
    # Gate 3: Wet deposition gate.
    # Rain scavenges aerosols. Suppress haze during active rain and for
    # 30 minutes after rain ends.
    # ------------------------------------------------------------------
    currently_raining = rain_rate is not None and rain_rate > 0.0

    if currently_raining:
        # Rain is active — update the end timestamp every cycle so that
        # the 30-minute holdoff starts from when rain actually stops.
        _last_rain_end = now
        _was_raining = True
        _record_history(now, is_hazy=False)
        return None

    if _was_raining and not currently_raining:
        # Transition: rain just stopped. Record the cessation time.
        _last_rain_end = now
        _was_raining = False

    if (now - _last_rain_end) < 1800.0:
        _record_history(now, is_hazy=False)
        return None

    # ------------------------------------------------------------------
    # RH computation (Magnus-Tetens approximation).
    # Both temperatures are in °F (US units from input_smoother).
    # Convert to °C for the formula.
    # ------------------------------------------------------------------
    rh: float | None = None

    if out_temp is not None and dewpoint is not None:
        t_c = (out_temp - 32.0) * 5.0 / 9.0
        td_c = (dewpoint - 32.0) * 5.0 / 9.0
        denom_t = 243.04 + t_c
        denom_td = 243.04 + td_c
        if denom_t != 0.0 and denom_td != 0.0:
            rh = 100.0 * (
                exp((17.625 * td_c) / denom_td)
                / exp((17.625 * t_c) / denom_t)
            )
            # Clamp to [0, 100] to absorb sensor noise.
            rh = max(0.0, min(100.0, rh))

    # ------------------------------------------------------------------
    # Gate 4: RH type discriminator.
    # RH > 90%: aerosol extinction is dominated by water droplets — defer
    # to fog/mist detection (Phase 5). Do NOT report haze.
    # ------------------------------------------------------------------
    if rh is not None and rh > 90.0:
        _record_history(now, is_hazy=False)
        return None

    # ------------------------------------------------------------------
    # Channel 2: PM confirmation.
    # At least one PM threshold must be exceeded before we proceed.
    # Graceful degradation: if no PM data is available, return None.
    # ------------------------------------------------------------------
    if pm25 is None and pm10 is None:
        _record_history(now, is_hazy=False)
        return None

    # RH-graduated PM thresholds (CMA/IMPROVE/WMO research-backed).
    # Both PM2.5 and PM10 are independent first-class indicators.
    if rh is not None and rh >= 80.0:
        pm25_threshold, pm10_threshold = _PM_THRESHOLDS_HUMID
    elif rh is not None and rh >= 60.0:
        pm25_threshold, pm10_threshold = _PM_THRESHOLDS_MODERATE
    else:
        pm25_threshold, pm10_threshold = _PM_THRESHOLDS_DRY

    pm_confirmed = False
    if pm25 is not None and pm25 > pm25_threshold:
        pm_confirmed = True
    if not pm_confirmed and pm10 is not None and pm10 > pm10_threshold:
        pm_confirmed = True

    if not pm_confirmed:
        _record_history(now, is_hazy=False)
        return None

    # ------------------------------------------------------------------
    # Channel 1: Kcs deficit.
    # A positive deficit (baseline - kcs) indicates aerosol extinction.
    # ------------------------------------------------------------------
    if kcs is None:
        _record_history(now, is_hazy=False)
        return None

    deficit = _clean_kcs_baseline - kcs
    if deficit <= 0.0:
        _record_history(now, is_hazy=False)
        return None

    # f(RH) hygroscopic correction (Hanel 1976 / Tang 1996).
    # At higher humidity, aerosol particles absorb water and swell,
    # producing more extinction per unit mass.  This inflates the Kcs
    # deficit even from clean-air background aerosol.  The correction
    # scales the deficit threshold UP at high humidity to distinguish
    # real haze extinction from humidity-inflated clean-air scattering.
    if rh is not None and 0.0 < rh < 100.0:
        rh_frac = rh / 100.0
        f_rh = ((1.0 - rh_frac) / (1.0 - _RH_REF)) ** (-_gamma)
    else:
        f_rh = 1.0

    adjusted_threshold = _DEFICIT_THRESHOLD * f_rh
    if deficit <= adjusted_threshold:
        _record_history(now, is_hazy=False)
        return None

    # Both channels confirmed and deficit exceeds f(RH)-adjusted threshold.
    _record_history(now, is_hazy=True)

    # ------------------------------------------------------------------
    # Temporal coherence filter: 5-minute window.
    # Haze is only reported when ≥ 50% of entries in the window are True.
    # Prevents label flicker at aerosol-concentration boundaries.
    # ------------------------------------------------------------------
    return _evaluate_coherence(now)


def set_baseline(value: float) -> None:
    """Set the clean-sky Kcs baseline.  Called by Phase 6 auto-calibration."""
    global _clean_kcs_baseline  # noqa: PLW0603
    _clean_kcs_baseline = float(value)


def set_gamma(value: float) -> None:
    """Set the f(RH) hygroscopic correction exponent.

    Wired to the [conditions] gamma key in api.conf (Phase 8, ADR-068).
    γ range: 0.12 (mineral dust) to 1.52 (sea salt) per Hanel 1976 / Tang 1996.
    Default 0.45 is composition-unknown (moderate).
    """
    global _gamma  # noqa: PLW0603
    _gamma = float(value)


def set_deficit_threshold(value: float) -> None:
    """Set the minimum Kcs deficit threshold at reference RH."""
    global _DEFICIT_THRESHOLD  # noqa: PLW0603
    _DEFICIT_THRESHOLD = float(value)


def reset() -> None:
    """Clear all module-level state.  For test isolation only."""
    global _enabled, _clean_kcs_baseline, _gamma, _DEFICIT_THRESHOLD  # noqa: PLW0603
    global _last_rain_end, _was_raining  # noqa: PLW0603
    _enabled = True
    _clean_kcs_baseline = 0.90
    _gamma = 0.45
    _DEFICIT_THRESHOLD = 0.03
    _last_rain_end = 0.0
    _was_raining = False
    _haze_history.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _record_history(now: float, *, is_hazy: bool) -> None:
    """Append a (timestamp, is_hazy) entry and prune entries older than 5 min."""
    _haze_history.append((now, is_hazy))
    cutoff = now - 300.0
    while _haze_history and _haze_history[0][0] < cutoff:
        _haze_history.popleft()


def _evaluate_coherence(now: float) -> str | None:
    """Return 'Hazy' if ≥ 50% of the 5-minute history entries are True."""
    window = list(_haze_history)
    if not window:
        return None
    hazy_count = sum(1 for _, is_hazy in window if is_hazy)
    total_count = len(window)
    if hazy_count / total_count >= 0.50:
        return "Hazy"
    return None
