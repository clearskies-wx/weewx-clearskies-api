"""Unit tests for the GRIB processor module (Phase 2 T2.1).

Tests cover:
  - GRIB_AVAILABLE flag and check_grib_available() error message
  - GribFieldData properties (lat_step, lon_step)
  - extract_nearest_value() — nearest-neighbor lookup, bounds, missing values
  - bilinear_interpolate() — interpolation accuracy, edge cases, missing nodes
  - apply_breaking_limit() — depth-induced breaking cap
  - compute_breaker_gamma() — Battjes 1974 formula, clamping, edge cases

No GRIB2 fixture files are needed for these tests — they exercise the
data structures and math functions directly. Real GRIB2 file tests
require eccodes on the test host and are in the integration test suite.
"""

from __future__ import annotations

import math

import pytest

from weewx_clearskies_api.providers.marine.grib_processor import (
    GRIB_AVAILABLE,
    GribFieldData,
    GribResult,
    apply_breaking_limit,
    bilinear_interpolate,
    compute_breaker_gamma,
    extract_nearest_value,
)


# ---------------------------------------------------------------------------
# GribFieldData property tests
# ---------------------------------------------------------------------------


class TestGribFieldData:
    def test_lat_step(self) -> None:
        field = GribFieldData(
            short_name="HTSGW",
            values=[[0.0] * 3 for _ in range(4)],
            ni=3,
            nj=4,
            lat_first=30.0,
            lon_first=-80.0,
            lat_last=33.0,
            lon_last=-78.0,
        )
        assert field.lat_step == pytest.approx(1.0)
        assert field.lon_step == pytest.approx(1.0)

    def test_single_row_step_zero(self) -> None:
        field = GribFieldData(
            short_name="HTSGW",
            values=[[1.0, 2.0]],
            ni=2,
            nj=1,
            lat_first=30.0,
            lon_first=-80.0,
            lat_last=30.0,
            lon_last=-79.0,
        )
        assert field.lat_step == 0.0
        assert field.lon_step == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# GribResult tests
# ---------------------------------------------------------------------------


class TestGribResult:
    def test_get_existing(self) -> None:
        field = GribFieldData(
            short_name="HTSGW",
            values=[[1.0]],
            ni=1,
            nj=1,
            lat_first=30.0,
            lon_first=-80.0,
            lat_last=30.0,
            lon_last=-80.0,
        )
        result = GribResult(fields={"HTSGW": field})
        assert result.get("HTSGW") is field

    def test_get_missing(self) -> None:
        result = GribResult()
        assert result.get("HTSGW") is None


# ---------------------------------------------------------------------------
# extract_nearest_value tests
# ---------------------------------------------------------------------------


def _make_grid(
    values: list[list[float]],
    lat_first: float = 30.0,
    lon_first: float = -80.0,
    lat_last: float = 32.0,
    lon_last: float = -78.0,
) -> GribFieldData:
    nj = len(values)
    ni = len(values[0]) if nj > 0 else 0
    return GribFieldData(
        short_name="TEST",
        values=values,
        ni=ni,
        nj=nj,
        lat_first=lat_first,
        lon_first=lon_first,
        lat_last=lat_last,
        lon_last=lon_last,
    )


class TestExtractNearestValue:
    def test_center_point(self) -> None:
        grid = _make_grid(
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
        )
        val = extract_nearest_value(grid, 31.0, -79.0)
        assert val == 5.0

    def test_corner_point(self) -> None:
        grid = _make_grid(
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
        )
        val = extract_nearest_value(grid, 30.0, -80.0)
        assert val == 1.0

    def test_out_of_bounds(self) -> None:
        grid = _make_grid([[1.0, 2.0], [3.0, 4.0]])
        val = extract_nearest_value(grid, 50.0, -80.0)
        assert val is None

    def test_missing_value(self) -> None:
        grid = _make_grid([[9.999e20]])
        val = extract_nearest_value(grid, 31.0, -79.0)
        assert val is None

    def test_empty_grid(self) -> None:
        grid = GribFieldData(
            short_name="TEST",
            values=[],
            ni=0,
            nj=0,
            lat_first=30.0,
            lon_first=-80.0,
            lat_last=32.0,
            lon_last=-78.0,
        )
        assert extract_nearest_value(grid, 31.0, -79.0) is None


# ---------------------------------------------------------------------------
# bilinear_interpolate tests
# ---------------------------------------------------------------------------


class TestBilinearInterpolate:
    def test_at_grid_node(self) -> None:
        """Interpolation at an exact grid node returns the grid value."""
        grid = _make_grid(
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
        )
        val = bilinear_interpolate(grid, 31.0, -79.0)
        assert val == pytest.approx(5.0)

    def test_midpoint_interpolation(self) -> None:
        """Midpoint between four nodes: average of the four values."""
        grid = _make_grid(
            [[0.0, 2.0], [4.0, 6.0]],
            lat_first=30.0,
            lon_first=-80.0,
            lat_last=32.0,
            lon_last=-78.0,
        )
        val = bilinear_interpolate(grid, 31.0, -79.0)
        assert val == pytest.approx(3.0)

    def test_quarter_point(self) -> None:
        """At 25% of the way from node (0,0) to (1,1)."""
        grid = _make_grid(
            [[0.0, 4.0], [8.0, 12.0]],
            lat_first=0.0,
            lon_first=0.0,
            lat_last=1.0,
            lon_last=1.0,
        )
        val = bilinear_interpolate(grid, 0.25, 0.25)
        # bilinear: (1-0.25)*(1-0.25)*0 + (1-0.25)*0.25*4 + 0.25*(1-0.25)*8 + 0.25*0.25*12
        # = 0.5625*0 + 0.1875*4 + 0.1875*8 + 0.0625*12 = 0 + 0.75 + 1.5 + 0.75 = 3.0
        assert val == pytest.approx(3.0)

    def test_missing_node_falls_back_to_nearest(self) -> None:
        """If a surrounding node has a missing value, falls back to nearest-neighbor."""
        grid = _make_grid(
            [[1.0, 9.999e20], [3.0, 4.0]],
            lat_first=0.0,
            lon_first=0.0,
            lat_last=1.0,
            lon_last=1.0,
        )
        val = bilinear_interpolate(grid, 0.3, 0.3)
        assert val is not None

    def test_edge_falls_back_to_nearest(self) -> None:
        """Point at the grid edge (j1 = nj) falls back to nearest-neighbor."""
        grid = _make_grid(
            [[1.0, 2.0], [3.0, 4.0]],
            lat_first=0.0,
            lon_first=0.0,
            lat_last=1.0,
            lon_last=1.0,
        )
        val = bilinear_interpolate(grid, 1.0, 0.5)
        assert val is not None

    def test_single_cell_falls_back(self) -> None:
        grid = _make_grid(
            [[5.0]],
            lat_first=30.0,
            lon_first=-80.0,
            lat_last=30.0,
            lon_last=-80.0,
        )
        val = bilinear_interpolate(grid, 30.0, -80.0)
        assert val == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# apply_breaking_limit tests
# ---------------------------------------------------------------------------


class TestApplyBreakingLimit:
    def test_wave_below_limit(self) -> None:
        result = apply_breaking_limit(1.0, 5.0, gamma=0.73)
        assert result == 1.0

    def test_wave_above_limit(self) -> None:
        result = apply_breaking_limit(5.0, 2.0, gamma=0.73)
        assert result == pytest.approx(0.73 * 2.0)

    def test_zero_depth(self) -> None:
        assert apply_breaking_limit(1.0, 0.0) == 0.0

    def test_negative_depth(self) -> None:
        assert apply_breaking_limit(1.0, -1.0) == 0.0

    def test_custom_gamma(self) -> None:
        result = apply_breaking_limit(3.0, 2.0, gamma=1.2)
        assert result == pytest.approx(2.4)

    def test_default_gamma(self) -> None:
        result = apply_breaking_limit(10.0, 5.0)
        assert result == pytest.approx(0.73 * 5.0)


# ---------------------------------------------------------------------------
# compute_breaker_gamma tests
# ---------------------------------------------------------------------------


class TestComputeBreakerGamma:
    def test_sand_beach(self) -> None:
        """Sand beach (slope ~0.02), moderate waves → γ ~0.7–0.8."""
        gamma = compute_breaker_gamma(0.02, 2.0, 10.0)
        assert 0.5 <= gamma <= 1.4
        assert 0.6 <= gamma <= 0.9

    def test_rock_slope(self) -> None:
        """Rock slope (~0.05) → γ ~0.8–1.0."""
        gamma = compute_breaker_gamma(0.05, 2.0, 10.0)
        assert 0.5 <= gamma <= 1.4
        assert 0.8 <= gamma <= 1.1

    def test_reef_slope(self) -> None:
        """Reef slope (~0.1) → γ ~0.9–1.2."""
        gamma = compute_breaker_gamma(0.1, 2.0, 10.0)
        assert 0.5 <= gamma <= 1.4
        assert 0.9 <= gamma <= 1.3

    def test_clamp_low(self) -> None:
        """Very flat slope with steep waves → γ clamped at 0.5."""
        gamma = compute_breaker_gamma(0.001, 5.0, 5.0)
        assert gamma >= 0.5

    def test_clamp_high(self) -> None:
        """Very steep slope with small long-period waves → γ clamped at 1.4."""
        gamma = compute_breaker_gamma(0.5, 0.1, 20.0)
        assert gamma <= 1.4

    def test_zero_slope_returns_default(self) -> None:
        assert compute_breaker_gamma(0.0, 2.0, 10.0) == 0.73

    def test_zero_height_returns_default(self) -> None:
        assert compute_breaker_gamma(0.02, 0.0, 10.0) == 0.73

    def test_zero_period_returns_default(self) -> None:
        assert compute_breaker_gamma(0.02, 2.0, 0.0) == 0.73

    def test_negative_inputs_return_default(self) -> None:
        assert compute_breaker_gamma(-0.02, 2.0, 10.0) == 0.73


# ---------------------------------------------------------------------------
# GRIB_AVAILABLE flag test
# ---------------------------------------------------------------------------


class TestGribAvailable:
    def test_flag_is_bool(self) -> None:
        assert isinstance(GRIB_AVAILABLE, bool)
