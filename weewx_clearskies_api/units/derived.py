"""Derived weather fields computed by the BFF.

These are computed server-side so the dashboard has zero unit or threshold
knowledge.  Both functions convert their input to the WMO/NWS canonical
unit before comparing against fixed thresholds.
"""

from __future__ import annotations

from .conversion import convert

# ---------------------------------------------------------------------------
# Beaufort scale — WMO standard thresholds in m/s
# Each entry: (upper_bound_exclusive, beaufort_number). Labels are no longer
# stored here (I18N T3.3) — they resolve from the locale file via
# i18n.t("beaufort.<number>", locale).
# ---------------------------------------------------------------------------

_BEAUFORT_SCALE: list[tuple[float, int]] = [
    (0.5,  0),
    (1.6,  1),
    (3.4,  2),
    (5.5,  3),
    (8.0,  4),
    (10.8, 5),
    (13.9, 6),
    (17.2, 7),
    (20.8, 8),
    (24.5, 9),
    (28.5, 10),
    (32.7, 11),
    (float("inf"), 12),
]


def beaufort(
    wind_speed: float,
    source_unit: str,
    locale: str | None = None,
) -> dict[str, object]:
    """Compute Beaufort number and label from wind speed.

    Args:
        wind_speed:  Wind speed value in source_unit.
        source_unit: Unit of wind_speed (e.g. "mile_per_hour").
        locale:      Optional locale code (I18N T3.3). When omitted, the
                     label resolves via the i18n module's active locale
                     (defaults to English).

    Returns:
        {"value": int, "label": str, "formatted": str}
    """
    from weewx_clearskies_api import i18n  # noqa: PLC0415

    # Convert to m/s for threshold comparison (skip convert() for identity).
    if source_unit == "meter_per_second":
        mps = wind_speed
    else:
        converted = convert(wind_speed, source_unit, "meter_per_second")
        # convert() only returns None when the input is None; wind_speed is a
        # float at this point, so the cast is safe.
        assert converted is not None
        mps = converted

    for threshold, number in _BEAUFORT_SCALE:
        if mps < threshold:
            label = i18n.t(f"beaufort.{number}", locale)
            return {"value": number, "label": label, "formatted": str(number)}

    # Unreachable: the final entry has threshold float("inf"), but kept for
    # type-checker completeness.
    label = i18n.t("beaufort.12", locale)
    return {"value": 12, "label": label, "formatted": "12"}  # pragma: no cover


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
