"""Unit conversion registry and convert() function.

All conversion factors are taken verbatim from weewx 5.3.1 source
(weewx/units.py) to ensure identical numeric results.

The registry is a flat dict keyed by (from_unit, to_unit) → callable.
Identity conversions (same unit) are short-circuited in convert() itself
and therefore not present in the registry.
"""

from __future__ import annotations

from typing import Callable

# ---------------------------------------------------------------------------
# Physical constants (from weewx source)
# ---------------------------------------------------------------------------

INHG_PER_MBAR: float = 0.0295299875
MM_PER_INCH: float = 25.4
CM_PER_INCH: float = 2.54
METER_PER_MILE: float = 1609.34
METER_PER_FOOT: float = 1609.34 / 5280.0

# ---------------------------------------------------------------------------
# Conversion registry
# ---------------------------------------------------------------------------

_CONVERSIONS: dict[tuple[str, str], Callable[[float], float]] = {
    # --- Temperature (non-linear) ---
    ("degree_F", "degree_C"): lambda x: (x - 32.0) / 1.8,
    ("degree_C", "degree_F"): lambda x: x * 1.8 + 32.0,
    ("degree_F", "degree_K"): lambda x: (x - 32.0) / 1.8 + 273.15,
    ("degree_K", "degree_F"): lambda x: (x - 273.15) * 1.8 + 32.0,
    ("degree_C", "degree_K"): lambda x: x + 273.15,
    ("degree_K", "degree_C"): lambda x: x - 273.15,
    ("degree_F", "degree_E"): lambda x: (7.0 * x - 80.0) / 9.0,
    ("degree_E", "degree_F"): lambda x: (9.0 * x + 80.0) / 7.0,
    ("degree_C", "degree_E"): lambda x: (7.0 / 5.0) * x + 16.0,
    ("degree_E", "degree_C"): lambda x: (x - 16.0) * 5.0 / 7.0,
    # degree_K ↔ degree_E (via degree_C as intermediate is cleaner, but we
    # chain through two registry lookups in convert() only if needed; adding
    # direct entries avoids that complexity)
    ("degree_K", "degree_E"): lambda x: (7.0 / 5.0) * (x - 273.15) + 16.0,
    ("degree_E", "degree_K"): lambda x: (x - 16.0) * 5.0 / 7.0 + 273.15,

    # --- Speed ---
    ("mile_per_hour", "km_per_hour"):      lambda x: x * 1.609344,
    ("mile_per_hour", "knot"):             lambda x: x * 0.868976242,
    ("mile_per_hour", "meter_per_second"): lambda x: x * 0.44704,
    ("km_per_hour", "mile_per_hour"):      lambda x: x * 1000.0 / 1609.34,
    ("km_per_hour", "knot"):               lambda x: x * 0.539956803,
    ("km_per_hour", "meter_per_second"):   lambda x: x * 0.277777778,
    ("knot", "mile_per_hour"):             lambda x: x * 1.15077945,
    ("knot", "km_per_hour"):               lambda x: x * 1.85200,
    ("knot", "meter_per_second"):          lambda x: x * 0.514444444,
    ("meter_per_second", "mile_per_hour"): lambda x: x * 3600.0 / 1609.34,
    ("meter_per_second", "km_per_hour"):   lambda x: x * 3.6,
    ("meter_per_second", "knot"):          lambda x: x * 1.94384449,

    # --- Speed2 (acceleration; same factors, _2 suffix units) ---
    ("mile_per_hour2", "km_per_hour2"):      lambda x: x * 1.609344,
    ("mile_per_hour2", "knot2"):             lambda x: x * 0.868976242,
    ("mile_per_hour2", "meter_per_second2"): lambda x: x * 0.44704,
    ("km_per_hour2", "mile_per_hour2"):      lambda x: x * 1000.0 / 1609.34,
    ("km_per_hour2", "knot2"):               lambda x: x * 0.539956803,
    ("km_per_hour2", "meter_per_second2"):   lambda x: x * 0.277777778,
    ("knot2", "mile_per_hour2"):             lambda x: x * 1.15077945,
    ("knot2", "km_per_hour2"):              lambda x: x * 1.85200,
    ("knot2", "meter_per_second2"):         lambda x: x * 0.514444444,
    ("meter_per_second2", "mile_per_hour2"): lambda x: x * 3600.0 / 1609.34,
    ("meter_per_second2", "km_per_hour2"):   lambda x: x * 3.6,
    ("meter_per_second2", "knot2"):          lambda x: x * 1.94384449,

    # --- Pressure ---
    ("inHg", "mbar"): lambda x: x / INHG_PER_MBAR,
    ("inHg", "hPa"):  lambda x: x / INHG_PER_MBAR,          # mbar == hPa
    ("inHg", "kPa"):  lambda x: x / INHG_PER_MBAR / 10.0,
    ("mbar", "inHg"): lambda x: x * INHG_PER_MBAR,
    ("mbar", "hPa"):  lambda x: x,                            # identity
    ("mbar", "kPa"):  lambda x: x / 10.0,
    ("hPa",  "inHg"): lambda x: x * INHG_PER_MBAR,
    ("hPa",  "mbar"): lambda x: x,                            # identity
    ("hPa",  "kPa"):  lambda x: x / 10.0,
    ("kPa",  "inHg"): lambda x: x * INHG_PER_MBAR * 10.0,
    ("kPa",  "mbar"): lambda x: x * 10.0,
    ("kPa",  "hPa"):  lambda x: x * 10.0,

    # --- Pressure rate (same factors, _per_hour suffix) ---
    ("inHg_per_hour", "mbar_per_hour"): lambda x: x / INHG_PER_MBAR,
    ("inHg_per_hour", "hPa_per_hour"):  lambda x: x / INHG_PER_MBAR,
    ("inHg_per_hour", "kPa_per_hour"):  lambda x: x / INHG_PER_MBAR / 10.0,
    ("mbar_per_hour", "inHg_per_hour"): lambda x: x * INHG_PER_MBAR,
    ("mbar_per_hour", "hPa_per_hour"):  lambda x: x,
    ("mbar_per_hour", "kPa_per_hour"):  lambda x: x / 10.0,
    ("hPa_per_hour",  "inHg_per_hour"): lambda x: x * INHG_PER_MBAR,
    ("hPa_per_hour",  "mbar_per_hour"): lambda x: x,
    ("hPa_per_hour",  "kPa_per_hour"):  lambda x: x / 10.0,
    ("kPa_per_hour",  "inHg_per_hour"): lambda x: x * INHG_PER_MBAR * 10.0,
    ("kPa_per_hour",  "mbar_per_hour"): lambda x: x * 10.0,
    ("kPa_per_hour",  "hPa_per_hour"):  lambda x: x * 10.0,

    # --- Rain ---
    ("inch", "cm"): lambda x: x * CM_PER_INCH,
    ("inch", "mm"): lambda x: x * MM_PER_INCH,
    ("cm", "inch"): lambda x: x / CM_PER_INCH,
    ("cm", "mm"):   lambda x: x * 10.0,
    ("mm", "inch"): lambda x: x / MM_PER_INCH,
    ("mm", "cm"):   lambda x: x * 0.10,

    # --- Rain rate (same factors, _per_hour suffix) ---
    ("inch_per_hour", "cm_per_hour"): lambda x: x * CM_PER_INCH,
    ("inch_per_hour", "mm_per_hour"): lambda x: x * MM_PER_INCH,
    ("cm_per_hour", "inch_per_hour"): lambda x: x / CM_PER_INCH,
    ("cm_per_hour", "mm_per_hour"):   lambda x: x * 10.0,
    ("mm_per_hour", "inch_per_hour"): lambda x: x / MM_PER_INCH,
    ("mm_per_hour", "cm_per_hour"):   lambda x: x * 0.10,

    # --- Altitude ---
    ("foot",  "meter"): lambda x: x * METER_PER_FOOT,
    ("meter", "foot"):  lambda x: x / METER_PER_FOOT,

    # --- Distance ---
    ("mile", "km"): lambda x: x * 1.609344,
    ("km", "mile"): lambda x: x * 0.621371192,
}


def convert(value: float | None, from_unit: str, to_unit: str) -> float | None:
    """Convert *value* from *from_unit* to *to_unit*.

    Returns None if *value* is None.
    Returns *value* unchanged when *from_unit* == *to_unit* (identity).
    Raises ValueError if no conversion path exists for the (from, to) pair.
    """
    if value is None:
        return None
    if from_unit == to_unit:
        return value
    fn = _CONVERSIONS.get((from_unit, to_unit))
    if fn is None:
        raise ValueError(
            f"No conversion from '{from_unit}' to '{to_unit}'"
        )
    return fn(value)
