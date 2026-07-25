"""Unit tests for services/surf_1d_analytical.py — Phase 4A, T4A.2b
(Battjes-Janssen conservative energy-flux marching, LC-2 + LC-22).

These are real physics assertions, not tautologies — each test encodes a
specific numerical property the round brief and coordinator's lead calls
require, verified against hand-derived and independently-checked reference
values (see c:\\tmp\\marine-sep-P4A-scratch.md "p4a-physics" log for the full
derivation and cross-checks against a live HB bathymetry sample).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from weewx_clearskies_api.enrichment.bathymetry import interpolate_profile_pchip
from weewx_clearskies_api.services.surf_1d_analytical import (
    _G,
    _battjes_janssen,
    _dispersion,
    _group_velocity,
    _snell_refraction,
    _solve_breaking_fraction,
    run_1d_analytical,
)


def _shoaled_refracted_hs(
    hs: float, tp: float, depths: np.ndarray, theta0_deg: float = 0.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build the pre-B-J Hs array (Ks*Kr applied, no friction) plus Cg/Kr,
    matching exactly what run_1d_analytical hands to _battjes_janssen."""
    theta0 = math.radians(theta0_deg)
    L0 = _G * tp**2 / (2.0 * np.pi)
    L = _dispersion(tp, depths)
    Cg = _group_velocity(tp, depths, L)
    Ks = np.sqrt(Cg[0] / np.maximum(Cg, 0.01))
    _, Kr = _snell_refraction(theta0, L0, L)
    Hs_in = hs * Ks * Kr
    return Hs_in, Cg, Kr, L


# ---------------------------------------------------------------------------
# _solve_breaking_fraction (LC-22) — exact implicit Battjes-Janssen Qb
# ---------------------------------------------------------------------------


def test_solve_breaking_fraction_matches_reference_table():
    # Coordinator's independently-verified reference values (LC-22). Only
    # the two rows that are internally self-consistent are asserted here --
    # for beta=0.1414 and beta=0.0849 this solver's output does NOT match
    # the coordinator's table (got 1.8998e-22 / 5.6021e-61 vs their reported
    # 6.3257e-17 / 1.0000e-300). Verified this solver's own result at those
    # betas satisfies the EXACT primary-source equation to machine precision
    # (g(u) = (1-Qb) + beta**2*ln(Qb) ~ 1e-16 at the converged root, checked
    # directly) and matches the independent exp(-1/beta**2) leading
    # asymptotic to ~14 significant figures -- consistent with genuine
    # convergence, not a bug in this implementation. Flagged to the lead as
    # a likely precision limitation in whatever tool produced the table's
    # most extreme rows (extreme-underflow root-finding in linear Qb-space,
    # rather than this module's log-space reformulation, is a common failure
    # mode). Not silently resolved -- see closeout / scratch-file note.
    cases = [
        (0.3536, 3.3637e-4),
        (0.2121, 2.2336e-10),
    ]
    for beta, expected in cases:
        got = _solve_breaking_fraction(beta)
        assert got == pytest.approx(expected, rel=0.01)


def test_solve_breaking_fraction_underflows_to_exactly_zero_for_tiny_beta():
    assert _solve_breaking_fraction(0.0212) == 0.0


def test_solve_breaking_fraction_guards_beta_bounds():
    assert _solve_breaking_fraction(0.0) == 0.0
    assert _solve_breaking_fraction(-0.5) == 0.0
    assert _solve_breaking_fraction(1.0) == 1.0
    assert _solve_breaking_fraction(1.5) == 1.0


def test_solve_breaking_fraction_no_gamma_independent_floor():
    # The bug LC-22 fixes: the OLD Qb=1-exp(-ratio**2) stand-in had
    # Dtot/E -> 1/T regardless of gamma. The TRUE Qb must actually shrink
    # as beta shrinks -- confirm monotonic decrease toward zero.
    betas = [0.5, 0.3, 0.2, 0.1, 0.05, 0.02]
    qbs = [_solve_breaking_fraction(b) for b in betas]
    for i in range(1, len(qbs)):
        assert qbs[i] < qbs[i - 1]
    assert qbs[-1] == 0.0


# ---------------------------------------------------------------------------
# _battjes_janssen — LC-2 mandatory regression invariant
# ---------------------------------------------------------------------------


def test_battjes_janssen_flux_marching_preserves_shoaling_and_refraction():
    """Mandatory deliverable (brief §5 LC-2): with breaking suppressed
    (gamma large enough that Hs < gamma*d everywhere) and friction off, the
    forward-marching output must reproduce hs*Ks*Kr to within 1e-6 relative
    at every point. If this fails, the marching implementation is wrong
    regardless of what break-point counts look like elsewhere.

    gamma=5 verified (c:\\tmp\\marine-sep-P4A-scratch.md) to give max relative
    error ~2.2e-16 (machine precision) with the LC-22-corrected Qb -- the OLD
    Qb stand-in could NOT pass this test at any gamma (a ~98% flux collapse
    over 500m even at gamma=100, independent of gamma) because of its
    gamma-independent dissipation floor; this is the test that caught it.
    """
    distances = np.arange(0, 501, 5.0)[::-1]  # offshore -> shore, 500m -> 0m
    depths = 3.0 + 17.0 * distances / 500.0  # monotonic, 20m depth -> 3m depth
    hs, tp = 1.0, 10.3
    gamma = 5.0  # large enough that Qb ~ 0 everywhere (verified below)

    Hs_in, Cg, Kr, _ = _shoaled_refracted_hs(hs, tp, depths)
    dx = np.diff(distances, prepend=distances[0])

    Hs_out = _battjes_janssen(Hs_in, depths, gamma, dx, Cg, Kr, tp)

    rel_err = np.abs(Hs_out - Hs_in) / np.maximum(Hs_in, 1e-9)
    assert rel_err.max() < 1e-6, f"max relative error {rel_err.max():.3e} exceeds 1e-6"

    # Sanity: confirm breaking really was suppressed everywhere (Qb ~ 0), not
    # that the assertion above passed by coincidence.
    Hmax = gamma * depths
    beta = (Hs_out / math.sqrt(2.0)) / np.maximum(Hmax, 0.01)
    assert beta.max() < 0.35, "test setup does not actually suppress breaking"


def test_battjes_janssen_flux_marching_dissipates_when_breaking_occurs():
    # Sanity counterpart to the invariant above: with a REALISTIC gamma
    # (production default) the output must actually differ from the input
    # once depth shoals enough to trigger breaking -- the invariant test is
    # only meaningful if this ALSO holds (otherwise both "pass" trivially).
    distances = np.arange(0, 301, 2.0)[::-1]
    depths = np.maximum(0.05, 15.0 * distances / 300.0)
    hs, tp, gamma = 2.0, 12.0, 0.73

    Hs_in, Cg, Kr, _ = _shoaled_refracted_hs(hs, tp, depths)
    dx = np.diff(distances, prepend=distances[0])
    Hs_out = _battjes_janssen(Hs_in, depths, gamma, dx, Cg, Kr, tp)

    rel_diff = np.abs(Hs_out - Hs_in) / np.maximum(Hs_in, 1e-9)
    assert rel_diff.max() > 0.01, "breaking dissipation never engaged in a breaking scenario"


# ---------------------------------------------------------------------------
# run_1d_analytical end-to-end — multi-bar reformation, non-degenerate decay,
# dx-sensitivity (T4A.2b Accept bullets)
# ---------------------------------------------------------------------------


def _two_bar_profile(dx_step: float) -> np.ndarray:
    """A single sharp nearshore bar (crest ~180m from shore) on an 8m-deep,
    700m-long shelf. Verified (c:\\tmp\\marine-sep-P4A-scratch.md) to produce
    two break points (200m and 166m) with Hs dipping to a local minimum at
    the bar crest then recovering (reformation) before re-breaking."""
    max_dist = 700.0
    n = int(max_dist / dx_step) + 1
    d = np.array([i * dx_step for i in range(n)])[::-1]
    base = 9.0 * d / max_dist
    outer_bar = 2.2 * np.exp(-((d - 450.0) ** 2) / (2 * 15.0**2))
    inner_bar = 1.5 * np.exp(-((d - 180.0) ** 2) / (2 * 10.0**2))
    depth = np.maximum(0.05, base - outer_bar - inner_bar)
    return np.column_stack([d, depth])


def test_multibar_profile_shows_reformation_between_bars():
    bathy = _two_bar_profile(2.0)
    res = run_1d_analytical(
        hs=1.5, tp=13.0, direction=180.0, bathy_profile=bathy, gamma=0.73,
        beach_facing=180.0, cfjon=0.038,
    )
    assert len(res.break_points) >= 2, "expected at least 2 break points on a barred profile"

    bp_outer, bp_inner = res.break_points[0], res.break_points[1]
    idx_outer = int(np.argmin(np.abs(res.distances - bp_outer.distance_m)))
    idx_inner = int(np.argmin(np.abs(res.distances - bp_inner.distance_m)))
    # distances descend (offshore -> shore); shoreward of bp_outer means a
    # larger index, and bp_inner is shoreward of bp_outer.
    assert idx_inner > idx_outer

    between = res.hs_profile[idx_outer : idx_inner + 1]
    trough_min = between.min()
    # Reformation: Hs at the second break point (shoreward, after recovering
    # through the trough) must exceed the local minimum reached just after
    # the first break -- not a monotonic decay all the way through.
    assert bp_inner.hs_m > trough_min, (
        f"Hs at second break ({bp_inner.hs_m:.3f}) does not exceed the "
        f"post-first-break local minimum ({trough_min:.3f}) -- no reformation"
    )


def test_post_breaking_decay_is_not_degenerate_to_gamma_times_depth():
    """T4A.2b's problem statement: the OLD independent-per-point code
    degenerated post-breaking Hs to just `gamma * depth` (the explicit
    depth-limited cap), not real physics. Assert a meaningful fraction of
    post-break points differ from gamma*d by more than a small tolerance."""
    bathy = _two_bar_profile(2.0)
    gamma = 0.73
    res = run_1d_analytical(
        hs=1.5, tp=13.0, direction=180.0, bathy_profile=bathy, gamma=gamma,
        beach_facing=180.0, cfjon=0.038,
    )
    assert res.break_points, "test profile must produce at least one break point"
    idx_first_break = int(np.argmin(np.abs(res.distances - res.break_points[0].distance_m)))

    post_break_hs = res.hs_profile[idx_first_break:]
    post_break_depths = res.depths[idx_first_break:]
    gamma_d = gamma * post_break_depths

    diffs = np.abs(post_break_hs - gamma_d)
    # 3cm -- well above floating-point/clip noise, comfortably below the
    # measured ~78% of points that actually differ by more than this on the
    # test profile (verified numerically; a literal gamma*d degenerate decay
    # would put ~100% of points at diff==0).
    meaningful = diffs > 0.03
    fraction_meaningful = meaningful.sum() / len(diffs)
    assert fraction_meaningful > 0.5, (
        f"only {fraction_meaningful:.0%} of post-break points differ from "
        "gamma*depth by > 3cm -- looks like the degenerate gamma*d decay "
        "T4A.2b exists to fix"
    )


def test_dx_sensitivity_break_point_and_face_height_agreement():
    """Same physical profile sampled at native 2m and 5m (both then run
    through interpolate_profile_pchip, matching T4A.2's actual pipeline) must
    produce break points within a few metres of each other and face heights
    within a modest tolerance. Verified numerically (c:\\tmp\\marine-sep-P4A-
    scratch.md) on an HB-scale profile: native 2m/5m/8m/10.28m all agree on
    the first break point to within 0.01m distance and 5 decimal places of
    Hs -- this test asserts a looser, more robust bound on that same result.
    """
    from scipy.interpolate import PchipInterpolator

    anchor_x = [0.0, 49.8, 90.0, 150.0, 201.0, 2350.0, 2440.2]
    anchor_z = [0.0, 0.74, 1.82, 3.37, 4.6, 15.0, 15.5]
    depth_curve = PchipInterpolator(anchor_x, anchor_z)
    max_dist = 2440.2

    def _run(native_step: float):
        n = int(max_dist / native_step) + 1
        distances = [round(i * native_step, 1) for i in range(n)]
        raw = [
            {"distance_m": d, "depth_m": round(float(depth_curve(min(d, max_dist))), 4)}
            for d in distances
        ]
        interpolated = interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)
        bathy = np.array([[p["distance_m"], p["depth_m"]] for p in interpolated])
        return run_1d_analytical(
            hs=1.0, tp=10.3, direction=180.0, bathy_profile=bathy, gamma=0.73,
            beach_facing=180.0, cfjon=0.038,
        )

    res_fine = _run(2.0)
    res_coarse = _run(5.0)

    assert res_fine.break_points and res_coarse.break_points
    bp_fine, bp_coarse = res_fine.break_points[0], res_coarse.break_points[0]

    assert abs(bp_fine.distance_m - bp_coarse.distance_m) <= 5.0, (
        f"break point moved {abs(bp_fine.distance_m - bp_coarse.distance_m):.2f}m "
        "between native dx=2m and dx=5m"
    )
    assert abs(bp_fine.hs_m - bp_coarse.hs_m) <= 0.05, (
        f"face height differs by {abs(bp_fine.hs_m - bp_coarse.hs_m):.4f}m "
        "between native dx=2m and dx=5m"
    )


def test_interpolated_hb_scale_profile_finds_nonzero_break_point():
    """T4A.2's core Accept bullet: run_1d_analytical() with the PCHIP-
    interpolated profile finds >=1 break point with non-zero face height for
    1.0m Hs / 10.3s Tp input. Uses an HB-scale monotonic shelf profile
    (anchored to real depth facts from c:\\tmp\\marine-sep-P4A-scratch.md:
    depth(49.8m)=0.74m, depth(2350m)=15.0m, zero depth reversals) sampled at
    native ~8m resolution, matching T4A.2 step 3's input contract."""
    from scipy.interpolate import PchipInterpolator

    anchor_x = [0.0, 49.8, 90.0, 150.0, 201.0, 2350.0, 2440.2]
    anchor_z = [0.0, 0.74, 1.82, 3.37, 4.6, 15.0, 15.5]
    depth_curve = PchipInterpolator(anchor_x, anchor_z)

    max_dist = 2440.2
    step = 8.0
    n = int(max_dist / step) + 1
    distances = [round(i * step, 1) for i in range(n)]
    raw = [
        {"distance_m": d, "depth_m": round(float(depth_curve(min(d, max_dist))), 4)}
        for d in distances
    ]

    interpolated = interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73, structure_zone_depth=0.0)
    assert len(interpolated) > len(raw)

    bathy = np.array([[p["distance_m"], p["depth_m"]] for p in interpolated])
    res = run_1d_analytical(
        hs=1.0, tp=10.3, direction=180.0, bathy_profile=bathy, gamma=0.73,
        beach_facing=180.0, cfjon=0.038,
    )
    assert len(res.break_points) >= 1
    assert res.break_points[0].hs_m > 0.0
    # Physical-plausibility bound (brief §4.3): break depth should be a
    # shallow fraction of the offshore Hs, not tens of metres deep.
    assert 0.0 < res.break_points[0].depth_m < 5.0
