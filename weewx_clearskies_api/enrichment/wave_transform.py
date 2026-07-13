"""NWPS supplement processor (Phase 3, T3.2).

Applies four targeted supplements to NWPS/SWAN nearshore wave data, per
API-MANUAL.md §17 "NWPS supplement processor". Registered against the surf
scoring pipeline: runs after the NWPS fetch, before ``surf_scorer.py``.

The four supplements, applied in this order:

1. Sub-grid spatial interpolation (bilinear) — refines the NWPS grid-cell
   value to the exact spot coordinates. Runs first because it establishes
   the wave height baseline the other three supplements correct.
2. Breaker index correction (Battjes 1974 γ tuning) — replaces SWAN's
   constant γ = 0.73 with a site-specific value derived from the Iribarren
   number, then re-caps wave height at the breaking depth.
3. Coastal structure transmission/reflection effects — attenuates wave
   height for spots in the lee of a jetty, breakwater, pier, seawall, or
   groin. Direction-dependent (T7.2): a shadow-zone check and cos²(theta)
   angular Kt modulation scale the effect by the wave direction relative
   to the structure's bearing.
4. Topographic focusing/sheltering — a multiplicative adjustment based on
   the spot's operator-classified topographic feature.

No-op when NWPS data is unavailable — WaveWatch III (or any other
provider's) data passes through unmodified because ``apply_supplements``
is simply not called in that case.

**What is NOT supplemented:** Shoaling, refraction, bottom friction,
wave-current interaction. NWPS/SWAN computes these with its own bathymetry
and RTOFS currents; re-running them here would duplicate NWPS's work
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

#: Transmission coefficients (Kt) by structure material.
#: H_transmitted = Kt * H_incident.
KT: dict[str, float] = {
    "impermeable": 0.10,
    "semi_permeable": 0.35,
    "permeable": 0.75,
}

#: Topographic wave focusing/sheltering multipliers.
TOPO_MULTIPLIERS: dict[str, float] = {
    "point_break": 1.1,
    "headland": 1.2,
    "bay_break": 0.9,
    "straight_beach": 1.0,
}

#: Structure influence-zone multipliers (multiplied by structure length_m).
INFLUENCE_ZONE_MULTIPLIERS: dict[str, float] = {
    "jetty": 4.0,
    "breakwater": 3.0,
    "pier": 1.5,
    "seawall": 20.0,
    "groin": 2.5,
}


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
# Supplement 2 — Coastal structure transmission/reflection effects
# ---------------------------------------------------------------------------


def _structure_kt_effective(
    structure: Any, wave_direction: float | None = None
) -> float:
    """Return the effective (distance-attenuated) Kt for one structure.

    Within the structure's influence zone (structure-type multiplier x
    length_m), the full material Kt applies. Beyond it, the effect
    diminishes with the square of (influence_zone / distance) — full Kt
    at the zone boundary, approaching 1.0 (no effect) as distance grows.

    ``wave_direction`` (degrees true, direction the waves are coming FROM)
    enables two direction-dependent refinements (T7.2, research brief
    §11.5.1). Both are skipped — falling back to the omnidirectional
    behavior above — when ``wave_direction`` is None:

    1. **Shadow zone check.** When the structure also carries a
       ``bearing_to_spot_degrees`` (bearing from the structure's nearest
       point to the surf spot), the spot is only in the structure's
       "shadow" if it falls within a cone projected along the wave's
       travel direction (``wave_direction + 180``), half-angle
       ``atan2(length_m, 2*distance_m)``. Outside that cone the structure
       cannot block these waves at all — returns 1.0 (no effect)
       regardless of material/distance.
    2. **Angular Kt modulation.** The blocking fraction ``(1 - kt)`` is
       scaled by ``cos²(theta)``, where ``theta`` is the angle between the
       wave direction and the structure's normal (0 = wave hits the
       structure perpendicularly = full blocking; 90 = wave travels
       parallel to the structure's length = no blocking).
    """
    kt = KT.get(structure.material, 1.0)
    zone_multiplier = INFLUENCE_ZONE_MULTIPLIERS.get(structure.type, 1.0)
    influence_zone = zone_multiplier * structure.length_m
    distance = structure.distance_m

    bearing_to_spot = getattr(structure, "bearing_to_spot_degrees", None)
    if wave_direction is not None and bearing_to_spot is not None:
        wave_travel = (wave_direction + 180) % 360
        cone_half = math.degrees(math.atan2(structure.length_m, 2 * distance))
        bearing_diff = abs((bearing_to_spot - wave_travel) % 360)
        if bearing_diff > 180:
            bearing_diff = 360 - bearing_diff
        if bearing_diff > cone_half:
            # Spot is NOT in the structure's shadow for this wave direction.
            return 1.0

    angular_factor = 1.0
    if wave_direction is not None and hasattr(structure, "bearing_degrees"):
        alpha = abs((wave_direction - structure.bearing_degrees) % 360)
        if alpha > 180:
            alpha = 360 - alpha
        theta = abs(alpha - 90)
        angular_factor = math.cos(math.radians(theta)) ** 2

    effective_blocking = (1.0 - kt) * angular_factor
    if influence_zone <= 0 or distance <= influence_zone:
        return 1.0 - effective_blocking
    return 1.0 - effective_blocking * (influence_zone / distance) ** 2


def apply_structure_effects(
    wave_height: float,
    structures: list[Any],
    wave_direction: float | None = None,
) -> tuple[float, bool]:
    """Apply coastal-structure transmission effects to wave height.

    Multiple structures combine via linear superposition (their effective
    Kt values are multiplied together). Returns ``(new_height, applied)``;
    ``applied`` is False (and height unchanged) when ``structures`` is empty.

    ``wave_direction`` (degrees true) enables directional Kt modulation and
    the shadow-zone check in ``_structure_kt_effective`` — see that
    function's docstring. ``None`` (the default) preserves the prior
    omnidirectional behavior.
    """
    if not structures:
        return wave_height, False

    combined_kt = 1.0
    for structure in structures:
        combined_kt *= _structure_kt_effective(structure, wave_direction=wave_direction)

    return wave_height * combined_kt, True


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
    """Standard bilinear interpolation over a 2D NWPS grid.

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
# Supplement 4 — Topographic focusing/sheltering
# ---------------------------------------------------------------------------


def apply_topographic_adjustment(wave_height: float, topographic_feature: str) -> float:
    """Multiply wave height by the spot's topographic feature multiplier.

    Unknown/missing feature values default to 1.0 (no adjustment) rather
    than raising — ``SurfSpotConfig.validate()`` is the trust boundary that
    rejects invalid ``topographic_feature`` values at config-load time.
    """
    multiplier = TOPO_MULTIPLIERS.get(topographic_feature, 1.0)
    return wave_height * multiplier


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def apply_supplements(
    nwps_data: dict[str, Any] | None,
    spot_config: Any,
    spot_lat: float,
    spot_lon: float,
) -> dict[str, Any] | None:
    """Apply the four NWPS supplements and return corrected wave data.

    Processing order: interpolation (3) -> breaker correction (1) ->
    structure effects (2) -> topographic adjustment (4).

    Returns a dict with keys ``wave_height``, ``wave_period``,
    ``wave_direction``, ``breaker_gamma``, ``structure_applied`` (bool), and
    ``supplements_applied`` (list of the supplement names that were actually
    applied, in application order).

    Returns None when ``nwps_data`` is None or empty (no-op — WaveWatch III
    or other non-NWPS data is not touched by this processor).
    """
    if not nwps_data:
        return None

    wave_height = nwps_data.get("wave_height")
    wave_period = nwps_data.get("wave_period")
    wave_direction = nwps_data.get("wave_direction")

    supplements_applied: list[str] = []
    breaker_gamma: float | None = None
    structure_applied = False

    # Supplement 3 — sub-grid interpolation. Runs first: it establishes the
    # wave-height baseline the remaining supplements correct.
    grid_data = nwps_data.get("grid_data")
    grid_lats = nwps_data.get("grid_lats")
    grid_lons = nwps_data.get("grid_lons")
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

    # Supplement 2 — coastal structure effects.
    structures = getattr(spot_config, "structures", None) or []
    if wave_height is not None:
        wave_height, structure_applied = apply_structure_effects(
            wave_height, structures, wave_direction=wave_direction
        )
        if structure_applied:
            supplements_applied.append("structure_effects")

    # Supplement 4 — topographic focusing/sheltering.
    topographic_feature = getattr(spot_config, "topographic_feature", None)
    if wave_height is not None and topographic_feature:
        wave_height = apply_topographic_adjustment(wave_height, topographic_feature)
        supplements_applied.append("topographic_adjustment")

    return {
        "wave_height": wave_height,
        "wave_period": wave_period,
        "wave_direction": wave_direction,
        "breaker_gamma": breaker_gamma,
        "structure_applied": structure_applied,
        "supplements_applied": supplements_applied,
    }
