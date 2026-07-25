"""Tests for the T4A.3 apply-time marine CUDEM/grid-sizing/profile chain
(`endpoints.setup._run_marine_apply_chain`).

Exercises the full L1 -> COARSE -> 30m contour -> L2 -> MEDIUM -> 15m
contour -> L3 (T4A.11 trigger + viability) -> FINE -> native profile ->
PCHIP -> cache pipeline end to end, with `download_bathymetry_for_level`
mocked (no real network access) to return a synthetic bathymetry grid.

No live HTTP calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import configobj
import pytest

from weewx_clearskies_api.endpoints import setup as setup_endpoint


def _synthetic_grid(
    *,
    lat_first: float = 33.60,
    lon_first: float = -117.97,
    lat_last: float = 33.70,
    lon_last: float = -117.95,
    n: int = 151,
    max_depth_m: float = 35.0,
    land_elevation_m: float = 5.0,
    datum: str = "NAVD88",
) -> dict:
    """West = deep ocean, east = actual land (elevation > 0), crossing
    through a real depth=0 shoreline INSIDE the grid's queryable interior
    (not at the last column, which bilinear interpolation can never sample
    -- ``_bilinear_grid_depth`` requires ``fi < ni - 1``). Matches CUDEM's
    sign convention (negative = underwater)."""
    row = [
        -max_depth_m + i * (max_depth_m + land_elevation_m) / (n - 1)
        for i in range(n)
    ]
    depths = [row for _ in range(n)]
    return {
        "lat_first": lat_first, "lon_first": lon_first,
        "lat_last": lat_last, "lon_last": lon_last,
        "ni": n, "nj": n, "depths": depths,
        "vertical_datum": datum,
        "source": "test_fixture",
    }


def _write_api_conf(config_dir: Path, *, topographic_feature: str = "straight_beach") -> None:
    """A minimal api.conf with one surf spot. Segment runs N->S (start lat
    > end lat) so the computed offshore bearing (perpendicular, +90°
    clockwise from the segment's forward bearing) is ~270° (west) --
    matching _synthetic_grid()'s west=deep convention."""
    text = f"""\
[marine]
  [[locations]]
    [[[hb_pier]]]
      name = HB Pier
      lat = 33.6553
      lon = -117.9580
      activities = surf
      [[[[surf]]]]
        segment_start_lat = 33.6560
        segment_start_lon = -117.9580
        segment_end_lat = 33.6546
        segment_end_lon = -117.9580
        transect_spacing_m = 10.0
        bottom_type = sand
        topographic_feature = {topographic_feature}
        l3_enabled = auto
        max_hs_m = 4.0
"""
    (config_dir / "api.conf").write_text(text, encoding="utf-8")
    # Sanity-check it parses before the test relies on it.
    configobj.ConfigObj(str(config_dir / "api.conf"), interpolation=False)


@pytest.fixture
def _cache_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    profile_dir = tmp_path / "spot_profiles"
    sizing_path = tmp_path / "swan_grid_sizing.json"
    monkeypatch.setattr(setup_endpoint, "_MARINE_PROFILE_CACHE_DIR", profile_dir)
    monkeypatch.setattr(setup_endpoint, "_SWAN_GRID_SIZING_CACHE_PATH", sizing_path)
    return profile_dir, sizing_path


class TestMarineApplyChainOpenBeach:
    def test_straight_beach_no_structures_no_l3_but_profile_written(
        self, tmp_path: Path, _cache_dirs: tuple[Path, Path]
    ) -> None:
        profile_dir, sizing_path = _cache_dirs
        _write_api_conf(tmp_path, topographic_feature="straight_beach")

        with patch(
            "weewx_clearskies_api.providers.nearshore.swan.download_bathymetry_for_level",
            return_value=_synthetic_grid(),
        ):
            setup_endpoint._run_marine_apply_chain(tmp_path)

        assert sizing_path.exists()
        sizing = json.loads(sizing_path.read_text(encoding="utf-8"))
        assert sizing["level3_clusters"][0]["grid"] is None  # no trigger

        profile_path = profile_dir / "hb_pier.json"
        assert profile_path.exists()
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        assert profile["vertical_datum"] == "NAVD88"  # from the mocked grid, not hardcoded
        assert profile["structure_zone_depth"] == 0.0  # no structures
        assert len(profile["profile"]) > 1
        assert profile["contour_15m_distance_m"] is not None
        assert profile["contour_15m_distance_m"] > 0


class TestMarineApplyChainPointBreak:
    def test_point_break_no_structures_enables_l3_and_downloads_fine(
        self, tmp_path: Path, _cache_dirs: tuple[Path, Path]
    ) -> None:
        profile_dir, sizing_path = _cache_dirs
        _write_api_conf(tmp_path, topographic_feature="point_break")

        call_levels: list[int] = []

        def _fake_download(domain, level):  # noqa: ANN001
            call_levels.append(level)
            return _synthetic_grid()

        with patch(
            "weewx_clearskies_api.providers.nearshore.swan.download_bathymetry_for_level",
            side_effect=_fake_download,
        ):
            setup_endpoint._run_marine_apply_chain(tmp_path)

        assert 3 in call_levels, "L3 (FINE) download must be attempted for a point_break trigger"

        sizing = json.loads(sizing_path.read_text(encoding="utf-8"))
        assert sizing["level3_clusters"][0]["grid"] is not None

        profile = json.loads((profile_dir / "hb_pier.json").read_text(encoding="utf-8"))
        assert profile["vertical_datum"] == "NAVD88"


class TestMarineApplyChainMissingCoarseDownload:
    def test_coarse_download_failure_aborts_with_no_cache_written(
        self, tmp_path: Path, _cache_dirs: tuple[Path, Path]
    ) -> None:
        profile_dir, sizing_path = _cache_dirs
        _write_api_conf(tmp_path)

        with patch(
            "weewx_clearskies_api.providers.nearshore.swan.download_bathymetry_for_level",
            return_value={},  # empty dict == download failure, per its own contract
        ):
            setup_endpoint._run_marine_apply_chain(tmp_path)

        assert not sizing_path.exists()
        assert not (profile_dir / "hb_pier.json").exists()


class TestMarineApplyChainNoMarineConfig:
    def test_missing_api_conf_does_not_raise(
        self, tmp_path: Path, _cache_dirs: tuple[Path, Path]
    ) -> None:
        # No api.conf written at all.
        setup_endpoint._run_marine_apply_chain(tmp_path)  # must not raise

    def test_no_surf_spots_is_a_silent_noop(
        self, tmp_path: Path, _cache_dirs: tuple[Path, Path]
    ) -> None:
        (tmp_path / "api.conf").write_text("[marine]\n", encoding="utf-8")
        setup_endpoint._run_marine_apply_chain(tmp_path)  # must not raise
