"""SWAN supplement processor (Phase 3, T3.1).

Applies targeted supplements to SWAN nearshore wave data, per
API-MANUAL.md §17 "SWAN nearshore model". Registered against the
surf scoring pipeline: runs after SwanProvider.fetch(), before
``surf_scorer.py``.

Four supplements were originally defined (numbered per API-MANUAL.md §17);
two remain active. Numbering is preserved across removals because
ADR-095, ADR-093 Amendment 2, and API-MANUAL §17 all cite it — do not
renumber the survivors.

1. Breaker index correction (Battjes 1974 γ tuning) — replaces SWAN's
   constant γ = 0.73 with a site-specific value derived from the Iribarren
   number, then re-caps wave height at the breaking depth.
2. Coastal structure transmission/reflection effects — REMOVED per
   ADR-095; structure effects are now handled natively by the SWAN
   OBSTACLE command during wave propagation (T2.3).
3. Sub-grid spatial interpolation (bilinear) — refines the SWAN grid-cell
   value to the exact spot coordinates. Runs first in ``apply_supplements``
   because it establishes the wave height baseline Supplement 1 corrects.
4. Topographic wave focusing/sheltering — REMOVED per ADR-093 Amendment 2
   §5b (2026-07-25). The multipliers (point break x1.1, headland x1.2,
   bay break x0.9, straight beach x1.0) stood in for refraction SWAN now
   computes; once L2 existed at 100 m it began computing that refraction
   itself, so the multipliers had been double-counting since nesting
   landed. The operator's topographic classification is retained in spot
   config — its job is now the L3 enable trigger (PROVIDER-MANUAL §14.15),
   not a wave-height adjustment.

No-op when wave data is None or empty — passes through unmodified because
``apply_supplements`` is simply not called in that case.

**What is NOT supplemented:** Shoaling, refraction, bottom friction,
wave-current interaction. SWAN computes these with its own bathymetry
and RTOFS currents; re-running them here would duplicate SWAN's work
without improving it. Do not add ``calculate_shoaling_coefficient`` or
``calculate_refraction_coefficient`` functions to this module.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Physics constants (module-level, with source citations per API-MANUAL.md §17)
# ---------------------------------------------------------------------------

#: Gravitational acceleration, m/s^2. Used in the deep-water wavelength formula.
G: float = 9.81

#: Breaker index (gamma) physical bounds from literature (Battjes 1974).
GAMMA_MIN: float = 0.5
GAMMA_MAX: float = 1.4


# ---------------------------------------------------------------------------
# Supplement 1 — Breaker index correction (Battjes 1974)
# ---------------------------------------------------------------------------


def compute_iribarren(slope: float, wave_height: float, wave_period: float) -> float:
    """Return the Iribarren number xi = tan(alpha) / sqrt(H0 / L0).

    ``slope`` is tan(alpha), the nearshore bottom slope. ``wave_height`` (H0)
    is the deep-water significant wave height in meters, ``wave_period`` (T)
    the peak period in seconds. L0 = g * T^2 / (2*pi) is the deep-water
    wavelength.

    Returns 0.0 (a degenerate/invalid value) when height or period is
    non-positive, so callers can detect and skip the correction rather than
    hitting a ZeroDivisionError.
    """
    if wave_height <= 0 or wave_period <= 0:
        return 0.0
    deep_water_wavelength = G * wave_period**2 / (2 * math.pi)
    return slope / math.sqrt(wave_height / deep_water_wavelength)


def compute_breaker_gamma(iribarren: float) -> float:
    """Return the Battjes (1974) breaker index gamma, clamped to [GAMMA_MIN, GAMMA_MAX].

    Formula: gamma = 1.06 + 0.14 * ln(xi).

    A non-positive Iribarren number is physically degenerate (ln undefined);
    it is logged as a warning and treated as clamped to GAMMA_MIN.
    """
    if iribarren <= 0:
        logger.warning(
            "compute_breaker_gamma: non-positive Iribarren number (%.6f); "
            "clamping gamma to GAMMA_MIN=%.2f",
            iribarren,
            GAMMA_MIN,
        )
        return GAMMA_MIN

    gamma = 1.06 + 0.14 * math.log(iribarren)

    if gamma < GAMMA_MIN:
        logger.warning(
            "compute_breaker_gamma: gamma %.4f below GAMMA_MIN; clamping to %.2f",
            gamma,
            GAMMA_MIN,
        )
        return GAMMA_MIN
    if gamma > GAMMA_MAX:
        logger.warning(
            "compute_breaker_gamma: gamma %.4f above GAMMA_MAX; clamping to %.2f",
            gamma,
            GAMMA_MAX,
        )
        return GAMMA_MAX
    return gamma


def apply_breaker_correction(
    wave_height: float,
    wave_period: float,
    beach_slope: float,
    depth_at_break: float,
) -> tuple[float, float]:
    """Recompute breaking-wave height cap using a site-specific gamma.

    Returns ``(corrected_height, gamma)``. When ``wave_height`` is within the
    site-specific maximum (``gamma * abs(depth_at_break)``), it is returned
    unchanged; otherwise it is capped at that maximum.
    """
    iribarren = compute_iribarren(beach_slope, wave_height, wave_period)
    gamma = compute_breaker_gamma(iribarren)
    h_max = gamma * abs(depth_at_break)
    corrected_height = wave_height if wave_height <= h_max else h_max
    return corrected_height, gamma


# ---------------------------------------------------------------------------
# Supplement 3 — Sub-grid spatial interpolation
# ---------------------------------------------------------------------------


def _find_bracket(values: list[float], target: float) -> tuple[int, int]:
    """Return indices (i0, i1) in an ascending or descending axis bracketing target.

    When target is outside the axis range, both indices collapse to the
    nearest edge (i.e. the interpolation weight becomes 0 or 1 — an edge
    clamp rather than an extrapolation).
    """
    n = len(values)
    if n == 0:
        raise ValueError("bilinear_interpolate: empty coordinate axis")
    if n == 1:
        return 0, 0

    for i in range(n - 1):
        lo, hi = values[i], values[i + 1]
        if (lo <= target <= hi) or (hi <= target <= lo):
            return i, i + 1

    # Outside the range entirely — clamp to the nearest edge.
    if target <= values[0]:
        return 0, 0
    return n - 1, n - 1


def bilinear_interpolate(
    grid_data: list[list[float]],
    grid_lats: list[float],
    grid_lons: list[float],
    target_lat: float,
    target_lon: float,
) -> float:
    """Standard bilinear interpolation over a 2D nearshore wave grid.

    ``grid_data`` is indexed ``grid_data[lat_index][lon_index]``. Finds the
    four surrounding grid nodes and computes a weighted average based on the
    target's fractional position within that cell. A target that lands
    exactly on a node returns that node's value exactly.
    """
    i0, i1 = _find_bracket(grid_lats, target_lat)
    j0, j1 = _find_bracket(grid_lons, target_lon)

    lat0, lat1 = grid_lats[i0], grid_lats[i1]
    lon0, lon1 = grid_lons[j0], grid_lons[j1]

    q11 = grid_data[i0][j0]
    q12 = grid_data[i0][j1]
    q21 = grid_data[i1][j0]
    q22 = grid_data[i1][j1]

    tlat = 0.0 if lat1 == lat0 else (target_lat - lat0) / (lat1 - lat0)
    tlon = 0.0 if lon1 == lon0 else (target_lon - lon0) / (lon1 - lon0)

    top = q11 + (q12 - q11) * tlon
    bottom = q21 + (q22 - q21) * tlon
    return top + (bottom - top) * tlat


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def apply_supplements(
    wave_data: dict[str, Any] | None,
    spot_config: Any,
    spot_lat: float,
    spot_lon: float,
) -> dict[str, Any] | None:
    """Apply the surviving SWAN supplements and return corrected wave data.

    Supplement 2 (coastal structure transmission/reflection effects) was
    removed per ADR-095; structure effects are now handled natively by the
    SWAN OBSTACLE command during wave propagation. Supplement 4 (topographic
    focusing/sheltering) was removed per ADR-093 Amendment 2 §5b
    (2026-07-25); its multipliers double-counted refraction SWAN's nested
    grids (L2) now compute directly.

    Processing order: interpolation (3) -> breaker correction (1).

    Input ``wave_data`` is a dict with keys ``wave_height`` (meters, SWAN Hsig),
    ``wave_period`` (seconds), ``wave_direction`` (degrees true north), and
    optionally ``grid_data``, ``grid_lats``, ``grid_lons`` for the bilinear
    interpolation supplement.

    Returns a dict with keys ``wave_height``, ``wave_period``,
    ``wave_direction``, ``breaker_gamma``, ``structure_applied`` (bool), and
    ``supplements_applied`` (list of the supplement names that were actually
    applied, in application order).

    Returns None when ``wave_data`` is None or empty.
    """
    if not wave_data:
        return None

    wave_height = wave_data.get("wave_height")
    wave_period = wave_data.get("wave_period")
    wave_direction = wave_data.get("wave_direction")

    supplements_applied: list[str] = []
    breaker_gamma: float | None = None
    structure_applied = False

    # Supplement 3 — sub-grid interpolation. Runs first: it establishes the
    # wave-height baseline the remaining supplements correct.
    grid_data = wave_data.get("grid_data")
    grid_lats = wave_data.get("grid_lats")
    grid_lons = wave_data.get("grid_lons")
    if grid_data is not None and grid_lats is not None and grid_lons is not None:
        try:
            wave_height = bilinear_interpolate(
                grid_data, grid_lats, grid_lons, spot_lat, spot_lon
            )
            supplements_applied.append("interpolation")
        except Exception:  # noqa: BLE001
            logger.exception(
                "apply_supplements: bilinear interpolation failed; "
                "falling back to scalar wave_height"
            )

    # Supplement 1 — breaker index correction. Requires a configured beach
    # slope and a bathymetric profile; skipped (not an error) otherwise.
    beach_slope = getattr(spot_config, "beach_slope", None)
    bathymetric_profile = getattr(spot_config, "bathymetric_profile", None)
    if (
        beach_slope is not None
        and bathymetric_profile
        and wave_height is not None
        and wave_period is not None
    ):
        depth_at_break = bathymetric_profile[0].depth_m
        wave_height, breaker_gamma = apply_breaker_correction(
            wave_height, wave_period, beach_slope, depth_at_break
        )
        supplements_applied.append("breaker_correction")

    # Supplement 2 removed — structure effects handled by SWAN OBSTACLE (ADR-095).
    # The SWAN OBSTACLE command (T2.3) natively models structure transmission
    # and reflection during the wave propagation computation, which is more
    # physically correct than the post-processing approach.
    structure_applied = False

    # Supplement 4 removed — topographic multipliers double-counted refraction
    # SWAN's nested grids (L2) now compute directly (ADR-093 Amendment 2 §5b,
    # 2026-07-25). The operator's topographic classification is retained in
    # spot config as the L3 enable trigger; it no longer adjusts wave height.

    return {
        "wave_height": wave_height,
        "wave_period": wave_period,
        "wave_direction": wave_direction,
        "breaker_gamma": breaker_gamma,
        "structure_applied": structure_applied,
        "supplements_applied": supplements_applied,
    }
