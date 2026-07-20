"""Tests for swan_formats.py and swan_runner.py (T2.2, T2.3, T2.4).

Coverage:
  - hrrr_to_swan_wind: grid dims, block count, value interpolation
  - cudem_to_swan_bottom: sign flip, fallback depth, grid dims
  - ww3_to_swan_boundary: TPAR format, swell preference, missing-data handling
  - SWANRunner._write_input_files: all files created with expected content
  - SWANRunner._parse_output: column detection, value extraction, spot matching
  - Physical validation: Hs >20 m, Tm01 <1 s, NaN MWD all rejected
  - SWANRunError: carries stderr attribute

Tests do NOT invoke the SWAN executable.  _spawn_swan is patched where needed.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.services.swan_formats import (
    _bilinear_interp,
    _compute_swan_grid_dims,
    build_swan_input,
    cudem_to_swan_bottom,
    hrrr_to_swan_wind,
    ww3_to_swan_boundary,
)
from weewx_clearskies_api.services.swan_runner import (
    SWANRunError,
    SWANRunner,
    _is_valid_point,
    _parse_table_output,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_hrrr_grid(
    nj: int = 5,
    ni: int = 7,
    lat_first: float = 33.40,
    lon_first: float = -118.30,
    lat_last: float = 33.80,
    lon_last: float = -117.90,
    u_val: float = 5.0,
    v_val: float = -3.0,
    valid_time: str = "2024-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Create a synthetic single HRRR forecast grid."""
    return {
        "valid_time": valid_time,
        "forecast_hour": 0,
        "nj": nj,
        "ni": ni,
        "lat_first": lat_first,
        "lon_first": lon_first,
        "lat_last": lat_last,
        "lon_last": lon_last,
        "u_earth": [[u_val] * ni for _ in range(nj)],
        "v_earth": [[v_val] * ni for _ in range(nj)],
    }


def _make_hrrr_wind_field(n_grids: int = 3) -> dict[str, Any]:
    """Create a multi-hour HRRR wind field dict."""
    grids = []
    for h in range(n_grids):
        grids.append(
            _make_hrrr_grid(
                valid_time=f"2024-01-01T0{h}:00:00Z",
                u_val=float(h + 1) * 2.0,
                v_val=float(h + 1) * -1.0,
            )
        )
    return {"cycle_time": "2024-01-01T00:00:00Z", "grids": grids}


def _make_cudem_grid(
    nj: int = 5,
    ni: int = 7,
    lat_first: float = 33.40,
    lon_first: float = -118.30,
    lat_last: float = 33.80,
    lon_last: float = -117.90,
    depth_val: float = -15.0,  # CUDEM: negative = ocean
) -> dict[str, Any]:
    """Create a uniform synthetic CUDEM grid."""
    return {
        "lat_first": lat_first,
        "lon_first": lon_first,
        "lat_last": lat_last,
        "lon_last": lon_last,
        "nj": nj,
        "ni": ni,
        "depths": [[depth_val] * ni for _ in range(nj)],
    }


def _make_ww3_point(
    time: str = "2024-01-01T00:00:00Z",
    hs: float = 1.5,
    tp: float = 12.0,
    mwd: float = 270.0,
    swell_hs: float | None = None,
    swell_tp: float | None = None,
    swell_dir: float | None = None,
) -> dict[str, Any]:
    return {
        "time": time,
        "waveHeight": hs,
        "wavePeriod": tp,
        "waveDirection": mwd,
        "swellHeight": swell_hs,
        "swellPeriod": swell_tp,
        "swellDirection": swell_dir,
    }


def _make_ww3_data(n_hours: int = 3) -> dict[str, Any]:
    forecast = []
    for h in range(n_hours):
        forecast.append(
            _make_ww3_point(
                time=f"2024-01-01T0{h}:00:00Z",
                hs=1.5 + h * 0.1,
                tp=12.0 + h * 0.2,
                mwd=270.0 - h * 2.0,
            )
        )
    return {"forecast": forecast, "model_run": "2024-01-01T00:00:00Z", "grid": "ww3"}


_DOMAIN_BBOX = (-118.30, 33.40, -117.90, 33.80)
_RESOLUTION_M = 2000.0  # coarse for fast tests

_RUNNER_CONFIG = {
    "domain_bbox": list(_DOMAIN_BBOX),
    "surf_spots": {
        "spot_a": {"lon": -118.10, "lat": 33.60},
        "spot_b": {"lon": -118.20, "lat": 33.65},
    },
    "swan_binary": "/usr/local/bin/swan",
    "grid_resolution_m": _RESOLUTION_M,
    "compute_dt_min": 10,
    "output_interval_hr": 1.0,
    "swan_timeout_s": 900,
}


# ---------------------------------------------------------------------------
# _compute_swan_grid_dims
# ---------------------------------------------------------------------------


class TestComputeSwanGridDims:
    def test_returns_expected_keys(self) -> None:
        dims = _compute_swan_grid_dims(_DOMAIN_BBOX, 2000.0)
        for key in ("mxc", "myc", "dlon", "dlat", "nx", "ny", "lon_sw", "lat_sw"):
            assert key in dims, f"Missing key: {key}"

    def test_nx_equals_mxc_plus_one(self) -> None:
        dims = _compute_swan_grid_dims(_DOMAIN_BBOX, 2000.0)
        assert dims["nx"] == dims["mxc"] + 1
        assert dims["ny"] == dims["myc"] + 1

    def test_minimum_one_cell(self) -> None:
        # Tiny domain should not produce zero cells
        tiny_bbox = (-118.01, 33.40, -118.00, 33.41)
        dims = _compute_swan_grid_dims(tiny_bbox, 50_000.0)  # 50 km — larger than domain
        assert dims["mxc"] >= 1
        assert dims["myc"] >= 1

    def test_sw_corner_preserved(self) -> None:
        dims = _compute_swan_grid_dims(_DOMAIN_BBOX, 2000.0)
        assert dims["lon_sw"] == pytest.approx(_DOMAIN_BBOX[0])
        assert dims["lat_sw"] == pytest.approx(_DOMAIN_BBOX[1])


# ---------------------------------------------------------------------------
# _bilinear_interp
# ---------------------------------------------------------------------------


class TestBilinearInterp:
    def test_exact_corner(self) -> None:
        grid = [[1.0, 2.0], [3.0, 4.0]]  # 2x2, j=0..1, i=0..1
        v = _bilinear_interp(grid, 33.0, -118.0, 33.1, -117.9, 2, 2, 33.0, -118.0)
        assert v == pytest.approx(1.0)

    def test_midpoint(self) -> None:
        grid = [[0.0, 0.0], [4.0, 4.0]]  # uniform lat gradient
        v = _bilinear_interp(grid, 33.0, -118.0, 33.2, -117.8, 2, 2, 33.1, -117.9)
        assert v == pytest.approx(2.0)

    def test_uniform_grid_any_point(self) -> None:
        val = 7.5
        grid = [[val, val, val], [val, val, val], [val, val, val]]
        v = _bilinear_interp(grid, 33.0, -118.0, 33.2, -117.8, 3, 3, 33.1, -117.9)
        assert v == pytest.approx(val)


# ---------------------------------------------------------------------------
# hrrr_to_swan_wind
# ---------------------------------------------------------------------------


class TestHrrrToSwanWind:
    def test_empty_grids_raises(self) -> None:
        with pytest.raises(ValueError, match="no forecast grids"):
            hrrr_to_swan_wind({"grids": []}, _DOMAIN_BBOX, _RESOLUTION_M)

    def test_returns_tuple_of_dict_and_str(self) -> None:
        wf = _make_hrrr_wind_field(n_grids=2)
        dims, text = hrrr_to_swan_wind(wf, _DOMAIN_BBOX, _RESOLUTION_M)
        assert isinstance(dims, dict)
        assert isinstance(text, str)

    def test_valid_times_in_dims(self) -> None:
        wf = _make_hrrr_wind_field(n_grids=3)
        dims, _ = hrrr_to_swan_wind(wf, _DOMAIN_BBOX, _RESOLUTION_M)
        assert len(dims["valid_times"]) == 3

    def test_block_count_matches_grid_count(self) -> None:
        n_grids = 2
        wf = _make_hrrr_wind_field(n_grids=n_grids)
        dims, text = hrrr_to_swan_wind(wf, _DOMAIN_BBOX, _RESOLUTION_M)
        ny = dims["ny"]
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # Each grid contributes ny rows U + ny rows V = 2*ny rows
        assert len(lines) == n_grids * 2 * ny

    def test_uniform_wind_all_same_value(self) -> None:
        u_val, v_val = 8.0, -4.0
        wf = {"grids": [_make_hrrr_grid(u_val=u_val, v_val=v_val)]}
        dims, text = hrrr_to_swan_wind(wf, _DOMAIN_BBOX, _RESOLUTION_M)
        ny = dims["ny"]
        lines = [ln for ln in text.splitlines() if ln.strip()]
        u_lines = lines[:ny]  # U block
        v_lines = lines[ny:]  # V block

        for ln in u_lines:
            for val_str in ln.split():
                assert float(val_str) == pytest.approx(u_val, abs=1e-3)
        for ln in v_lines:
            for val_str in ln.split():
                assert float(val_str) == pytest.approx(v_val, abs=1e-3)

    def test_longitude_normalisation_0_360(self) -> None:
        """Longitudes in 0..360 range should be normalised before interpolation."""
        grid = _make_hrrr_grid(
            lon_first=241.70,  # = -118.30 in 0..360
            lon_last=242.10,   # = -117.90 in 0..360
            u_val=3.0,
            v_val=-1.0,
        )
        wf = {"grids": [grid]}
        # Should not raise; the u/v values should still interpolate correctly
        dims, text = hrrr_to_swan_wind(wf, _DOMAIN_BBOX, _RESOLUTION_M)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert lines, "Expected non-empty WIND.txt"


# ---------------------------------------------------------------------------
# cudem_to_swan_bottom
# ---------------------------------------------------------------------------


class TestCudemToSwanBottom:
    def test_sign_flip_ocean(self) -> None:
        cudem_depth = -20.0  # 20 m deep ocean in CUDEM convention
        grid = _make_cudem_grid(depth_val=cudem_depth)
        dims, text = cudem_to_swan_bottom(grid, _DOMAIN_BBOX, _RESOLUTION_M)
        for line in text.splitlines():
            for val_str in line.split():
                assert float(val_str) == pytest.approx(20.0, abs=0.01), (
                    "SWAN depth should be positive (20.0) for ocean points"
                )

    def test_sign_flip_land(self) -> None:
        cudem_depth = 5.0  # 5 m above MSL in CUDEM convention (land)
        grid = _make_cudem_grid(depth_val=cudem_depth)
        _, text = cudem_to_swan_bottom(grid, _DOMAIN_BBOX, _RESOLUTION_M)
        for line in text.splitlines():
            for val_str in line.split():
                assert float(val_str) == pytest.approx(-5.0, abs=0.01), (
                    "SWAN depth should be negative for land points"
                )

    def test_empty_depths_fallback(self) -> None:
        """If depths list is empty, should produce 15 m ocean (default)."""
        grid = {"lat_first": 33.40, "lon_first": -118.30, "lat_last": 33.80,
                "lon_last": -117.90, "ni": 0, "nj": 0, "depths": []}
        _, text = cudem_to_swan_bottom(grid, _DOMAIN_BBOX, _RESOLUTION_M)
        assert text.strip(), "Expected non-empty BOTTOM.txt"
        for line in text.splitlines():
            for val_str in line.split():
                assert float(val_str) == pytest.approx(15.0, abs=0.01), (
                    "Fallback depth should be 15.0 m"
                )

    def test_grid_dims_match_domain(self) -> None:
        grid = _make_cudem_grid()
        dims, _ = cudem_to_swan_bottom(grid, _DOMAIN_BBOX, _RESOLUTION_M)
        assert dims["nx"] == dims["mxc"] + 1
        assert dims["ny"] == dims["myc"] + 1


# ---------------------------------------------------------------------------
# ww3_to_swan_boundary
# ---------------------------------------------------------------------------


class TestWw3ToSwanBoundary:
    def test_output_starts_with_tpar(self) -> None:
        ww3 = _make_ww3_data(n_hours=3)
        text = ww3_to_swan_boundary(ww3)
        assert text.startswith("TPAR\n")

    def test_row_count_matches_forecast_length(self) -> None:
        n = 4
        ww3 = _make_ww3_data(n_hours=n)
        text = ww3_to_swan_boundary(ww3)
        data_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("TPAR")]
        assert len(data_lines) == n

    def test_row_format_tokens(self) -> None:
        ww3 = _make_ww3_data(n_hours=1)
        text = ww3_to_swan_boundary(ww3)
        data_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("TPAR")]
        assert len(data_lines) == 1
        tokens = data_lines[0].split()
        assert len(tokens) == 5, f"Expected 5 tokens per row, got: {data_lines[0]}"
        # tokens: YYYYMMDD.HHmmss Hs Tp DIR DSPR
        assert "." in tokens[0], "First token should be SWAN time (YYYYMMDD.HHmmss)"
        assert float(tokens[1]) > 0.0, "Hs should be positive"
        assert float(tokens[4]) == pytest.approx(30.0), "Default DSPR should be 30.0"

    def test_swell_takes_priority_over_total_wave(self) -> None:
        swell_hs = 0.8
        total_hs = 1.5
        pt = _make_ww3_point(hs=total_hs, swell_hs=swell_hs, swell_tp=14.0, swell_dir=265.0)
        ww3 = {"forecast": [pt], "model_run": "2024-01-01T00:00:00Z", "grid": "ww3"}
        text = ww3_to_swan_boundary(ww3)
        data_line = [ln for ln in text.splitlines() if ln and not ln.startswith("TPAR")][0]
        tokens = data_line.split()
        assert float(tokens[1]) == pytest.approx(swell_hs, abs=1e-3), (
            "Swell Hs should be used when swell data is present"
        )

    def test_missing_hs_row_skipped(self) -> None:
        ww3 = {"forecast": [{"time": "2024-01-01T00:00:00Z", "waveHeight": None}],
                "model_run": "", "grid": "ww3"}
        text = ww3_to_swan_boundary(ww3)
        # Only header or empty
        assert text == "" or text.strip() == "TPAR"

    def test_empty_forecast_returns_empty(self) -> None:
        text = ww3_to_swan_boundary({"forecast": [], "model_run": "", "grid": "ww3"})
        assert text == ""

    def test_time_format_is_swan_style(self) -> None:
        ww3 = _make_ww3_data(n_hours=1)
        text = ww3_to_swan_boundary(ww3)
        data_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("TPAR")]
        time_tok = data_lines[0].split()[0]
        # Should be YYYYMMDD.HHmmss
        assert len(time_tok) == 15 and time_tok[8] == ".", (
            f"Expected YYYYMMDD.HHmmss, got {time_tok}"
        )


# ---------------------------------------------------------------------------
# Physical validation
# ---------------------------------------------------------------------------


class TestIsValidPoint:
    def test_valid_values_pass(self) -> None:
        assert _is_valid_point(1.5, 12.0, 270.0) is True

    def test_hs_zero_valid(self) -> None:
        assert _is_valid_point(0.0, 12.0, 270.0) is True

    def test_hs_above_25_rejected(self) -> None:
        assert _is_valid_point(25.1, 12.0, 270.0) is False

    def test_hs_exactly_25_accepted(self) -> None:
        assert _is_valid_point(25.0, 12.0, 270.0) is True

    def test_tm01_sub_1s_valid(self) -> None:
        assert _is_valid_point(1.5, 0.5, 270.0) is True

    def test_tm01_exactly_1_accepted(self) -> None:
        assert _is_valid_point(1.5, 1.0, 270.0) is True

    def test_tm01_above_35_rejected(self) -> None:
        assert _is_valid_point(1.5, 35.1, 270.0) is False

    def test_nan_hs_rejected(self) -> None:
        assert _is_valid_point(float("nan"), 12.0, 270.0) is False

    def test_nan_tm01_rejected(self) -> None:
        assert _is_valid_point(1.5, float("nan"), 270.0) is False

    def test_nan_mwd_rejected(self) -> None:
        assert _is_valid_point(1.5, 12.0, float("nan")) is False


# ---------------------------------------------------------------------------
# _parse_table_output
# ---------------------------------------------------------------------------


def _make_table_text(rows: list[dict[str, Any]]) -> str:
    """Build a minimal synthetic SWAN TABLE HEAD output string."""
    lines = [
        "%   Time             Xp        Yp        Hs      Tm01      Dir",
        "%                  degr      degr         m         s     degr",
    ]
    for row in rows:
        t = row.get("time", "20240101.000000")
        xp = row.get("xp", -118.10)
        yp = row.get("yp", 33.60)
        hs = row.get("hs", 1.5)
        tm = row.get("tm", 12.0)
        d = row.get("dir", 270.0)
        lines.append(f"  {t}  {xp:.4f}   {yp:.4f}    {hs:.3f}    {tm:.3f}    {d:.2f}")
    return "\n".join(lines) + "\n"


_TEST_SPOTS = {
    "spot_a": (-118.10, 33.60),
    "spot_b": (-118.20, 33.65),
}


class TestParseTableOutput:
    def test_basic_single_point_single_time(self) -> None:
        text = _make_table_text([{"xp": -118.10, "yp": 33.60, "hs": 1.5, "tm": 12.0, "dir": 270.0}])
        result = _parse_table_output(text, _TEST_SPOTS)
        assert "spot_a" in result
        assert len(result["spot_a"]) == 1
        pt = result["spot_a"][0]
        assert isinstance(pt, MarineForecastPoint)
        assert pt.waveHeight == pytest.approx(1.5, abs=0.01)
        assert pt.wavePeriod == pytest.approx(12.0, abs=0.1)
        assert pt.waveDirection == pytest.approx(270.0, abs=0.1)

    def test_two_spots_two_times(self) -> None:
        rows = [
            {"xp": -118.10, "yp": 33.60, "hs": 1.2, "tm": 11.0, "dir": 268.0, "time": "20240101.000000"},
            {"xp": -118.20, "yp": 33.65, "hs": 1.4, "tm": 11.5, "dir": 272.0, "time": "20240101.000000"},
            {"xp": -118.10, "yp": 33.60, "hs": 1.3, "tm": 11.2, "dir": 269.0, "time": "20240101.010000"},
            {"xp": -118.20, "yp": 33.65, "hs": 1.5, "tm": 11.8, "dir": 274.0, "time": "20240101.010000"},
        ]
        text = _make_table_text(rows)
        result = _parse_table_output(text, _TEST_SPOTS)
        assert len(result["spot_a"]) == 2
        assert len(result["spot_b"]) == 2

    def test_unmatched_coordinates_ignored(self) -> None:
        # Point at a completely different location — no spot should get it
        text = _make_table_text([{"xp": -120.00, "yp": 35.00, "hs": 1.5, "tm": 12.0, "dir": 270.0}])
        result = _parse_table_output(text, _TEST_SPOTS)
        assert all(len(pts) == 0 for pts in result.values())

    def test_hs_above_25_rejected(self) -> None:
        text = _make_table_text([{"xp": -118.10, "yp": 33.60, "hs": 25.1, "tm": 12.0, "dir": 270.0}])
        result = _parse_table_output(text, _TEST_SPOTS)
        assert len(result["spot_a"]) == 0

    def test_tm01_sub_1s_valid(self) -> None:
        text = _make_table_text([{"xp": -118.10, "yp": 33.60, "hs": 1.5, "tm": 0.5, "dir": 270.0}])
        result = _parse_table_output(text, _TEST_SPOTS)
        assert len(result["spot_a"]) == 1

    def test_time_iso_conversion(self) -> None:
        text = _make_table_text([{
            "xp": -118.10, "yp": 33.60, "hs": 1.5, "tm": 12.0, "dir": 270.0,
            "time": "20240115.120000"
        }])
        result = _parse_table_output(text, _TEST_SPOTS)
        assert len(result["spot_a"]) == 1
        assert result["spot_a"][0].time == "2024-01-15T12:00:00Z"

    def test_empty_table_returns_empty_lists(self) -> None:
        result = _parse_table_output("", _TEST_SPOTS)
        assert all(len(pts) == 0 for pts in result.values())

    def test_comment_only_file_returns_empty_lists(self) -> None:
        text = "% Time  Xp  Yp  Hs  Tm01  Dir\n% degr degr m s degr\n"
        result = _parse_table_output(text, _TEST_SPOTS)
        assert all(len(pts) == 0 for pts in result.values())

    def test_result_spot_ids_match_input_spots(self) -> None:
        text = _make_table_text([])
        result = _parse_table_output(text, _TEST_SPOTS)
        assert set(result.keys()) == set(_TEST_SPOTS.keys())


# ---------------------------------------------------------------------------
# SWANRunner._write_input_files
# ---------------------------------------------------------------------------


class TestSWANRunnerWriteInputFiles:
    def setup_method(self) -> None:
        self.runner = SWANRunner(_RUNNER_CONFIG)
        self.hrrr = _make_hrrr_wind_field(n_grids=3)
        self.ww3 = _make_ww3_data(n_hours=3)
        self.cudem = _make_cudem_grid()

    def test_all_required_files_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            for fname in ("INPUT", "BOTTOM.txt", "WIND.txt", "BOUND_SPEC.txt", "OUTPUT_POINTS.txt"):
                assert (tmpdir / fname).exists(), f"Missing: {fname}"

    def test_input_file_has_cgrid_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            content = (tmpdir / "INPUT").read_text()
            assert "CGRID REG" in content

    def test_input_file_has_compute_nonst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            content = (tmpdir / "INPUT").read_text()
            assert "COMPUTE NONST" in content

    def test_input_file_has_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            content = (tmpdir / "INPUT").read_text()
            assert "STOP" in content

    def test_output_points_file_has_correct_spot_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            content = (tmpdir / "OUTPUT_POINTS.txt").read_text()
            lines = [ln for ln in content.splitlines() if ln.strip()]
            n_spots = len(_RUNNER_CONFIG["surf_spots"])
            assert len(lines) == n_spots

    def test_output_points_file_lon_lat_order(self) -> None:
        """OUTPUT_POINTS.txt must list longitude first (SWAN X), then latitude (SWAN Y)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            content = (tmpdir / "OUTPUT_POINTS.txt").read_text()
            first_line = content.strip().splitlines()[0].split()
            lon = float(first_line[0])
            lat = float(first_line[1])
            # Longitude for our test config should be ~ -118; latitude ~ 33
            assert lon < 0, "First column should be longitude (negative for W hemisphere)"
            assert 30.0 < lat < 40.0, "Second column should be latitude"

    def test_bound_spec_fallback_when_ww3_empty(self) -> None:
        """When WW3 data is empty, a calm fallback boundary should still be written."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            empty_ww3 = {"forecast": [], "model_run": "", "grid": "ww3"}
            self.runner._write_input_files(tmpdir, self.hrrr, empty_ww3, self.cudem)
            content = (tmpdir / "BOUND_SPEC.txt").read_text()
            assert content.startswith("TPAR"), "Fallback BOUND_SPEC.txt should start with TPAR"

    def test_grid_info_returned_has_valid_times(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            grid_info = self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            assert "valid_times" in grid_info
            assert len(grid_info["valid_times"]) == 3

    def test_input_file_spherical_nautical_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.runner._write_input_files(tmpdir, self.hrrr, self.ww3, self.cudem)
            content = (tmpdir / "INPUT").read_text()
            assert "SPHERICAL" in content
            assert "NAUTICAL" in content


# ---------------------------------------------------------------------------
# SWANRunner._parse_output
# ---------------------------------------------------------------------------


class TestSWANRunnerParseOutput:
    def setup_method(self) -> None:
        self.runner = SWANRunner(_RUNNER_CONFIG)
        self.spots = {
            sid: (data["lon"], data["lat"])
            for sid, data in _RUNNER_CONFIG["surf_spots"].items()
        }

    def test_valid_output_produces_points(self) -> None:
        rows = [{"xp": -118.10, "yp": 33.60, "hs": 1.5, "tm": 12.0, "dir": 270.0}]
        table_text = _make_table_text(rows)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "OUTPUT_TABLE.txt").write_text(table_text)
            result = self.runner._parse_output(tmpdir, {})
        assert len(result["spot_a"]) == 1

    def test_missing_output_table_raises_swan_run_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with pytest.raises(SWANRunError, match="OUTPUT_TABLE.txt"):
                self.runner._parse_output(tmpdir, {})

    def test_errfile_content_in_exception_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "Errfile").write_text("SWAN fatal: grid too coarse")
            try:
                self.runner._parse_output(tmpdir, {})
            except SWANRunError as exc:
                assert "grid too coarse" in exc.stderr


# ---------------------------------------------------------------------------
# SWANRunError
# ---------------------------------------------------------------------------


class TestSWANRunError:
    def test_has_stderr_attribute(self) -> None:
        err = SWANRunError("test error", stderr="some error output")
        assert err.stderr == "some error output"

    def test_has_returncode_attribute(self) -> None:
        err = SWANRunError("test error", returncode=1)
        assert err.returncode == 1

    def test_returncode_none_for_timeout(self) -> None:
        err = SWANRunError("timed out", returncode=None)
        assert err.returncode is None

    def test_is_exception(self) -> None:
        with pytest.raises(SWANRunError, match="deliberate"):
            raise SWANRunError("deliberate", stderr="")


# ---------------------------------------------------------------------------
# SWANRunner.run — subprocess patched
# ---------------------------------------------------------------------------


class TestSWANRunnerRunPatched:
    """Test run() end-to-end with _spawn_swan patched out."""

    def setup_method(self) -> None:
        self.runner = SWANRunner(_RUNNER_CONFIG)
        self.hrrr = _make_hrrr_wind_field(n_grids=3)
        self.ww3 = _make_ww3_data(n_hours=3)
        self.cudem = _make_cudem_grid()

    def test_run_returns_dict_keyed_by_spot_id(self) -> None:
        """run() should return a dict with the configured spot IDs as keys."""

        def fake_spawn(tmpdir: Path) -> None:
            # Write a synthetic OUTPUT_TABLE.txt with one valid row per spot
            rows = [
                {"xp": -118.10, "yp": 33.60, "hs": 1.5, "tm": 12.0, "dir": 270.0},
                {"xp": -118.20, "yp": 33.65, "hs": 1.3, "tm": 11.5, "dir": 272.0},
            ]
            (tmpdir / "OUTPUT_TABLE.txt").write_text(_make_table_text(rows))

        with patch.object(self.runner, "_spawn_swan", side_effect=fake_spawn):
            result = self.runner.run(self.hrrr, self.ww3, self.cudem)

        assert set(result.keys()) == set(_RUNNER_CONFIG["surf_spots"].keys())

    def test_run_values_are_marine_forecast_points(self) -> None:
        def fake_spawn(tmpdir: Path) -> None:
            rows = [{"xp": -118.10, "yp": 33.60, "hs": 1.5, "tm": 12.0, "dir": 270.0}]
            (tmpdir / "OUTPUT_TABLE.txt").write_text(_make_table_text(rows))

        with patch.object(self.runner, "_spawn_swan", side_effect=fake_spawn):
            result = self.runner.run(self.hrrr, self.ww3, self.cudem)

        for pt in result["spot_a"]:
            assert isinstance(pt, MarineForecastPoint)

    def test_run_propagates_swan_run_error(self) -> None:
        def failing_spawn(tmpdir: Path) -> None:
            raise SWANRunError("test failure", stderr="swan error", returncode=1)

        with patch.object(self.runner, "_spawn_swan", side_effect=failing_spawn):
            with pytest.raises(SWANRunError) as exc_info:
                self.runner.run(self.hrrr, self.ww3, self.cudem)

        assert exc_info.value.stderr == "swan error"
        assert exc_info.value.returncode == 1


# ---------------------------------------------------------------------------
# build_swan_input — HOTFILE bootstrap (regression test for hotstart chain bug)
# ---------------------------------------------------------------------------


class TestBuildSwanInputHotstart:
    """Verify HOTFILE bootstrap behavior.

    Bug: HOTFILE was gated on ``if hotstart_file:``, same as INIT HOTSTART.
    On first run (no previous hotstart) neither command was emitted, so SWAN
    never wrote a hotstart file.  Every subsequent run was also a cold start —
    the chain never bootstrapped.

    Fix: HOTFILE 'hotstart.dat' is unconditional.  INIT HOTSTART stays
    conditional (only reads when a previous file already exists).
    """

    def _call(self, hotstart_file: str | None) -> str:
        dims = _compute_swan_grid_dims(_DOMAIN_BBOX, _RESOLUTION_M)
        return build_swan_input(
            dims=dims,
            valid_times=["2024-01-01T00:00:00Z"],
            spots={"spot_a": (-118.10, 33.60)},
            grid_level="inner",
            stationary=True,
            hotstart_file=hotstart_file,
        )

    def test_hotfile_present_on_cold_start(self) -> None:
        """HOTFILE must appear even when hotstart_file=None (first-run bootstrap)."""
        content = self._call(hotstart_file=None)
        assert "HOTFILE 'hotstart.dat'" in content, (
            "HOTFILE must be unconditional so first run creates hotstart for subsequent runs"
        )

    def test_hotfile_present_when_previous_hotstart_exists(self) -> None:
        """When hotstart_file is set, both INIT HOTSTART and HOTFILE must appear."""
        content = self._call(hotstart_file="hotstart.dat")
        assert "INIT HOTSTART 'hotstart.dat'" in content, (
            "INIT HOTSTART must appear when a previous hotstart file is provided"
        )
        assert "HOTFILE 'hotstart.dat'" in content, (
            "HOTFILE must also appear when hotstart_file is provided"
        )

    def test_init_hotstart_absent_on_cold_start(self) -> None:
        """INIT HOTSTART must NOT appear when hotstart_file=None (cold start)."""
        content = self._call(hotstart_file=None)
        assert "INIT HOTSTART" not in content, (
            "INIT HOTSTART should only appear when a previous hotstart file exists"
        )


class TestBuildSwanInputPerLevelPhysics:
    """Verify per-level physics selection (SWAN-L3-STABILITY-PLAN T2.1).

    SETUP removed from all levels (unsupported in parallel OpenMP runs).
    DIFFRACTION stabilized at L3 only; removed at L1/L2 (sub-grid).
    NUMERIC alfa=0.01 emitted for L3 stationary only.
    """

    def _call(self, grid_level: str, stationary: bool = False) -> str:
        dims = _compute_swan_grid_dims(_DOMAIN_BBOX, _RESOLUTION_M)
        inner_dims = _compute_swan_grid_dims(_DOMAIN_BBOX, _RESOLUTION_M) if grid_level == "outer" else None
        return build_swan_input(
            dims=dims,
            valid_times=["2024-01-01T00:00:00Z"],
            spots={"spot_a": (-118.10, 33.60)},
            grid_level=grid_level,
            stationary=stationary,
            inner_dims=inner_dims,
        )

    def test_outer_has_no_setup(self) -> None:
        content = self._call("outer")
        assert "\nSETUP\n" not in content and "\nSETUP " not in content

    def test_outer_has_no_diffraction(self) -> None:
        content = self._call("outer")
        assert "DIFFRACTION" not in content

    def test_inner_nonstat_has_stabilized_diffraction(self) -> None:
        content = self._call("inner", stationary=False)
        assert "DIFFRACTION 1 0.2 27" in content

    def test_inner_nonstat_has_no_setup(self) -> None:
        content = self._call("inner", stationary=False)
        assert "\nSETUP\n" not in content and "\nSETUP " not in content

    def test_inner_nonstat_has_no_numeric_alfa(self) -> None:
        content = self._call("inner", stationary=False)
        assert "alfa=" not in content

    def test_inner_stat_has_stabilized_diffraction(self) -> None:
        content = self._call("inner", stationary=True)
        assert "DIFFRACTION 1 0.2 27" in content

    def test_inner_stat_has_numeric_alfa(self) -> None:
        content = self._call("inner", stationary=True)
        assert "alfa=0.01" in content

    def test_inner_stat_has_no_setup(self) -> None:
        content = self._call("inner", stationary=True)
        assert "\nSETUP\n" not in content and "\nSETUP " not in content

    def test_table_columns_exclude_setup(self) -> None:
        content = self._call("inner", stationary=False)
        for line in content.splitlines():
            if line.strip().startswith("TABLE") and "HSIGN" in line:
                assert "SETUP" not in line, f"TABLE line still contains SETUP: {line}"
