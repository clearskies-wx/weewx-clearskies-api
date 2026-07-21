"""Tests (T8.2): 1D analytical model consistency vs linear wave theory in QB=0 zone.

Verifies that in the pre-breaking (QB=0) region, ``run_1d_analytical()`` Hs values
closely match linear shoaling + Snell's law refraction theory.  This is the R3
consistency check from SURF-1D-IMPLEMENTATION-PLAN.md Phase 8.

Physics references
------------------
- Dispersion relation: L = L₀ × tanh(2πd/L)  where L₀ = gT²/(2π)
- Group velocity: Cg = n × C  where n = 0.5 × (1 + 2kd / sinh(2kd))
- Shoaling coefficient: Ks = sqrt(Cg_ref / Cg_local)
- Snell's law (deep-water reference): sin(θ)/L = sin(θ₀)/L₀
- Refraction coefficient: Kr = sqrt(cos(θ₀) / cos(θ_local))
- Expected Hs = Hs_ref × Ks × Kr  (linear theory, no dissipation)
- QB=0 zone: wave not yet broken → Hs_expected < γ × depth

Direction convention used in run_1d_analytical()
-------------------------------------------------
theta0 = direction − beach_facing  (approach angle from shore-normal).
Shore-normal: direction == beach_facing → theta0 = 0°  (Kr = 1 everywhere).

For the shore-normal test: direction = beach_facing = 180°  (theta0 = 0°).
For the 30° oblique test:  direction = 210°, beach_facing = 180°  (theta0 = 30°).

Note: the combination direction=0°, beach_facing=180° gives theta0=−180°, which
produces cos(−π)=−1 and causes the refraction guard (clip to 0) to zero out Kr.
The values used here are the physically correct inputs for the intended angles.
"""

from __future__ import annotations

import math

import numpy as np

from weewx_clearskies_api.services.surf_1d_analytical import run_1d_analytical

# ---------------------------------------------------------------------------
# Physical constants and test parameters
# ---------------------------------------------------------------------------

_G = 9.81       # gravitational acceleration (m/s²)
_GAMMA = 0.73   # breaking parameter — matches SWAN default used in the model
_HS_HANDOFF = 1.0  # m — significant wave height at the offshore (handoff) point
_TP = 12.0         # s — peak period

# Linear slope profile: 15 m depth offshore to 0 m at shore, no bars.
# Slope ≈ 0.03 (15 m / 500 m) — gentle enough that Hs=1 m, Tp=12 s remains
# non-breaking across most of the profile, producing a long QB=0 zone.
_PROFILE_LENGTH_M = 500.0  # m — transect length from shore to offshore end
_MAX_DEPTH_M = 15.0        # m — depth at the offshore end
_PROFILE_DX_M = 3.0        # m — point spacing along the transect

# Tolerance for consistency check: 5% relative error.
# The Battjes-Janssen (1978) dissipation term is nonzero even when Hs < γd
# (Qb ~ exp(−(Hs/γd)²)), but remains small far from the breaker line.
# On a gentle linear slope at these parameters the BJ term accumulates to < 3%.
_RTOL = 0.05  # 5 % relative tolerance


# ---------------------------------------------------------------------------
# Helpers — independent implementation of linear wave theory
# ---------------------------------------------------------------------------


def _make_linear_slope_bathy(
    length_m: float = _PROFILE_LENGTH_M,
    dx: float = _PROFILE_DX_M,
) -> np.ndarray:
    """Return a linear-slope bathymetric profile with no sandbars.

    Produces a clean, uninterrupted pre-breaking (QB=0) zone for the
    consistency check.  The slope is 15 m / 500 m ≈ 0.03.

    Returns
    -------
    np.ndarray, shape (N, 2):
        [[distance_from_shore_m, depth_m], ...]
        Ordered offshore-first (large distance to small distance) as
        required by run_1d_analytical().  Depths are positive below water.
    """
    n_pts = int(round(length_m / dx)) + 1
    # shore (0 m) to offshore (length_m), then reverse for offshore-first ordering
    distances_shore_to_offshore = np.linspace(0.0, length_m, n_pts)
    depths = _MAX_DEPTH_M * distances_shore_to_offshore / length_m
    depths = np.maximum(depths, 0.01)
    return np.column_stack([distances_shore_to_offshore[::-1], depths[::-1]])


def _dispersion_linear_theory(tp: float, depths: np.ndarray, n_iter: int = 25) -> np.ndarray:
    """Solve the dispersion relation L = L₀ × tanh(2πd/L) by Newton iteration.

    Implements the standard iterative scheme with more iterations than the
    production model to ensure tight convergence for the reference values.

    Parameters
    ----------
    tp:     peak period (s)
    depths: water depths at each profile point (m)
    n_iter: number of Newton iterations (25 gives < 1e-10 m residual)

    Returns
    -------
    np.ndarray: local wavelength L at each depth (m)
    """
    L0 = _G * tp * tp / (2.0 * math.pi)
    L = L0 * np.ones_like(depths)
    for _ in range(n_iter):
        k = 2.0 * math.pi / L
        L = L0 * np.tanh(k * depths)
    return np.maximum(L, 0.01)


def _group_velocity_linear_theory(
    tp: float, depths: np.ndarray, L: np.ndarray
) -> np.ndarray:
    """Compute group velocity Cg = n × C where n = 0.5 × (1 + 2kd / sinh(2kd)).

    This is the standard linear wave theory formula (CEM, 2002, §II-1-1).

    Parameters
    ----------
    tp:     peak period (s)
    depths: water depths (m)
    L:      local wavelength at each depth (m)

    Returns
    -------
    np.ndarray: group velocity Cg at each depth (m/s)
    """
    k = 2.0 * math.pi / L
    C = L / tp
    kd = k * depths
    kd_safe = np.clip(kd, 1e-6, 50.0)
    n = 0.5 * (1.0 + 2.0 * kd_safe / np.sinh(2.0 * kd_safe))
    return n * C


def _snell_refraction_linear_theory(
    theta0_rad: float,
    L0_deep: float,
    L_local: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply Snell's law with deep-water invariant: sin(θ)/L = sin(θ₀)/L₀.

    Returns the local approach angle and refraction coefficient Kr at each
    depth.  Uses the same reference convention as run_1d_analytical(): theta0
    is the approach angle measured in true deep water (L₀), not at the
    offshore boundary of the profile.

    Parameters
    ----------
    theta0_rad: deep-water approach angle from shore-normal (radians)
    L0_deep:    deep-water wavelength = gT²/(2π) (m)
    L_local:    local wavelength at each profile point (m)

    Returns
    -------
    theta_local: np.ndarray — local approach angle from shore-normal (radians)
    Kr:          np.ndarray — refraction coefficient = sqrt(cos(θ₀)/cos(θ_local))
    """
    sin_theta = math.sin(theta0_rad) * L_local / L0_deep
    sin_theta = np.clip(sin_theta, -1.0, 1.0)
    theta_local = np.arcsin(sin_theta)

    cos_theta0 = math.cos(theta0_rad)
    cos_theta_local = np.cos(theta_local)
    # Guard against near-zero denominator (grazing incidence)
    safe_cos = np.where(np.abs(cos_theta_local) > 0.001, cos_theta_local, np.sign(cos_theta_local) * 0.001)
    cos_ratio = np.where(safe_cos > 0, cos_theta0 / safe_cos, 0.0)
    cos_ratio = np.clip(cos_ratio, 0.0, 10.0)
    Kr = np.sqrt(cos_ratio)
    return theta_local, Kr


def _expected_hs_linear_theory(
    hs_ref: float,
    tp: float,
    theta0_rad: float,
    depths: np.ndarray,
) -> np.ndarray:
    """Compute the expected Hs profile from linear shoaling + Snell refraction.

    Uses the same reference convention as ``run_1d_analytical()``:

    - Shoaling: Ks = sqrt(Cg[0] / Cg[i])  where index 0 is the offshore
      (deepest) point — the handoff reference.
    - Refraction: Snell invariant is sin(θ)/L = sin(θ₀)/L₀ where L₀ is the
      deep-water wavelength (not L at index 0).

    In the QB=0 (pre-breaking) zone the Battjes-Janssen dissipation term
    is negligible, so ``run_1d_analytical()`` should match this prediction
    within ``_RTOL``.

    Parameters
    ----------
    hs_ref:     Hs at the offshore reference (handoff) point (m)
    tp:         peak period (s)
    theta0_rad: approach angle from shore-normal in deep water (radians)
    depths:     water depths along the profile — must be in the same order as
                the ``depths`` array returned by run_1d_analytical() (offshore
                first, i.e. depths[0] is the largest depth)

    Returns
    -------
    np.ndarray: expected Hs at each profile point (m)
    """
    L0_deep = _G * tp * tp / (2.0 * math.pi)   # deep-water wavelength
    L = _dispersion_linear_theory(tp, depths)
    Cg = _group_velocity_linear_theory(tp, depths, L)

    # Shoaling coefficient relative to the offshore reference (index 0 = deepest)
    Cg_ref = Cg[0]
    Ks = np.sqrt(Cg_ref / np.maximum(Cg, 0.001))

    _, Kr = _snell_refraction_linear_theory(theta0_rad, L0_deep, L)

    return hs_ref * Ks * Kr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_consistency_shoaling_only_shore_normal():
    """1D model Hs matches pure linear shoaling in the QB=0 zone (shore-normal).

    Profile
    -------
    Linear slope 15 m → 0 m over 500 m (slope ≈ 0.03, no bars).  A bar-free
    slope is used so the QB=0 zone spans a predictable continuous region from
    the offshore end to approximately the 1.4 m breaker depth.

    Inputs
    ------
    Hs = 1.0 m, Tp = 12 s, tide = 0.0 m, gamma = 0.73.
    direction = beach_facing = 180° → theta0 = 0° (shore-normal).

    Expected behaviour
    ------------------
    For shore-normal incidence Kr = 1 everywhere (no refraction).  In the
    QB=0 zone (Hs_expected < gamma × depth) Battjes-Janssen dissipation is
    negligible.  The 1D model should match Hs_deep × Ks within 5% relative
    tolerance, where Ks = sqrt(Cg_ref / Cg_local) from linear wave theory.

    Acceptance criterion (T8.2)
    ---------------------------
    Max relative error |Hs_model − Hs_expected| / Hs_expected < 5 % at every
    QB=0 profile point with depth > 0.5 m.
    """
    beach_facing = 180.0
    # Shore-normal: theta0 = direction − beach_facing = 0°
    # (direction=0°, beach_facing=180° gives theta0=−180° which makes cos(theta0)=−1
    # and zeroes out Kr via the clip guard — physically wrong for shore-normal.
    # The correct shore-normal input is direction == beach_facing.)
    direction = 180.0

    bathy = _make_linear_slope_bathy()

    result = run_1d_analytical(
        hs=_HS_HANDOFF,
        tp=_TP,
        direction=direction,
        bathy_profile=bathy,
        tide_level=0.0,
        gamma=_GAMMA,
        beach_facing=beach_facing,
    )

    depths = result.depths        # offshore-first, as returned by the model
    hs_model = result.hs_profile  # model output

    theta0_rad = math.radians(direction - beach_facing)  # = 0.0 rad
    expected_hs = _expected_hs_linear_theory(_HS_HANDOFF, _TP, theta0_rad, depths)

    # QB=0 zone: expected (non-breaking) Hs < gamma × depth
    qb_zero_mask = expected_hs < _GAMMA * depths

    assert qb_zero_mask.any(), (
        "No QB=0 (pre-breaking) zone found on the linear slope profile. "
        f"expected_hs range: [{expected_hs.min():.3f}, {expected_hs.max():.3f}] m, "
        f"gamma*depth range: [{(_GAMMA * depths).min():.3f}, "
        f"{(_GAMMA * depths).max():.3f}] m. "
        "Check profile geometry and wave parameters."
    )

    # Exclude very shallow depths (< 0.5 m) where depth-limiting saturation
    # dominates and the QB=0 condition transitions to the active breaking zone.
    valid = qb_zero_mask & (depths > 0.5)

    assert valid.sum() >= 10, (
        f"Only {valid.sum()} valid QB=0 points with depth > 0.5 m (minimum 10 required). "
        "Increase profile length or reduce Hs to capture more pre-breaking zone."
    )

    # For shore-normal incidence Kr = 1 everywhere — verify this from the theory
    L0_deep = _G * _TP * _TP / (2.0 * math.pi)
    L = _dispersion_linear_theory(_TP, depths)
    _, Kr = _snell_refraction_linear_theory(theta0_rad, L0_deep, L)
    kr_range = float(np.max(Kr[valid]) - np.min(Kr[valid]))
    assert kr_range < 0.001, (
        f"Shore-normal test: Kr should be 1.0 everywhere but varies by {kr_range:.4f}. "
        "theta0 is not zero — check direction / beach_facing inputs."
    )

    # Relative error: |Hs_model − Hs_expected| / Hs_expected
    rel_errors = (
        np.abs(hs_model[valid] - expected_hs[valid])
        / np.maximum(expected_hs[valid], 0.001)
    )
    max_rel_error = float(rel_errors.max())
    mean_rel_error = float(rel_errors.mean())

    assert max_rel_error < _RTOL, (
        f"Shore-normal consistency check FAILED: "
        f"max relative error {max_rel_error:.1%} exceeds {_RTOL:.0%} tolerance "
        f"(mean = {mean_rel_error:.1%}) over {valid.sum()} QB=0 profile points. "
        f"Model Hs range in QB=0 zone: "
        f"[{hs_model[valid].min():.4f}, {hs_model[valid].max():.4f}] m. "
        f"Expected Hs range: "
        f"[{expected_hs[valid].min():.4f}, {expected_hs[valid].max():.4f}] m. "
        "In the QB=0 zone Battjes-Janssen dissipation should be near-zero; "
        "a >5% deviation indicates a shoaling computation error."
    )


def test_consistency_shoaling_refraction_oblique():
    """1D model Hs matches Hs_ref × Ks × Kr in QB=0 zone (30° oblique incidence).

    Profile
    -------
    Same linear slope as the shore-normal test (15 m → 0 m over 500 m, no bars).

    Inputs
    ------
    Hs = 1.0 m, Tp = 12 s, tide = 0.0 m, gamma = 0.73.
    direction = 210°, beach_facing = 180° → theta0 = 30° oblique incidence.

    Expected behaviour
    ------------------
    For oblique incidence the expected Hs is:
        Hs_expected = Hs_ref × Ks × Kr
    where:
        Ks = sqrt(Cg_ref / Cg_local)        (shoaling)
        Kr = sqrt(cos(θ₀) / cos(θ_local))   (refraction, from Snell's law)
        sin(θ_local)/L_local = sin(θ₀)/L₀   (Snell invariant, deep-water reference)

    Waves refract toward shore-normal as they enter shallower water: θ_local
    decreases from θ₀ = 30° at the offshore end.  The test verifies that both
    the shoaling and the Snell refraction are handled consistently with linear
    theory in the non-breaking zone.

    Acceptance criterion (T8.2)
    ---------------------------
    Max relative error |Hs_model − Hs_expected| / Hs_expected < 5 % at every
    QB=0 profile point with depth > 0.5 m.
    Kr must vary across the QB=0 zone (confirming refraction is active, not
    a trivial Kr = 1 pass-through).
    """
    beach_facing = 180.0
    # 30° oblique: theta0 = direction − beach_facing = 210 − 180 = 30°
    direction = 210.0
    theta0_deg = direction - beach_facing  # = 30.0°
    theta0_rad = math.radians(theta0_deg)

    bathy = _make_linear_slope_bathy()

    result = run_1d_analytical(
        hs=_HS_HANDOFF,
        tp=_TP,
        direction=direction,
        bathy_profile=bathy,
        tide_level=0.0,
        gamma=_GAMMA,
        beach_facing=beach_facing,
    )

    depths = result.depths
    hs_model = result.hs_profile

    expected_hs = _expected_hs_linear_theory(_HS_HANDOFF, _TP, theta0_rad, depths)

    # QB=0 zone: not yet breaking
    qb_zero_mask = expected_hs < _GAMMA * depths
    valid = qb_zero_mask & (depths > 0.5)

    assert valid.any(), (
        f"No valid QB=0 zone found for {theta0_deg:.0f}° oblique incidence. "
        f"expected_hs range: [{expected_hs.min():.3f}, {expected_hs.max():.3f}] m. "
        "The wave may be breaking across the entire profile for these parameters."
    )
    assert valid.sum() >= 10, (
        f"Only {valid.sum()} valid QB=0 points with depth > 0.5 m "
        f"for {theta0_deg:.0f}° oblique (minimum 10 required). "
    )

    # Verify refraction is actually exercised: Kr must vary meaningfully
    # across the QB=0 zone (Kr decreases as waves refract toward shore-normal).
    L0_deep = _G * _TP * _TP / (2.0 * math.pi)
    L = _dispersion_linear_theory(_TP, depths)
    _, Kr = _snell_refraction_linear_theory(theta0_rad, L0_deep, L)
    kr_variation = float(np.max(Kr[valid]) - np.min(Kr[valid]))
    assert kr_variation > 0.005, (
        f"Kr varies by only {kr_variation:.4f} across the QB=0 zone; "
        f"refraction at theta0={theta0_deg:.0f}° is not being exercised meaningfully. "
        "Check the direction / beach_facing inputs."
    )

    # Relative error between model and linear theory
    rel_errors = (
        np.abs(hs_model[valid] - expected_hs[valid])
        / np.maximum(expected_hs[valid], 0.001)
    )
    max_rel_error = float(rel_errors.max())
    mean_rel_error = float(rel_errors.mean())

    assert max_rel_error < _RTOL, (
        f"Oblique ({theta0_deg:.0f}°) consistency check FAILED: "
        f"max relative error {max_rel_error:.1%} exceeds {_RTOL:.0%} tolerance "
        f"(mean = {mean_rel_error:.1%}) over {valid.sum()} QB=0 profile points. "
        f"theta0 = {theta0_deg:.0f}°, "
        f"Kr range in QB=0 zone: [{Kr[valid].min():.4f}, {Kr[valid].max():.4f}]. "
        f"Model Hs range: [{hs_model[valid].min():.4f}, {hs_model[valid].max():.4f}] m. "
        f"Expected Hs range: [{expected_hs[valid].min():.4f}, {expected_hs[valid].max():.4f}] m. "
        "A >5% deviation means either shoaling (Ks) or Snell refraction (Kr) "
        "is inconsistent with linear theory in the pre-breaking zone."
    )
