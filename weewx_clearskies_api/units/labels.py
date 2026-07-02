"""Default display labels and format strings per unit.

These are the weewx defaults; operator config (skin.conf [Units][[Labels]]
and [[StringFormats]]) can override them via the helper functions below.

I18N (T3.2): both ``get_label()`` and ``format_value()`` accept an optional
``locale`` parameter. When omitted (``None``), behavior is unchanged from
before locale support existed — no locale module is imported, no babel
formatting occurs. Passing a locale is opt-in and callers that don't yet
know about locales (T3.5 wires the operator's configured locale through at
startup) are unaffected.
"""

from __future__ import annotations

import re

# Extracts the decimal-place count from a %-style format string, e.g.
# "%.1f" -> "1", "%03.0f" -> "0", "%.5f" -> "5".
_FORMAT_DECIMALS_RE = re.compile(r"%[-+0# ]*\d*\.(\d+)f")

DEFAULT_LABELS: dict[str, str] = {
    # Temperature
    "degree_F": "°F",
    "degree_C": "°C",
    "degree_K": "°K",
    "degree_E": "°E",
    # Speed
    "mile_per_hour":      " mph",
    "km_per_hour":        " km/h",
    "knot":               " knots",
    "meter_per_second":   " m/s",
    # Speed2
    "mile_per_hour2":     " mph",
    "km_per_hour2":       " km/h",
    "knot2":              " knots",
    "meter_per_second2":  " m/s",
    # Pressure
    "inHg":               " inHg",
    "mbar":               " mbar",
    "hPa":                " hPa",
    "kPa":                " kPa",
    # Pressure rate
    "inHg_per_hour":      " inHg/h",
    "mbar_per_hour":      " mbar/h",
    "hPa_per_hour":       " hPa/h",
    "kPa_per_hour":       " kPa/h",
    # Rain
    "inch":               " in",
    "cm":                 " cm",
    "mm":                 " mm",
    # Rain rate
    "inch_per_hour":      " in/h",
    "cm_per_hour":        " cm/h",
    "mm_per_hour":        " mm/h",
    # Altitude / distance
    "foot":               " feet",
    "meter":              " m",
    "mile":               " miles",
    "km":                 " km",
    # Direction
    "degree_compass":     "°",
    # Radiation
    "watt_per_meter_squared": " W/m²",
    # Misc
    "percent":            "%",
    "centibar":           " cb",
    "volt":               " V",
    "uv_index":           "",
}

DEFAULT_FORMATS: dict[str, str] = {
    # Temperature
    "degree_F": "%.1f",
    "degree_C": "%.1f",
    "degree_K": "%.1f",
    "degree_E": "%.1f",
    # Speed
    "mile_per_hour":      "%.0f",
    "km_per_hour":        "%.0f",
    "knot":               "%.0f",
    "meter_per_second":   "%.1f",
    # Speed2
    "mile_per_hour2":     "%.1f",
    "km_per_hour2":       "%.1f",
    "knot2":              "%.1f",
    "meter_per_second2":  "%.1f",
    # Pressure
    "inHg":               "%.2f",
    "mbar":               "%.1f",
    "hPa":                "%.1f",
    "kPa":                "%.2f",
    # Pressure rate
    "inHg_per_hour":      "%.5f",
    "mbar_per_hour":      "%.4f",
    "hPa_per_hour":       "%.3f",
    "kPa_per_hour":       "%.4f",
    # Rain
    "inch":               "%.2f",
    "cm":                 "%.2f",
    "mm":                 "%.1f",
    # Rain rate
    "inch_per_hour":      "%.2f",
    "cm_per_hour":        "%.2f",
    "mm_per_hour":        "%.1f",
    # Altitude / distance
    "foot":               "%.0f",
    "meter":              "%.0f",
    "mile":               "%.1f",
    "km":                 "%.1f",
    # Direction
    "degree_compass":     "%03.0f",
    # Radiation
    "watt_per_meter_squared": "%.1f",
    # Misc
    "percent":            "%.0f",
    "centibar":           "%.0f",
    "volt":               "%.1f",
    "uv_index":           "%.1f",
}


def get_label(
    unit: str,
    overrides: dict[str, str] | None = None,
    locale: str | None = None,
) -> str:
    """Return the display label for *unit*.

    Resolution order (I18N T3.2):
      1. *overrides* (operator [[Labels]] config) — operator always wins.
      2. *locale* (``unit_labels.<unit>`` in the locale file) — locale-specific
         default, when *locale* is provided and a translation exists.
      3. ``DEFAULT_LABELS`` — built-in English fallback.

    *locale* is optional and defaults to ``None``, which preserves the
    pre-i18n behavior exactly (no locale module import, no lookup).
    """
    if overrides and unit in overrides:
        return overrides[unit]
    if locale:
        from weewx_clearskies_api import i18n  # noqa: PLC0415

        key = f"unit_labels.{unit}"
        translated = i18n.t(key, locale)
        if translated != key:
            return translated
    return DEFAULT_LABELS.get(unit, "")


def format_value(
    value: float,
    unit: str,
    overrides: dict[str, str] | None = None,
    locale: str | None = None,
) -> str:
    """Format *value* for display using the format string for *unit*.

    *overrides* maps unit → format string (from operator [[StringFormats]]).
    Falls back to DEFAULT_FORMATS, then to "%.1f" if the unit is unknown.

    When *locale* is provided (I18N T3.2), the decimal-place count is taken
    from the resolved %-style format string but the actual rendering uses
    ``i18n.format_number()`` (babel) so the decimal separator, digit
    grouping, etc. match the locale's conventions. When *locale* is
    ``None`` (default), behavior is unchanged: plain ``%`` formatting.
    """
    fmt = (overrides or {}).get(unit) or DEFAULT_FORMATS.get(unit, "%.1f")
    if locale:
        from weewx_clearskies_api import i18n  # noqa: PLC0415

        match = _FORMAT_DECIMALS_RE.search(fmt)
        decimals = int(match.group(1)) if match else 1
        return i18n.format_number(value, decimals, locale)
    return fmt % value
