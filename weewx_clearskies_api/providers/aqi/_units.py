"""µg/m³ → ppm gas conversion, PPB → ppm direct conversion, EPA AQI category band table.

Conversion formula per canonical-data-model §4.2 footnote:
    ppm = µg/m³ × 24.45 / molecular_weight
where 24.45 L/mol is the molar volume of an ideal gas at 25°C and 1 atm.

PPB → ppm direct conversion (for providers that supply valuePPB directly, e.g. Aeris):
    ppm = ppb / 1000

EPA AQI category breakpoints per canonical-data-model §3.8 (canonical):
    0-50      → Good
    51-100    → Moderate
    101-150   → Unhealthy for Sensitive Groups
    151-200   → Unhealthy
    201-300   → Very Unhealthy
    301-500   → Hazardous

Tables are static; molecular weights and EPA bands are constants of nature
+ EPA regulation respectively (not provider-specific).
"""

from __future__ import annotations

# Molar volume at 25°C / 1 atm.  Used by µg/m³ → ppm conversion.
_MOLAR_VOLUME = 24.45  # L/mol

# Molecular weights for the four gases canonical stores in ppm (group_fraction).
# Particulates (PM2.5, PM10) stay in µg/m³ (group_concentration) — no conversion.
_MOLECULAR_WEIGHTS_G_PER_MOL: dict[str, float] = {
    "O3":  48.00,
    "NO2": 46.01,
    "SO2": 64.07,
    "CO":  28.01,
}


def ugm3_to_ppm(ugm3: float | None, *, pollutant: str) -> float | None:
    """Convert µg/m³ concentration to ppm for the given gas.

    Args:
        ugm3: concentration in µg/m³ (or None).
        pollutant: canonical pollutant id ("O3", "NO2", "SO2", "CO").

    Returns:
        ppm value (None propagates).

    Raises:
        KeyError: if pollutant is not in the conversion table.
    """
    if ugm3 is None:
        return None
    mw = _MOLECULAR_WEIGHTS_G_PER_MOL[pollutant]
    return ugm3 * _MOLAR_VOLUME / mw


# EPA AQI category breakpoints (upper bounds, inclusive).
# Bisect-by-upper-bound dispatch: aqi value <= upper → that category.
# Order matters — list MUST be sorted by upper bound ascending.
_EPA_CATEGORY_BANDS: list[tuple[int, str]] = [
    (50,  "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (500, "Hazardous"),
]


def ppb_to_ppm(ppb: float | None) -> float | None:
    """Convert ppb (parts per billion) to ppm (parts per million).

    Used by the Aeris AQI provider which returns gas concentrations in valuePPB
    directly (O3, NO2, SO2, CO).  Distinct from ugm3_to_ppm — no molar volume
    or molecular weight needed; the conversion is purely a 1000x scale factor.

    Args:
        ppb: concentration in ppb (or None).

    Returns:
        ppm value (None propagates).  ppm = ppb / 1000.
    """
    if ppb is None:
        return None
    return ppb / 1000.0


def epa_category(aqi: int | float | None) -> str | None:
    """Map a 0–500 EPA AQI value to its category name.

    Args:
        aqi: AQI value (or None).

    Returns:
        EPA category name (canonical spelling per canonical §3.8) or None.
        Values > 500 fall into "Hazardous" (max band) for safety.
    """
    if aqi is None:
        return None
    for upper, name in _EPA_CATEGORY_BANDS:
        if aqi <= upper:
            return name
    # Above 500 — cap at "Hazardous" (top band) rather than raising. Spec is
    # 0-500 but provider-side bugs producing 501+ shouldn't crash us.
    return "Hazardous"
