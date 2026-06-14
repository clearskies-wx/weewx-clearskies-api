"""Derived weather fields computed by the BFF.

These are computed server-side so the dashboard has zero unit or threshold
knowledge.  Both functions convert their input to the WMO/NWS canonical
unit before comparing against fixed thresholds.
"""

from __future__ import annotations

from .conversion import convert

# ---------------------------------------------------------------------------
# Beaufort scale — WMO standard thresholds in m/s
# Each entry: (upper_bound_exclusive, beaufort_number, label)
# ---------------------------------------------------------------------------

_BEAUFORT_SCALE: list[tuple[float, int, str]] = [
    (0.5,  0,  "Calm"),
    (1.6,  1,  "Very Light Breeze"),
    (3.4,  2,  "Light breeze"),
    (5.5,  3,  "Gentle breeze"),
    (8.0,  4,  "Moderate breeze"),
    (10.8, 5,  "Fresh breeze"),
    (13.9, 6,  "Strong breeze"),
    (17.2, 7,  "Near gale"),
    (20.8, 8,  "Gale"),
    (24.5, 9,  "Strong gale"),
    (28.5, 10, "Storm"),
    (32.7, 11, "Violent storm"),
    (float("inf"), 12, "Hurricane"),
]


def beaufort(wind_speed: float, source_unit: str) -> dict[str, object]:
    """Compute Beaufort number and label from wind speed.

    Args:
        wind_speed:  Wind speed value in source_unit.
        source_unit: Unit of wind_speed (e.g. "mile_per_hour").

    Returns:
        {"value": int, "label": str, "formatted": str}
    """
    # Convert to m/s for threshold comparison (skip convert() for identity).
    if source_unit == "meter_per_second":
        mps = wind_speed
    else:
        converted = convert(wind_speed, source_unit, "meter_per_second")
        # convert() only returns None when the input is None; wind_speed is a
        # float at this point, so the cast is safe.
        assert converted is not None
        mps = converted

    for threshold, number, label in _BEAUFORT_SCALE:
        if mps < threshold:
            return {"value": number, "label": label, "formatted": str(number)}

    # Unreachable: the final entry has threshold float("inf"), but kept for
    # type-checker completeness.
    return {"value": 12, "label": "Hurricane", "formatted": "12"}  # pragma: no cover


# ---------------------------------------------------------------------------
# Comfort index — NWS thresholds in °F
# ---------------------------------------------------------------------------

# NWS uses ≤50 °F for wind-chill applicability and ≥80 °F for heat index.
_WIND_CHILL_THRESHOLD_F: float = 50.0
_HEAT_INDEX_THRESHOLD_F: float = 80.0


def comfort_index(temp_value: float, source_unit: str) -> str:
    """Determine which comfort metric applies given an outdoor temperature.

    Uses NWS thresholds: wind chill when ≤50 °F, heat index when ≥80 °F.

    Args:
        temp_value:  Temperature in source_unit.
        source_unit: Unit of temp_value (e.g. "degree_C").

    Returns:
        "windChill", "heatIndex", or "none"
    """
    if source_unit == "degree_F":
        temp_f = temp_value
    else:
        converted = convert(temp_value, source_unit, "degree_F")
        assert converted is not None
        temp_f = converted

    if temp_f <= _WIND_CHILL_THRESHOLD_F:
        return "windChill"
    if temp_f >= _HEAT_INDEX_THRESHOLD_F:
        return "heatIndex"
    return "none"
