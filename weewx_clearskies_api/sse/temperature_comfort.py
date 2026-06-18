"""Temperature-comfort 2D classifier (ADR-044 §5-7).

Combines an apparent-temperature axis (§5) with a dewpoint-based moisture axis
(§6) to produce a composite descriptor (§7).  Three stability mechanisms
(§8) prevent the label from bouncing at tier boundaries:

1. Smoothed inputs — callers should pass rolling-average values from
   ``enrichment.input_smoother.get_smoothed()``.
2. Hysteresis — module-level tier state is only updated when a value crosses
   2 °F past the opposite boundary.
3. Hold time — the composed string is cached for 5 minutes minimum.
"""

from __future__ import annotations

import time

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

_TEMP_HYSTERESIS: float = 2.0   # °F — required overshoot to change temp tier
_DP_HYSTERESIS: float = 2.0     # °F — required overshoot to change moisture tier
_HOLD_SECONDS: float = 300.0    # 5 minutes — minimum hold on cached output

# ---------------------------------------------------------------------------
# Temperature tiers
#
# Each entry is (upper_bound_inclusive, label).  The last tier has no upper
# bound — it is used when value > all listed upper bounds.
# Tiers 1-5 (appTemp ≤ 32 °F) suppress the moisture modifier.
# ---------------------------------------------------------------------------

_TEMP_TIERS: list[tuple[float, str]] = [
    (-10.0, "Dangerously Cold"),  # tier 1: ≤ −10
    (0.0,   "Bitter Cold"),       # tier 2: −9 to 0
    (10.0,  "Extreme Cold"),      # tier 3: 1 to 10
    (20.0,  "Very Cold"),         # tier 4: 11 to 20
    (32.0,  "Cold"),              # tier 5: 21 to 32
    (45.0,  "Chilly"),            # tier 6: 33 to 45
    (60.0,  "Cool"),              # tier 7: 46 to 60
    (75.0,  "Pleasant"),          # tier 8: 61 to 75
    (85.0,  "Warm"),              # tier 9: 76 to 85
    (95.0,  "Hot"),               # tier 10: 86 to 95
    (104.0, "Very Hot"),          # tier 11: 96 to 104
    # tier 12: ≥ 105 — default
]
_TEMP_DEFAULT: str = "Dangerously Hot"

# Number of tiers where moisture is suppressed (tiers 1-5, appTemp ≤ 32 °F).
# These are the first 5 entries (indices 0-4) in _TEMP_TIERS.
_COLD_TIER_COUNT: int = 5

# ---------------------------------------------------------------------------
# Moisture tiers
#
# Each entry is (upper_bound_inclusive, modifier | None).  None = no modifier.
# The last tier has no upper bound.
# ---------------------------------------------------------------------------

_MOISTURE_TIERS: list[tuple[float, str | None]] = [
    (44.9,  None),              # tier A: < 45 °F
    (54.9,  None),              # tier B: 45–54 °F
    (59.9,  "Slightly Humid"),  # tier C: 55–59 °F
    (64.9,  "Humid"),           # tier D: 60–64 °F
    (69.9,  "Very Humid"),      # tier E: 65–69 °F
    (74.9,  "Oppressive"),      # tier F: 70–74 °F
    # tier G: ≥ 75 — default
]
_MOISTURE_DEFAULT: str = "Miserable"

# ---------------------------------------------------------------------------
# NWS danger thresholds
# ---------------------------------------------------------------------------

_HI_EXTREME_DANGER: float = 125.0   # °F — Heat Index
_HI_DANGER: float = 104.0           # °F — Heat Index
_WC_EXTREME_DANGER: float = -45.0   # °F — Wind Chill
_WC_DANGER: float = -25.0           # °F — Wind Chill

# ---------------------------------------------------------------------------
# Module-level hysteresis + hold state
# ---------------------------------------------------------------------------

_current_temp_tier: int | None = None    # index into _TEMP_TIERS (or len = default)
_current_moisture_tier: int | None = None  # index into _MOISTURE_TIERS (or len = default)
_cached_result: str | None = None
_cache_time: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _temp_tier_for(value: float) -> int:
    """Return the tier index for *value* (no hysteresis applied).

    Returns an index 0..(len(_TEMP_TIERS)-1) when value falls within a listed
    tier, or ``len(_TEMP_TIERS)`` for the default (highest) tier.
    """
    for i, (upper, _label) in enumerate(_TEMP_TIERS):
        if value <= upper:
            return i
    return len(_TEMP_TIERS)  # default tier (Dangerously Hot)


def _moisture_tier_for(value: float) -> int:
    """Return the tier index for *value* (no hysteresis applied).

    Returns an index 0..(len(_MOISTURE_TIERS)-1) when value falls within a
    listed tier, or ``len(_MOISTURE_TIERS)`` for the default (highest) tier.
    """
    for i, (upper, _modifier) in enumerate(_MOISTURE_TIERS):
        if value <= upper:
            return i
    return len(_MOISTURE_TIERS)  # default tier (Miserable)


def _temp_label(tier_index: int) -> str:
    """Return the temperature label for *tier_index*."""
    if tier_index < len(_TEMP_TIERS):
        return _TEMP_TIERS[tier_index][1]
    return _TEMP_DEFAULT


def _moisture_modifier(tier_index: int) -> str | None:
    """Return the moisture modifier for *tier_index*, or None."""
    if tier_index < len(_MOISTURE_TIERS):
        return _MOISTURE_TIERS[tier_index][1]
    return _MOISTURE_DEFAULT


def _temp_tier_upper(tier_index: int) -> float:
    """Return the inclusive upper bound of *tier_index*.

    For the default (last) tier there is no listed upper bound; return a
    sentinel value large enough to be unreachable in practice.
    """
    if tier_index < len(_TEMP_TIERS):
        return _TEMP_TIERS[tier_index][0]
    return 1_000_000.0


def _temp_tier_lower(tier_index: int) -> float:
    """Return the effective lower bound of *tier_index*.

    Tier 0 has no lower bound; return a sentinel value small enough to be
    unreachable in practice.
    """
    if tier_index == 0:
        return -1_000_000.0
    # Lower bound = upper bound of previous tier + ε.  For hysteresis
    # purposes we use the previous tier's upper bound as the boundary value.
    return _TEMP_TIERS[tier_index - 1][0]


def _moisture_tier_upper(tier_index: int) -> float:
    """Return the inclusive upper bound of the moisture *tier_index*."""
    if tier_index < len(_MOISTURE_TIERS):
        return _MOISTURE_TIERS[tier_index][0]
    return 1_000_000.0


def _moisture_tier_lower(tier_index: int) -> float:
    """Return the effective lower bound of the moisture *tier_index*."""
    if tier_index == 0:
        return -1_000_000.0
    return _MOISTURE_TIERS[tier_index - 1][0]


def _apply_temp_hysteresis(value: float, current: int | None) -> int:
    """Return the new temperature tier, applying hysteresis if a tier is established.

    When *current* is None (startup), returns the raw tier for *value*.
    Otherwise, only switches tier when the value exceeds the boundary by
    ``_TEMP_HYSTERESIS`` in the direction of the new tier.
    """
    raw = _temp_tier_for(value)
    if current is None:
        return raw
    if raw == current:
        return current
    if raw < current:
        # Moving down: must drop below current tier's lower bound by hysteresis amount.
        # Current tier lower bound = _temp_tier_lower(current).
        threshold = _temp_tier_lower(current) - _TEMP_HYSTERESIS
        return raw if value <= threshold else current
    # raw > current — moving up: must exceed current tier's upper bound by hysteresis.
    threshold = _temp_tier_upper(current) + _TEMP_HYSTERESIS
    return raw if value >= threshold else current


def _apply_moisture_hysteresis(value: float, current: int | None) -> int:
    """Return the new moisture tier, applying hysteresis if a tier is established."""
    raw = _moisture_tier_for(value)
    if current is None:
        return raw
    if raw == current:
        return current
    if raw < current:
        threshold = _moisture_tier_lower(current) - _DP_HYSTERESIS
        return raw if value <= threshold else current
    threshold = _moisture_tier_upper(current) + _DP_HYSTERESIS
    return raw if value >= threshold else current


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(
    app_temp: float | None,
    dewpoint: float | None = None,
    out_temp: float | None = None,
    heatindex: float | None = None,
    windchill: float | None = None,
) -> str | None:
    """Return the temperature-comfort descriptor.

    All inputs must be in °F.  Returns ``None`` when *app_temp* is ``None``
    (temperature axis unavailable — caller should omit this component).

    Stability mechanisms applied (ADR-044 §8):
    - Hysteresis on tier transitions (±2 °F).
    - 5-minute hold cache: if smoothed inputs produce a different result, the
      cached value is kept until the hold expires.

    Args:
        app_temp:  Apparent temperature (feels-like) in °F.
        dewpoint:  Dewpoint in °F (may be ``None``).
        out_temp:  Dry-bulb temperature in °F, used for near-saturation check.
        heatindex: Heat index in °F, used for NWS danger escalation.
        windchill: Wind chill in °F, used for NWS danger escalation.

    Returns:
        Composite descriptor string, or ``None``.
    """
    global _current_temp_tier, _current_moisture_tier, _cached_result, _cache_time

    if app_temp is None:
        return None

    # ------------------------------------------------------------------
    # Step 1: NWS danger escalation (checked FIRST, overrides everything)
    # ------------------------------------------------------------------
    danger_label: str | None = None
    if heatindex is not None:
        if heatindex >= _HI_EXTREME_DANGER:
            danger_label = "Extreme Danger Heat"
        elif heatindex >= _HI_DANGER:
            danger_label = "Dangerous Heat"
    if danger_label is None and windchill is not None:
        if windchill <= _WC_EXTREME_DANGER:
            danger_label = "Extreme Danger Cold"
        elif windchill <= _WC_DANGER:
            danger_label = "Dangerous Cold"

    # ------------------------------------------------------------------
    # Step 2: Determine temperature tier with hysteresis
    # ------------------------------------------------------------------
    new_temp_tier = _apply_temp_hysteresis(app_temp, _current_temp_tier)

    # ------------------------------------------------------------------
    # Step 3: Determine moisture tier with hysteresis (when dewpoint available)
    # ------------------------------------------------------------------
    new_moisture_tier: int | None
    if dewpoint is not None:
        new_moisture_tier = _apply_moisture_hysteresis(dewpoint, _current_moisture_tier)
    else:
        new_moisture_tier = _current_moisture_tier  # keep existing if no data

    # ------------------------------------------------------------------
    # Step 4: Compose the base label
    # ------------------------------------------------------------------
    if danger_label is not None:
        base = danger_label
    else:
        t_label = _temp_label(new_temp_tier)

        # Cold-temperature suppression: tiers 1-5 (indices 0-4) suppress moisture.
        is_cold = new_temp_tier < _COLD_TIER_COUNT
        if is_cold or dewpoint is None or new_moisture_tier is None:
            base = t_label
        elif out_temp is not None and round(out_temp - dewpoint, 1) <= 0.0:
            # Full saturation: outTemp at or below dewpoint — air cannot hold
            # more water vapour.  Overrides all moisture tier labels.
            base = f"{t_label} and Fully Saturated"
        else:
            modifier = _moisture_modifier(new_moisture_tier)
            if modifier:
                base = f"{t_label} and {modifier}"
            else:
                base = t_label

    # ------------------------------------------------------------------
    # Step 5: Hold-time cache
    # If a cached result exists and its hold period hasn't expired, return
    # the cached value when the new result would differ.
    # ------------------------------------------------------------------
    now = time.monotonic()
    hold_expired = (now - _cache_time) >= _HOLD_SECONDS

    if _cached_result is not None and not hold_expired and base != _cached_result:
        # New result differs but hold has not expired — keep cached value.
        return _cached_result

    # ------------------------------------------------------------------
    # Step 6: Update module state and cache
    # ------------------------------------------------------------------
    _current_temp_tier = new_temp_tier
    if new_moisture_tier is not None:
        _current_moisture_tier = new_moisture_tier
    _cached_result = base
    _cache_time = now

    return base


def configure(archive_interval: int) -> None:
    """Set hold time based on archive interval.

    Called once at startup from __main__.py after load_station_metadata().
    The hold time scales to 5× the archive interval so that a station with
    60-second archives holds for 300 s and a station with 300-second archives
    holds for 1500 s.
    """
    global _HOLD_SECONDS  # noqa: PLW0603
    _HOLD_SECONDS = float(archive_interval) * 5.0


def reset() -> None:
    """Clear hysteresis state and hold cache.  For test isolation only."""
    global _current_temp_tier, _current_moisture_tier, _cached_result, _cache_time, _HOLD_SECONDS
    _current_temp_tier = None
    _current_moisture_tier = None
    _cached_result = None
    _cache_time = 0.0
    _HOLD_SECONDS = 300.0
