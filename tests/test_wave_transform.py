"""Unit tests for the nearshore supplement processor (Phase 3, T3.2).

Module under test: weewx_clearskies_api/enrichment/wave_transform.py

Config inputs (SurfSpotConfig, StructureConfig, BathymetryPoint) are stubbed
locally rather than imported from weewx_clearskies_api.config.marine_config
— this test only needs the attributes wave_transform.py actually reads
(duck typing), and stubbing keeps the test independent of the config
loader's construction/validation path.
"""

from __future__ import annotations

import logging

import pytest

from weewx_clearskies_api.enrichment import wave_transform as wt

# ---------------------------------------------------------------------------
# Local stubs mirroring config/marine_config.py's public attributes
# ---------------------------------------------------------------------------


class StubBathymetryPoint:
    def __init__(self, distance_m: float, depth_m: float) -> None:
        self.distance_m = distance_m
        self.depth_m = depth_m


class StubStructure:
    def __init__(
        self,
        type: str,  # noqa: A002
        material: str,
        length_m: float,
        distance_m: float,
        bearing_degrees: float = 0.0,
    ) -> None:
        self.type = type
        self.material = material
        self.length_m = length_m
        self.distance_m = distance_m
        self.bearing_degrees = bearing_degrees


class StubSurfSpotConfig:
    def __init__(
        self,
        beach_slope: float | None = None,
        structures: list | None = None,
        topographic_feature: str = "straight_beach",
        bathymetric_profile: list | None = None,
        directional_exposure: dict | None = None,
        beach_facing_degrees: float = 0.0,
        bottom_type: str = "sand",
    ) -> None:
        self.beach_slope = beach_slope
        self.structures = structures if structures is not None else []
        self.topographic_feature = topographic_feature
        self.bathymetric_profile = bathymetric_profile
        self.directional_exposure = directional_exposure or {}
        self.beach_facing_degrees = beach_facing_degrees
        self.bottom_type = bottom_type


# ---------------------------------------------------------------------------
# Supplement 1 — Iribarren number / breaker gamma
# ---------------------------------------------------------------------------


def test_iribarren_sand_slope():
    xi = wt.compute_iribarren(slope=0.02, wave_height=2.0, wave_period=10)
    assert xi > 0
    assert xi < 1.0  # gentle sand slope -> small Iribarren number


def test_breaker_gamma_sand():
    xi = wt.compute_iribarren(slope=0.02, wave_height=2.0, wave_period=10)
    gamma = wt.compute_breaker_gamma(xi)
    assert wt.GAMMA_MIN <= gamma <= wt.GAMMA_MAX
    assert 0.7 <= gamma <= 0.9


def test_breaker_gamma_reef():
    xi_reef = wt.compute_iribarren(slope=0.1, wave_height=2.0, wave_period=10)
    xi_sand = wt.compute_iribarren(slope=0.02, wave_height=2.0, wave_period=10)
    gamma_reef = wt.compute_breaker_gamma(xi_reef)
    gamma_sand = wt.compute_breaker_gamma(xi_sand)
    assert wt.GAMMA_MIN <= gamma_reef <= wt.GAMMA_MAX
    assert gamma_reef > gamma_sand
    assert 1.0 <= gamma_reef <= 1.2


def test_gamma_clamping_low(caplog):
    # Very gentle slope + long period -> small Iribarren -> gamma below GAMMA_MIN.
    xi = wt.compute_iribarren(slope=0.001, wave_height=3.0, wave_period=15)
    with caplog.at_level(logging.WARNING):
        gamma = wt.compute_breaker_gamma(xi)
    assert gamma == wt.GAMMA_MIN
    assert any("clamping" in rec.message for rec in caplog.records)


def test_gamma_clamping_high(caplog):
    # Steep slope + short period/small height -> large Iribarren -> gamma above GAMMA_MAX.
    xi = wt.compute_iribarren(slope=2.0, wave_height=0.2, wave_period=3)
    with caplog.at_level(logging.WARNING):
        gamma = wt.compute_breaker_gamma(xi)
    assert gamma == wt.GAMMA_MAX
    assert any("clamping" in rec.message for rec in caplog.records)


def test_breaker_correction_caps_height():
    corrected, gamma = wt.apply_breaker_correction(
        wave_height=5.0, wave_period=10, beach_slope=0.02, depth_at_break=1.0
    )
    assert corrected < 5.0
    assert corrected == pytest.approx(gamma * 1.0)


def test_breaker_correction_no_change():
    corrected, gamma = wt.apply_breaker_correction(
        wave_height=2.0, wave_period=10, beach_slope=0.02, depth_at_break=10.0
    )
    assert corrected == pytest.approx(2.0)
    assert wt.GAMMA_MIN <= gamma <= wt.GAMMA_MAX


# ---------------------------------------------------------------------------
# Supplement 2 — coastal structure effects
# ---------------------------------------------------------------------------


def test_structure_jetty_within_zone():
    structure = StubStructure(
        type="jetty", material="semi_permeable", length_m=100.0, distance_m=50.0
    )
    new_height, applied = wt.apply_structure_effects(2.0, [structure])
    assert applied is True
    assert new_height == pytest.approx(2.0 * 0.35)
    assert new_height < 2.0


def test_structure_no_structures():
    new_height, applied = wt.apply_structure_effects(2.0, [])
    assert applied is False
    assert new_height == 2.0


def test_structure_outside_influence():
    structure = StubStructure(
        type="jetty", material="semi_permeable", length_m=10.0, distance_m=100_000.0
    )
    new_height, applied = wt.apply_structure_effects(2.0, [structure])
    assert applied is True
    assert new_height == pytest.approx(2.0, abs=0.001)


# ---------------------------------------------------------------------------
# Supplement 3 — bilinear interpolation
# ---------------------------------------------------------------------------


def test_bilinear_at_node():
    grid_data = [[1.0, 2.0], [3.0, 4.0]]
    grid_lats = [0.0, 1.0]
    grid_lons = [0.0, 1.0]
    assert wt.bilinear_interpolate(grid_data, grid_lats, grid_lons, 0.0, 0.0) == pytest.approx(1.0)
    assert wt.bilinear_interpolate(grid_data, grid_lats, grid_lons, 1.0, 1.0) == pytest.approx(4.0)


def test_bilinear_midpoint():
    grid_data = [[5.0, 5.0], [5.0, 5.0]]
    grid_lats = [0.0, 1.0]
    grid_lons = [0.0, 1.0]
    result = wt.bilinear_interpolate(grid_data, grid_lats, grid_lons, 0.5, 0.5)
    assert result == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Supplement 4 — topographic focusing/sheltering
# ---------------------------------------------------------------------------


def test_topographic_headland():
    assert wt.apply_topographic_adjustment(2.0, "headland") == pytest.approx(2.4)


def test_topographic_bay():
    assert wt.apply_topographic_adjustment(2.0, "bay_break") == pytest.approx(1.8)


def test_topographic_straight():
    assert wt.apply_topographic_adjustment(2.0, "straight_beach") == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Full pipeline / apply_supplements
# ---------------------------------------------------------------------------


def test_full_pipeline():
    spot_config = StubSurfSpotConfig(
        beach_slope=0.02,
        structures=[
            StubStructure(type="groin", material="permeable", length_m=50.0, distance_m=30.0)
        ],
        topographic_feature="point_break",
        bathymetric_profile=[StubBathymetryPoint(distance_m=100.0, depth_m=2.0)],
    )
    wave_data = {"wave_height": 2.0, "wave_period": 10.0, "wave_direction": 180.0}

    result = wt.apply_supplements(wave_data, spot_config, spot_lat=34.2, spot_lon=-77.8)

    assert result is not None
    assert 0.0 < result["wave_height"] < 10.0
    assert result["wave_period"] == 10.0
    assert result["wave_direction"] == 180.0
    assert result["structure_applied"] is False  # Supplement 2 removed — SWAN OBSTACLE handles structures (ADR-095)
    assert "breaker_correction" in result["supplements_applied"]
    assert "structure_effects" not in result["supplements_applied"]
    assert "topographic_adjustment" in result["supplements_applied"]


def test_none_input():
    spot_config = StubSurfSpotConfig()
    assert wt.apply_supplements(None, spot_config, 34.2, -77.8) is None
    assert wt.apply_supplements({}, spot_config, 34.2, -77.8) is None


def test_no_slope_skips_breaker():
    spot_config = StubSurfSpotConfig(
        beach_slope=None,
        bathymetric_profile=[StubBathymetryPoint(distance_m=100.0, depth_m=2.0)],
    )
    wave_data = {"wave_height": 2.0, "wave_period": 10.0, "wave_direction": 180.0}

    result = wt.apply_supplements(wave_data, spot_config, spot_lat=34.2, spot_lon=-77.8)

    assert result is not None
    assert result["breaker_gamma"] is None
    assert "breaker_correction" not in result["supplements_applied"]


def test_no_shoaling_code():
    import inspect

    source = inspect.getsource(wt)
    assert "def calculate_shoaling" not in source
    assert "def calculate_refraction" not in source


def test_supplements_applied_list():
    spot_config = StubSurfSpotConfig(
        beach_slope=0.02,
        structures=[
            StubStructure(type="pier", material="permeable", length_m=40.0, distance_m=20.0)
        ],
        topographic_feature="headland",
        bathymetric_profile=[StubBathymetryPoint(distance_m=100.0, depth_m=2.0)],
    )
    wave_data = {
        "wave_height": 2.0,
        "wave_period": 10.0,
        "wave_direction": 180.0,
        "grid_data": [[2.0, 2.0], [2.0, 2.0]],
        "grid_lats": [34.0, 34.5],
        "grid_lons": [-78.0, -77.5],
    }

    result = wt.apply_supplements(wave_data, spot_config, spot_lat=34.2, spot_lon=-77.8)

    assert result is not None
    assert result["supplements_applied"] == [
        "interpolation",
        "breaker_correction",
        "topographic_adjustment",
    ]  # structure_effects removed — SWAN OBSTACLE (ADR-095)
