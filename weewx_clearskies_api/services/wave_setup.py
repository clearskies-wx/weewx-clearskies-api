"""Analytic wave setup computation for SWAN WLEVEL injection.

Computes the wave-induced mean water level rise (setup) in the surf zone
using the radiation-stress balance (Longuet-Higgins & Stewart 1964,
USACE CEM II-4).  Delivers the result through the WLEVEL input grid that
SWAN already reads — parallel-safe and nest-BC-correct.

Design rationale (SWAN-L3-STABILITY-BRIEF §5.3 Stage 2):
  SWAN's SETUP command is unsupported in parallel runs (A1) and ill-posed in
  nested models (A2).  The equivalent physics can be injected through WLEVEL
  because SWAN adds the WLEVEL field to BOTTOM depth before the solve:
      depth_model(x, y) = BOTTOM(x, y) + WLEVEL(x, y, t) [+ SETUP, which is now 0]
  Delivering (tide + η) through WLEVEL is therefore mathematically identical
  to SWAN's internal setup from the wave model's perspective.

Used in the Stage 2 WLEVEL composition sequence (T7.1):
  1.  L2 runs → TABLE output of Hs / TM01 at the L3 offshore boundary.
  2.  Runner calls compute_setup_profile() with L2's Hs and the cached
      bidirectional profile.
  3.  Runner calls build_wlevel_with_setup() to compose the L3 WLEVEL grid:
          wlevel(x, y, t) = coops_tide(t) + eta(cross-shore distance)
  4.  L3 runs, reading corrected depth.

This module is pure computation — no file I/O, no SWAN INPUT syntax.

References:
    SWAN-L3-STABILITY-BRIEF §5.3
    Longuet-Higgins, M.S. & Stewart, R.W. (1964). Radiation stresses
        in water waves; a physical discussion with applications. Deep-Sea Res.,
        11, 529-562.
    USACE Coastal Engineering Manual (2002), Part II Chapter 4.
"""

from __future__ import annotations

import bisect
import logging
import math

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Setup profile computation
# ---------------------------------------------------------------------------


def compute_setup_profile(
    hs_offshore: float,
    tm01: float,
    profile: list[dict],
    gamma: float = 0.73,
) -> list[dict]:
    """Compute wave setup profile from the radiation-stress balance.

    Shoals *hs_offshore* along the cached cross-shore profile to locate
    the break point (where Hs / d >= gamma), then integrates the
    Longuet-Higgins & Stewart (1964) setup gradient landward from there.

    Shoaling approximation — Green's law (shallow water):
        Hs(d) = Hs_offshore * (d_offshore / d) ** 0.25

    Setup gradient inside the surf zone (USACE CEM II-4-39):
        deta/dx = -K * (dd/dx)
        K = 1 / (1 + 8 / (3 * gamma**2))

    At the break point, a small setdown is applied:
        eta_b = -(1/16) * gamma**2 * d_break

    Args:
        hs_offshore: Significant wave height at the OFFSHORE end of the
            profile (m), typically the L2 TABLE value at the L3 seaward
            boundary.
        tm01: Mean wave period (s) — available for future dispersion-based
            shoaling refinement; not used in the current Green's-law
            approximation.
        profile: Cross-shore profile ordered SHORE to OFFSHORE.  Each
            entry is ``{"distance_m": float, "depth_m": float}`` where
            ``distance_m == 0`` at the shoreline and ``depth_m`` is
            *negative* for submerged cells (CUDEM sign convention).
        gamma: Breaking index (Hs/d at breaking).  Default 0.73 matches
            ``BREAKING CONSTANT 1.0 0.73`` in the SWAN INPUT.

    Returns:
        List of ``{"distance_m": float, "setup_m": float}``, one entry per
        input profile point in the same shore-to-offshore order.
        ``setup_m`` is 0.0 seaward of the break point, small negative at
        the break (setdown), and positive increasing toward shore.

    Edge cases — all return all-zero setup (logged at INFO):
        * ``hs_offshore < 0.1 m``: flat conditions, no breaking.
        * Empty profile: no entries to process.
        * No break point found within the profile: waves stay below gamma*d.
    """
    # --- Edge case: empty profile ---
    if not profile:
        logger.info(
            "Wave setup: no breaking detected (Hs=%.2fm, max_depth=%.1fm) — zero setup",
            hs_offshore,
            0.0,
        )
        return []

    n = len(profile)

    # Convert CUDEM negative-water depth_m to positive water depth for math.
    # profile[0] = shore (~0 m depth), profile[-1] = offshore (~−15 m depth_m).
    depths: list[float] = [max(0.0, -p["depth_m"]) for p in profile]

    d_offshore = depths[-1]

    # --- Edge case: flat conditions ---
    if hs_offshore < 0.1:
        logger.info(
            "Wave setup: no breaking detected (Hs=%.2fm, max_depth=%.1fm) — zero setup",
            hs_offshore,
            d_offshore,
        )
        return [{"distance_m": p["distance_m"], "setup_m": 0.0} for p in profile]

    # --- Edge case: degenerate (zero-depth) offshore end ---
    if d_offshore < 1e-3:
        logger.info(
            "Wave setup: no breaking detected (Hs=%.2fm, max_depth=%.1fm) — zero setup",
            hs_offshore,
            d_offshore,
        )
        return [{"distance_m": p["distance_m"], "setup_m": 0.0} for p in profile]

    # --- Step 1: Walk from offshore toward shore; locate break point ---
    #
    # Green's law (shallow-water shoaling, Hs proportional to d^{-1/4}):
    #     Hs_local = Hs_offshore * (d_offshore / d_local) ** 0.25
    #
    # Breaking criterion: Hs_local / d_local >= gamma.
    # Walk from the LAST profile index (deepest / offshore) toward index 0 (shore).
    break_idx: int | None = None
    d_break: float = 0.0

    for i in range(n - 1, -1, -1):
        d_local = depths[i]
        if d_local < 1e-3:
            continue  # at or above waterline — skip dry/zero-depth cells
        hs_local = hs_offshore * (d_offshore / d_local) ** 0.25
        if hs_local / d_local >= gamma:
            break_idx = i
            d_break = d_local
            break

    # --- Edge case: waves never reach breaking depth within the profile ---
    if break_idx is None:
        logger.info(
            "Wave setup: no breaking detected (Hs=%.2fm, max_depth=%.1fm) — zero setup",
            hs_offshore,
            d_offshore,
        )
        return [{"distance_m": p["distance_m"], "setup_m": 0.0} for p in profile]

    # --- Step 2: Integrate setup from break point toward shore ---
    #
    # K = (3*gamma^2/8) / (1 + 3*gamma^2/8) = 1 / (1 + 8/(3*gamma^2))
    # For gamma = 0.73: K ≈ 0.167
    # Reference: USACE CEM Eq. (II-4-37) simplified for shallow-water surf zone.
    K = 1.0 / (1.0 + 8.0 / (3.0 * gamma ** 2))

    setup: list[float] = [0.0] * n  # zero everywhere; overwritten in surf zone

    # Setdown at the break point (negative; waves gain potential energy here)
    eta_b = -(1.0 / 16.0) * gamma ** 2 * d_break
    setup[break_idx] = eta_b

    # Integrate shoreward from break_idx toward index 0:
    #   eta[i] = eta[i+1] + K * (depths[i+1] - depths[i])
    # i+1 is the point just seaward (deeper); i is the next step toward shore.
    # depth decreases toward shore → (depths[i+1] - depths[i]) > 0 → eta rises.
    for i in range(break_idx - 1, -1, -1):
        d_prev = depths[i + 1]  # seaward point (deeper)
        d_curr = depths[i]      # shoreward point (shallower)
        setup[i] = setup[i + 1] + K * (d_prev - d_curr)

    # --- Logging ---
    hb = gamma * d_break
    dist_break = profile[break_idx]["distance_m"]
    eta_max = setup[0]  # index 0 = shoreline = maximum setup
    logger.info(
        "Wave setup: Hb=%.2fm at %.0fm from shore, eta_max=%.3fm (shoreline)",
        hb,
        dist_break,
        eta_max,
    )

    return [
        {"distance_m": profile[i]["distance_m"], "setup_m": setup[i]}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# WLEVEL grid composition
# ---------------------------------------------------------------------------


def build_wlevel_with_setup(
    tide_value: float,
    setup_profile: list[dict],
    grid_dims: dict,
    bearing: float,
) -> list[list[float]]:
    """Compose a 2-D WLEVEL grid: tide + wave setup at each grid cell.

    Each grid point receives::

        wlevel[j][i] = tide_value + eta(cross_shore_distance)

    where ``eta`` is linearly interpolated from *setup_profile*.  Setup is
    assumed uniform in the along-shore direction (open straight beach).

    Cross-shore distance for each grid point (i, j) is computed as the
    projection of the point's position onto the offshore bearing unit vector,
    measured from the shore-side corner of the grid.  The shore-side corner
    is the grid corner whose cross-shore projection is smallest — i.e. the
    corner that lies closest to land given the beach orientation.

    The returned layout matches SWAN's INPGRID / READINP WLEVEL convention:

    * **Outer list (rows):** south → north, index j = 0 … myc.
    * **Inner list (columns):** west → east, index i = 0 … mxc.
    * Total size: ``(myc + 1)`` rows × ``(mxc + 1)`` columns.

    Args:
        tide_value: CO-OPS tidal water level (m) for this timestep.
        setup_profile: Output of :func:`compute_setup_profile` —
            ``[{"distance_m": float, "setup_m": float}, …]`` ordered
            shore (distance 0) → offshore.  May be empty; in that case
            every cell receives ``tide_value`` with no setup added.
        grid_dims: Mapping with keys:

            * ``x_sw`` — UTM easting of SW corner (m).
            * ``y_sw`` — UTM northing of SW corner (m).
            * ``mxc`` — mesh count in x (number of cells; columns = mxc+1).
            * ``myc`` — mesh count in y (number of cells; rows    = myc+1).
            * ``dx``  — mesh size in x direction (m).
            * ``dy``  — mesh size in y direction (m).
        bearing: Offshore (beach-facing) direction in compass degrees
            (0 = north, 90 = east, 180 = south, 270 = west).  Cross-shore
            distance increases in this direction away from shore.

    Returns:
        ``grid[j][i]`` — water level (m) at SWAN grid point (i, j).
        Grid points outside the setup profile range (open ocean beyond the
        profile's offshore end) receive ``tide_value`` with no setup.
    """
    mxc = int(grid_dims["mxc"])
    myc = int(grid_dims["myc"])
    dx = float(grid_dims["dx"])
    dy = float(grid_dims["dy"])

    bearing_rad = math.radians(float(bearing))
    sin_b = math.sin(bearing_rad)
    cos_b = math.cos(bearing_rad)

    # Shore-side column/row indices:
    # The cross-shore projection of grid point (i, j) relative to the
    # SW corner is:
    #   cs_raw = (x_sw + i*dx)*sin_b + (y_sw + j*dy)*cos_b
    #
    # The shore-side corner minimises cs_raw.  Because the formula is linear
    # in i and j, cs_raw is minimum at:
    #   i_shore = mxc (east edge) when sin_b < 0
    #   i_shore = 0   (west edge) when sin_b >= 0
    #   j_shore = myc (north edge) when cos_b < 0
    #   j_shore = 0   (south edge) when cos_b >= 0
    #
    # The UTM offset (x_sw, y_sw) cancels in the relative formula:
    #   cs_dist(i, j) = cs_raw(i, j) - cs_raw(i_shore, j_shore)
    #                 = (i - i_shore)*dx*sin_b + (j - j_shore)*dy*cos_b  >= 0
    i_shore: int = mxc if sin_b < 0.0 else 0
    j_shore: int = myc if cos_b < 0.0 else 0

    # --- Build interpolation table from setup profile ---
    if setup_profile:
        profile_dist: list[float] = [p["distance_m"] for p in setup_profile]
        profile_eta: list[float] = [p["setup_m"] for p in setup_profile]
        max_dist: float = profile_dist[-1]
    else:
        profile_dist = [0.0]
        profile_eta = [0.0]
        max_dist = 0.0

    def _interp(dist: float) -> float:
        """Linearly interpolate setup (m) at cross-shore distance *dist* (m)."""
        if dist <= profile_dist[0]:
            return profile_eta[0]
        if dist >= max_dist:
            return 0.0  # offshore of profile: no setup contribution
        idx = bisect.bisect_right(profile_dist, dist) - 1
        # idx is guaranteed in [0, len-2] here (dist is strictly between two knots)
        span = profile_dist[idx + 1] - profile_dist[idx]
        if span <= 0.0:
            return profile_eta[idx]
        t = (dist - profile_dist[idx]) / span
        return profile_eta[idx] + t * (profile_eta[idx + 1] - profile_eta[idx])

    # --- Build 2-D grid: [j][i] = wlevel ---
    grid: list[list[float]] = []
    for j in range(myc + 1):
        row: list[float] = []
        for i in range(mxc + 1):
            # Cross-shore distance from shore (m); always >= 0 by construction
            cs_dist: float = (
                (i - i_shore) * dx * sin_b + (j - j_shore) * dy * cos_b
            )
            cs_dist = max(0.0, cs_dist)  # clamp against floating-point rounding
            eta: float = _interp(cs_dist)
            row.append(tide_value + eta)
        grid.append(row)

    return grid
