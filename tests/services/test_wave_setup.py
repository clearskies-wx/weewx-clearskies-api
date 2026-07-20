"""Tests for wave_setup.py (T7.1, SWAN-L3-STABILITY-PLAN Phase 7).

Coverage:
  - compute_setup_profile: physically reasonable setup for HB-Pier-like profile
  - compute_setup_profile: flat conditions produce all-zero setup
  - compute_setup_profile: setup is zero offshore of the break point
  - compute_setup_profile: setup increases monotonically from break to shore
  - compute_setup_profile: empty profile returns empty list
  - compute_setup_profile: depth-never-breaking profile returns zeros
  - build_wlevel_with_setup: grid shape, tide + setup composition, bearing
"""

from __future__ import annotations

from weewx_clearskies_api.services.wave_setup import (
    build_wlevel_with_setup,
    compute_setup_profile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slope_profile(
    slope: float = 0.01,      # depth / distance (m/m).  1:100 → slope=0.01
    max_dist_m: float = 200.0,
    n_points: int = 201,
) -> list[dict]:
    """Uniform-slope beach profile, shore (0 m) to offshore (max_dist_m).

    Profile convention:
        distance_m = 0 at shoreline, increasing offshore.
        depth_m    = −slope * distance_m  (negative = water, CUDEM convention).
    """
    step = max_dist_m / (n_points - 1)
    return [
        {
            "distance_m": i * step,
            "depth_m": -slope * i * step,
        }
        for i in range(n_points)
    ]


# ---------------------------------------------------------------------------
# Test 1 — physically reasonable setup for HB-Pier-like conditions
# ---------------------------------------------------------------------------


def test_setup_profile_hb_pier_like_setup_magnitude():
    """1 m offshore Hs on a 1:100 slope profile produces ~0.15–0.20 m shoreline setup.

    Profile geometry:
        201 points, 0–200 m offshore, depths 0 to −2.0 m.
    Offshore Hs = 1.0 m at depth 2.0 m → Hs/d = 0.5 (not breaking).
    Green's-law shoaling raises Hs/d to gamma at d_break ≈ 1.47 m (dist ≈ 147 m).
    Breaker height Hb = 0.73 × 1.47 ≈ 1.07 m.
    Expected shoreline setup ≈ 0.196 m (within 0.14–0.22 m tolerance for the
    discrete 1-m-spaced grid and the simplified Green's-law approximation).
    """
    profile = _make_slope_profile(slope=0.01, max_dist_m=200.0, n_points=201)
    result = compute_setup_profile(hs_offshore=1.0, tm01=10.0, profile=profile)

    assert len(result) == 201, "Result must have one entry per profile point"

    # Shoreline setup (distance_m = 0 is index 0)
    eta_shore = result[0]["setup_m"]
    assert 0.14 <= eta_shore <= 0.22, (
        f"Expected shoreline setup in [0.14, 0.22] m for 1 m offshore Hs on "
        f"1:100 slope, got {eta_shore:.4f} m"
    )

    # Distance_m must be preserved exactly
    for i, pt in enumerate(result):
        assert pt["distance_m"] == profile[i]["distance_m"], (
            f"distance_m mismatch at index {i}"
        )


# ---------------------------------------------------------------------------
# Test 2 — flat conditions produce zero setup everywhere
# ---------------------------------------------------------------------------


def test_setup_profile_flat_conditions_zero():
    """Hs < 0.1 m triggers the flat-conditions guard: all setup_m values = 0.0."""
    profile = _make_slope_profile(slope=0.01, max_dist_m=200.0, n_points=51)
    result = compute_setup_profile(hs_offshore=0.05, tm01=8.0, profile=profile)

    assert len(result) == 51
    for pt in result:
        assert pt["setup_m"] == 0.0, (
            f"Expected zero setup for flat conditions at distance={pt['distance_m']} m, "
            f"got {pt['setup_m']}"
        )


# ---------------------------------------------------------------------------
# Test 3 — setup is zero offshore of the break point
# ---------------------------------------------------------------------------


def test_setup_zero_offshore_of_break():
    """Every profile point seaward of the break point has setup_m == 0.0.

    The break point is at index ≈ 147 (distance ≈ 147 m) for the 1-m-spaced
    1:100 slope test profile with 1 m Hs and 2 m offshore depth.
    All points from index 148 onward must be exactly 0.0.
    """
    profile = _make_slope_profile(slope=0.01, max_dist_m=200.0, n_points=201)
    result = compute_setup_profile(hs_offshore=1.0, tm01=10.0, profile=profile)

    # Identify break point as the last (most offshore) entry with nonzero setup.
    break_dist: float | None = None
    for pt in reversed(result):
        if pt["setup_m"] != 0.0:
            break_dist = pt["distance_m"]
            break

    assert break_dist is not None, (
        "Expected at least one nonzero setup entry (the setdown at break point)"
    )

    # All points strictly offshore of break must have exactly zero setup.
    for pt in result:
        if pt["distance_m"] > break_dist:
            assert pt["setup_m"] == 0.0, (
                f"Expected zero setup at distance={pt['distance_m']} m "
                f"(seaward of break at {break_dist} m), got {pt['setup_m']:.6f}"
            )


# ---------------------------------------------------------------------------
# Test 4 — setup increases monotonically from break point toward shore
# ---------------------------------------------------------------------------


def test_setup_monotonically_increasing_toward_shore():
    """Within the surf zone, setup_m must be non-decreasing as distance_m decreases.

    Equivalently, for the surf-zone segment of the result (indices 0 through
    break_idx), each successive entry going shoreward (decreasing index,
    decreasing distance_m) must have setup_m >= the preceding one.
    """
    profile = _make_slope_profile(slope=0.01, max_dist_m=200.0, n_points=201)
    result = compute_setup_profile(hs_offshore=1.0, tm01=10.0, profile=profile)

    # Find the break index (last non-zero entry going from offshore end).
    break_idx: int | None = None
    for i in range(len(result) - 1, -1, -1):
        if result[i]["setup_m"] != 0.0:
            break_idx = i
            break

    assert break_idx is not None, "Expected a break point with non-zero setup"
    assert break_idx > 0, "Break point must be at index > 0 (not at shoreline)"

    # From shore (index 0) toward break (index break_idx):
    # result[i]["setup_m"] should be >= result[i+1]["setup_m"]
    # (setup decreases as we move away from shore toward the break point).
    # range(break_idx) covers pairs (0,1) … (break_idx-1, break_idx) inclusive.
    for i in range(break_idx):
        assert result[i]["setup_m"] >= result[i + 1]["setup_m"] - 1e-9, (
            f"Setup not monotone at index {i}: "
            f"result[{i}]={result[i]['setup_m']:.6f} < "
            f"result[{i+1}]={result[i+1]['setup_m']:.6f}"
        )


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


def test_setup_profile_empty_profile():
    """Empty profile returns an empty list — no crash, no setup."""
    result = compute_setup_profile(hs_offshore=1.5, tm01=10.0, profile=[])
    assert result == [], f"Expected empty list, got {result}"


def test_setup_profile_no_breaking():
    """If wave height never reaches gamma*d in the profile, all zeros are returned.

    Hs_offshore = 0.01 m is far below the 0.73 * d threshold at any depth
    in the test profile (minimum gamma*d on a 2 m deep profile = 0.073 m).
    """
    profile = _make_slope_profile(slope=0.01, max_dist_m=200.0, n_points=51)
    result = compute_setup_profile(hs_offshore=0.01, tm01=8.0, profile=profile)
    # Hs=0.01 m < 0.1 threshold → treated as flat conditions
    for pt in result:
        assert pt["setup_m"] == 0.0


# ---------------------------------------------------------------------------
# Tests for build_wlevel_with_setup
# ---------------------------------------------------------------------------


def test_build_wlevel_grid_shape():
    """Returned grid is (myc+1) rows × (mxc+1) columns."""
    grid_dims = {
        "x_sw": 400000.0, "y_sw": 3700000.0,
        "mxc": 10, "myc": 8,
        "dx": 10.0, "dy": 10.0,
    }
    setup_profile = [
        {"distance_m": 0.0, "setup_m": 0.15},
        {"distance_m": 200.0, "setup_m": 0.0},
    ]
    grid = build_wlevel_with_setup(
        tide_value=0.5,
        setup_profile=setup_profile,
        grid_dims=grid_dims,
        bearing=0.0,
    )
    assert len(grid) == 9, f"Expected 9 rows (myc+1), got {len(grid)}"
    for row in grid:
        assert len(row) == 11, f"Expected 11 cols (mxc+1), got {len(row)}"


def test_build_wlevel_north_bearing_values():
    """For bearing=0 (north = offshore), setup varies with row (j), not column (i).

    With bearing=0: sin_b=0, cos_b=1, j_shore=0 (south edge = shore).
    cross_shore_dist(i, j) = j * dy.

    Grid: mxc=20, myc=20, dx=10, dy=10.
    Setup profile: 0 m (eta=0.15) → 100 m (eta=0.05) → 200 m (eta=0.0).

    Checks:
        j=0  (cs=0 m)   → wlevel = tide + 0.15 = 0.65
        j=10 (cs=100 m) → wlevel = tide + 0.05 = 0.55
        j=20 (cs=200 m) → wlevel = tide + 0.00 = 0.50
        All columns (i) in same row must have identical wlevel (uniform along-shore).
    """
    tide = 0.5
    setup_profile = [
        {"distance_m": 0.0, "setup_m": 0.15},
        {"distance_m": 100.0, "setup_m": 0.05},
        {"distance_m": 200.0, "setup_m": 0.0},
    ]
    grid_dims = {
        "x_sw": 0.0, "y_sw": 0.0,
        "mxc": 20, "myc": 20,
        "dx": 10.0, "dy": 10.0,
    }
    grid = build_wlevel_with_setup(
        tide_value=tide,
        setup_profile=setup_profile,
        grid_dims=grid_dims,
        bearing=0.0,
    )

    # At j=0 (shore): wlevel = 0.5 + 0.15 = 0.65
    for i in range(21):
        assert abs(grid[0][i] - 0.65) < 1e-9, (
            f"j=0 i={i}: expected 0.65, got {grid[0][i]}"
        )

    # At j=10 (100 m offshore): wlevel = 0.5 + 0.05 = 0.55
    for i in range(21):
        assert abs(grid[10][i] - 0.55) < 1e-9, (
            f"j=10 i={i}: expected 0.55, got {grid[10][i]}"
        )

    # At j=20 (200 m = profile offshore end): wlevel = 0.5 + 0.0 = 0.50
    for i in range(21):
        assert abs(grid[20][i] - 0.50) < 1e-9, (
            f"j=20 i={i}: expected 0.50, got {grid[20][i]}"
        )

    # All columns in the same row are identical (uniform along-shore assumption)
    for j in range(21):
        values = [grid[j][i] for i in range(21)]
        assert max(values) - min(values) < 1e-12, (
            f"Row j={j} has column variation: {values[:5]}"
        )


def test_build_wlevel_empty_setup_profile_is_tide_only():
    """Empty setup_profile → every cell equals tide_value (no setup added)."""
    tide = 1.23
    grid_dims = {
        "x_sw": 0.0, "y_sw": 0.0,
        "mxc": 5, "myc": 5,
        "dx": 10.0, "dy": 10.0,
    }
    grid = build_wlevel_with_setup(
        tide_value=tide,
        setup_profile=[],
        grid_dims=grid_dims,
        bearing=45.0,
    )
    for j in range(6):
        for i in range(6):
            assert abs(grid[j][i] - tide) < 1e-9, (
                f"Expected tide-only {tide} at [{j}][{i}], got {grid[j][i]}"
            )


def test_build_wlevel_beyond_profile_is_tide_only():
    """Grid cells farther offshore than max(profile distance) receive tide_value only."""
    tide = 0.3
    # Profile extends to only 50 m; grid extends to 200 m offshore.
    setup_profile = [
        {"distance_m": 0.0, "setup_m": 0.10},
        {"distance_m": 50.0, "setup_m": 0.0},
    ]
    grid_dims = {
        "x_sw": 0.0, "y_sw": 0.0,
        "mxc": 1, "myc": 20,  # 20 cells × 10 m = 200 m
        "dx": 10.0, "dy": 10.0,
    }
    # bearing=0: j=0 is shore, j increases offshore; cs_dist = j * 10
    grid = build_wlevel_with_setup(
        tide_value=tide,
        setup_profile=setup_profile,
        grid_dims=grid_dims,
        bearing=0.0,
    )
    # j=5 → cs_dist = 50 m (at offshore boundary of profile) → eta=0 → wlevel=tide
    assert abs(grid[5][0] - tide) < 1e-9, f"Expected {tide} at j=5, got {grid[5][0]}"

    # j=10 → cs_dist = 100 m > 50 m → beyond profile → wlevel=tide
    assert abs(grid[10][0] - tide) < 1e-9, f"Expected {tide} at j=10, got {grid[10][0]}"
    assert abs(grid[20][0] - tide) < 1e-9, f"Expected {tide} at j=20, got {grid[20][0]}"
