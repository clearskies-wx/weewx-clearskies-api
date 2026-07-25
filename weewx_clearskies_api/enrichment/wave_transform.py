"""Sub-grid bilinear interpolation helper (Phase 3, T3.1; reduced T4A.7).

Originally the SWAN nearshore supplement processor (four numbered
supplements per API-MANUAL.md §17), registered against the surf scoring
pipeline and run after SwanProvider.fetch(), before ``surf_scorer.py``.
That role is gone as of T4A.7 (2026-07-25):

1. Breaker index correction (Battjes 1974 gamma tuning) — REMOVED. Its
   guard required ``spot_config.bathymetric_profile``, which
   ``config/marine_config.py`` no longer reads (``SurfSpotConfig`` has no
   such field), so the branch never executed. It was also superseded in
   principle: ``services/surf_1d_pipeline.py``'s ``run_1d_analytical()``
   already computes local slope and Iribarren number at every point from
   the real CUDEM profile — finer than this module's single configured
   ``beach_slope`` scalar.
2. Coastal structure transmission/reflection effects — REMOVED per
   ADR-095; handled natively by the SWAN OBSTACLE command during wave
   propagation (T2.3).
3. Sub-grid spatial interpolation (bilinear) — REMOVED as a *supplement*
   per T4A.7's verification gate: the SWAN handoff spectrum is emitted via
   ``POINTS`` at explicit UTM coordinates followed by ``SPECOUT``
   (``services/swan_formats.py``, ``services/swan_runner.py``), and SWAN
   bilinearly interpolates POINTS/CURVE/SPECOUT output from the four
   surrounding computational-grid nodes to that exact coordinate (SWAN
   user manual §2.6.4, §4.6.1; source ``swanout1.ftn`` ``SWOEXA``/
   ``SWOEXD``). There is no leftover grid-cell-centre value left for this
   module to refine. It was also already unreachable in practice — the
   only historical caller never populated ``grid_data``/``grid_lats``/
   ``grid_lons``.
4. Topographic wave focusing/sheltering — REMOVED per ADR-093 Amendment 2
   §5b (2026-07-25). The multipliers (point break x1.1, headland x1.2,
   bay break x0.9, straight beach x1.0) stood in for refraction SWAN now
   computes; once L2 existed at 100 m it began computing that refraction
   itself, so the multipliers had been double-counting since nesting
   landed. The operator's topographic classification is retained in spot
   config — its job is now the L3 enable trigger (PROVIDER-MANUAL §14.15),
   not a wave-height adjustment.

**What remains: ``bilinear_interpolate()``.** It has an unrelated live
caller — ``endpoints/surf.py`` uses it to interpolate HRRR wind U/V grid
components to a spot's coordinates for surf-quality scoring. That is not
a SWAN wave-height supplement; it stays for that reason alone (LC-R2-15,
2026-07-25). This module's name is now broader than its remaining
contents — flagged as a finding, not renamed (renaming/moving would be an
architectural change outside this task's scope).

**What is NOT supplemented:** Shoaling, refraction, bottom friction,
wave-current interaction. SWAN computes these with its own bathymetry
and RTOFS currents; re-running them here would duplicate SWAN's work
without improving it. Do not add ``calculate_shoaling_coefficient`` or
``calculate_refraction_coefficient`` functions to this module.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sub-grid spatial interpolation (bilinear)
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
