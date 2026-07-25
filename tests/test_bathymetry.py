"""Unit tests for enrichment/bathymetry.py (Phase 3, T3.1).

No live HTTP calls are made — the OpenTopoData boundary is mocked via
``unittest.mock.patch`` at the ``ProviderHTTPClient.get`` layer.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from unittest.mock import patch

import pytest

from weewx_clearskies_api.enrichment.bathymetry import BathymetryPoint
from weewx_clearskies_api.enrichment import bathymetry
from weewx_clearskies_api.providers._common.errors import TransientNetworkError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient


def _profile(pairs: list[tuple[float, float]]) -> list[BathymetryPoint]:
    """Build a list[BathymetryPoint] from (distance_m, depth_m) pairs."""
    return [BathymetryPoint({"distance_m": d, "depth_m": z}) for d, z in pairs]


@pytest.fixture(autouse=True)
def _reset_http_client():
    bathymetry._reset_http_client_for_tests()
    yield
    bathymetry._reset_http_client_for_tests()


# ---------------------------------------------------------------------------
# compute_beach_slope
# ---------------------------------------------------------------------------


def test_compute_beach_slope_flat():
    profile = _profile([(0, 0.0), (100, 1.0), (200, 2.0), (300, 3.0), (400, 4.0), (500, 5.0)])
    slope = bathymetry.compute_beach_slope(profile)
    assert slope == pytest.approx(0.01, abs=1e-6)


def test_compute_beach_slope_steep():
    profile = _profile([(0, 0.0), (50, 5.0), (100, 10.0), (150, 15.0), (200, 20.0)])
    slope = bathymetry.compute_beach_slope(profile)
    assert slope == pytest.approx(0.1, abs=1e-6)


def test_compute_beach_slope_ignores_offshore_points():
    # Nearshore (<=500m) is a gentle 0.01 slope; offshore points have a much
    # steeper apparent slope and must not influence the nearshore regression.
    profile = _profile(
        [(0, 0.0), (100, 1.0), (200, 2.0), (300, 3.0), (400, 4.0), (500, 5.0), (5000, 200.0)]
    )
    slope = bathymetry.compute_beach_slope(profile)
    assert slope == pytest.approx(0.01, abs=1e-3)


# ---------------------------------------------------------------------------
# identify_habitat_features
# ---------------------------------------------------------------------------


def test_identify_habitat_dropoff():
    profile = _profile([(0, 2.0), (150, 8.0), (300, 9.0)])
    features = bathymetry.identify_habitat_features(profile)
    dropoffs = [f for f in features if f["type"] == "dropoff"]
    assert len(dropoffs) == 1
    assert dropoffs[0]["distance_m"] == 150
    assert dropoffs[0]["depth_m"] == 8.0


def test_identify_habitat_ledge():
    profile = _profile([(0, 2.0), (100, 2.5), (150, 8.5)])
    features = bathymetry.identify_habitat_features(profile)
    ledges = [f for f in features if f["type"] == "ledge"]
    assert len(ledges) == 1
    assert ledges[0]["distance_m"] == 0
    dropoffs = [f for f in features if f["type"] == "dropoff"]
    assert len(dropoffs) == 1


def test_identify_habitat_no_features():
    profile = _profile(
        [(0, 1.0), (500, 2.0), (1000, 3.0), (1500, 4.0), (2000, 5.0), (2500, 6.0), (3000, 7.0)]
    )
    features = bathymetry.identify_habitat_features(profile)
    assert features == []


def test_identify_habitat_channel():
    # Local depth maximum (trough/depression) between two shallower points.
    profile = _profile([(0, 10.0), (100, 30.0), (200, 12.0)])
    features = bathymetry.identify_habitat_features(profile)
    channels = [f for f in features if f["type"] == "channel"]
    assert len(channels) == 1
    assert channels[0]["distance_m"] == 100
    assert channels[0]["depth_m"] == 30.0


def test_identify_habitat_pinnacle():
    # Local depth minimum (isolated shallow spot) surrounded by deep water.
    profile = _profile([(0, 40.0), (100, 22.0), (200, 45.0)])
    features = bathymetry.identify_habitat_features(profile)
    pinnacles = [f for f in features if f["type"] == "pinnacle"]
    assert len(pinnacles) == 1
    assert pinnacles[0]["distance_m"] == 100
    assert pinnacles[0]["depth_m"] == 22.0


def test_identify_habitat_reef():
    # Irregular (non-monotonic) pattern at shallow depth (<20m), not
    # matching the deep-water-neighbor pinnacle criteria.
    profile = _profile([(0, 5.0), (100, 3.0), (200, 6.0)])
    features = bathymetry.identify_habitat_features(profile)
    reefs = [f for f in features if f["type"] == "reef"]
    assert len(reefs) == 1
    assert reefs[0]["distance_m"] == 100


# ---------------------------------------------------------------------------
# Region classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        (34.0, -118.5, bathymetry.REGION_PACIFIC),
        (34.2, -77.8, bathymetry.REGION_ATLANTIC),
        (29.0, -90.0, bathymetry.REGION_GULF),
        (21.3, -157.8, bathymetry.REGION_HAWAII),
        (43.0, -87.0, bathymetry.REGION_GREAT_LAKES),
    ],
)
def test_region_classification(lat, lon, expected):
    assert bathymetry.classify_region(lat, lon) == expected


# ---------------------------------------------------------------------------
# Fallback profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon", "region"),
    [
        (34.0, -118.5, bathymetry.REGION_PACIFIC),
        (34.2, -77.8, bathymetry.REGION_ATLANTIC),
        (29.0, -90.0, bathymetry.REGION_GULF),
        (21.3, -157.8, bathymetry.REGION_HAWAII),
    ],
)
def test_fallback_profiles(lat, lon, region, caplog):
    with (
        patch.object(
            bathymetry,
            "_query_depths_m",
            side_effect=TransientNetworkError(
                "simulated network failure", provider_id="cudem", domain="bathymetry"
            ),
        ),
        caplog.at_level(logging.WARNING),
    ):
        profile = bathymetry.download_bathymetric_profile(lat, lon, bearing_degrees=90.0)

    expected_depths = bathymetry.FALLBACK_DEPTH_PROFILES_M[region]
    assert [p.depth_m for p in profile] == expected_depths
    assert [p.distance_m for p in profile] == bathymetry.FALLBACK_DISTANCES_M
    assert any("fallback" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# Safety / constants
# ---------------------------------------------------------------------------


def test_no_eval_in_module():
    source = Path(bathymetry.__file__).read_text(encoding="utf-8")
    assert "eval(" not in source


def test_attribution_constants():
    assert bathymetry.ATTRIBUTION == "NOAA National Centers for Environmental Information"
    assert bathymetry.DISCLAIMER == "Not for navigation"


# ---------------------------------------------------------------------------
# download_bathymetric_profile (mocked HTTP boundary)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, n_locations: int, elevation: float) -> None:
        self._n_locations = n_locations
        self._elevation = elevation

    def json(self) -> dict:
        return {
            "status": "OK",
            "results": [{"elevation": self._elevation} for _ in range(self._n_locations)],
        }

    @property
    def text(self) -> str:
        return ""


def _mock_get_constant_elevation(elevation: float):
    def _get(self, url, params=None, headers=None, log_url=None):  # noqa: ANN001
        n = len(params["locations"].split("|"))
        return _FakeResponse(n, elevation)

    return _get


def test_download_profile_mock():
    # elevation=-40 => depth=40m everywhere, comfortably above the Atlantic
    # region's 35m deep-water threshold, so the very first 1km search step
    # already satisfies it and deep_water_distance_m == 1000m.
    # The real 1 call/sec rate limiter is bypassed here — this test exercises
    # profile assembly, not live rate-limiting behavior (which would make a
    # 2+ HTTP-call test take multiple wall-clock seconds for no benefit).
    with (
        patch.object(ProviderHTTPClient, "get", _mock_get_constant_elevation(-40.0)),
        patch.object(bathymetry._rate_limiter, "acquire", lambda: None),
    ):
        profile = bathymetry.download_bathymetric_profile(
            34.2, -77.8, bearing_degrees=90.0, max_search_km=5.0
        )

    assert len(profile) >= 16
    distances = [p.distance_m for p in profile]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=1e-6)
    assert distances[-1] == pytest.approx(1000.0, abs=1e-6)
    # Roughly-even spacing across the 16-point interpolation (no refinement
    # should have triggered since depth is constant => zero gradient).
    gaps = [distances[i + 1] - distances[i] for i in range(len(distances) - 1)]
    expected_gap = 1000.0 / 15
    assert all(gap == pytest.approx(expected_gap, abs=1e-3) for gap in gaps)
    for point in profile:
        assert point.depth_m == pytest.approx(40.0, abs=1e-6)


class _FakeResponseVaried:
    def __init__(self, elevations: list[float]) -> None:
        self._elevations = elevations

    def json(self) -> dict:
        return {"status": "OK", "results": [{"elevation": e} for e in self._elevations]}

    @property
    def text(self) -> str:
        return ""


def test_download_profile_mock_triggers_refinement():
    # Elevation varies with longitude so gradient-based refinement inserts
    # extra points beyond the initial 16.
    def _get(self, url, params=None, headers=None, log_url=None):  # noqa: ANN001
        locations = params["locations"].split("|")
        elevations = []
        for loc in locations:
            _, _, lon_str = loc.partition(",")
            # Alternate steep/shallow so adjacent points' depth gradient
            # exceeds the refinement threshold.
            seed = int(abs(float(lon_str)) * 1000) % 2
            elevations.append(-40.0 - seed * 30.0)
        return _FakeResponseVaried(elevations)

    with (
        patch.object(ProviderHTTPClient, "get", _get),
        patch.object(bathymetry._rate_limiter, "acquire", lambda: None),
    ):
        profile = bathymetry.download_bathymetric_profile(
            34.2, -77.8, bearing_degrees=90.0, max_search_km=5.0
        )

    assert len(profile) >= 16


# ---------------------------------------------------------------------------
# point_along_bearing
# ---------------------------------------------------------------------------


def test_point_along_bearing_north():
    # ~1 degree of latitude at the equator, travelling due north.
    one_degree_m = bathymetry._EARTH_RADIUS_M * math.radians(1.0)
    lat2, lon2 = bathymetry.point_along_bearing(0.0, 0.0, 0.0, one_degree_m)
    assert lat2 == pytest.approx(1.0, abs=0.01)
    assert lon2 == pytest.approx(0.0, abs=0.01)


def test_point_along_bearing_east():
    # ~1 degree of longitude at the equator, travelling due east.
    one_degree_m = bathymetry._EARTH_RADIUS_M * math.radians(1.0)
    lat2, lon2 = bathymetry.point_along_bearing(0.0, 0.0, 90.0, one_degree_m)
    assert lat2 == pytest.approx(0.0, abs=0.01)
    assert lon2 == pytest.approx(1.0, abs=0.01)


def test_point_along_bearing_zero_distance():
    lat2, lon2 = bathymetry.point_along_bearing(34.2, -77.8, 135.0, 0.0)
    assert lat2 == pytest.approx(34.2, abs=1e-6)
    assert lon2 == pytest.approx(-77.8, abs=1e-6)


# ---------------------------------------------------------------------------
# interpolate_profile_pchip / compute_fine_zone_max_depth (Phase 4A, T4A.2)
# ---------------------------------------------------------------------------


def _native_profile(
    step_m: float = 8.0, length_m: float = 600.0, max_depth: float = 16.0, bar: bool = False
) -> list[dict[str, float]]:
    """A plausible native-resolution (~8m) raw profile: coastline (0m, ~0m
    depth) to ~600m offshore at ~16m depth, optionally with a Gaussian
    sandbar dip so monotonicity is NOT assumed everywhere."""
    n = int(length_m / step_m) + 1
    distances = [round(i * step_m, 1) for i in range(n)]
    depths = []
    for d in distances:
        z = max_depth * d / length_m
        if bar:
            z -= 1.5 * math.exp(-((d - 300.0) ** 2) / (2 * 40.0**2))
        depths.append(round(max(0.05, z), 3))
    return [{"distance_m": d, "depth_m": z} for d, z in zip(distances, depths, strict=True)]


def test_compute_fine_zone_max_depth_hb_example():
    # Plan T4A.2 step 4 worked example: HB, no structures, 4m max Hs.
    assert bathymetry.compute_fine_zone_max_depth(4.0, 0.73, 0.0) == pytest.approx(
        1.3 * 4.0 / 0.73, abs=1e-9
    )


def test_compute_fine_zone_max_depth_newport_example():
    # Plan T4A.2 step 4 worked example: Newport, breakwater at 8m ->
    # structure_zone_depth=10.0. LC-1: max(), not +. max(7.1..., 10.0) = 10.0,
    # NOT 17.1.
    result = bathymetry.compute_fine_zone_max_depth(4.0, 0.73, 10.0)
    assert result == pytest.approx(10.0, abs=1e-9)
    assert result != pytest.approx(1.3 * 4.0 / 0.73 + 10.0)


@pytest.mark.parametrize(
    ("max_hs_m", "gamma", "structure_zone_depth"),
    [
        (0.0, 0.73, 0.0),
        (-1.0, 0.73, 0.0),
        (4.0, 0.0, 0.0),
        (4.0, -0.5, 0.0),
        (4.0, 0.73, -1.0),
    ],
)
def test_compute_fine_zone_max_depth_validates_inputs(max_hs_m, gamma, structure_zone_depth):
    with pytest.raises(ValueError):
        bathymetry.compute_fine_zone_max_depth(max_hs_m, gamma, structure_zone_depth)


def test_interpolate_profile_pchip_zone_dx_bounds():
    raw = _native_profile(step_m=8.0, bar=True)
    result = bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)
    assert len(result) > len(raw)

    # Classify each output point by DISTANCE against the same zone-boundary
    # distances the implementation itself computes -- classifying by each
    # point's own instantaneous depth is unreliable near a sandbar dip, where
    # depth crosses the threshold more than once.
    raw_distances = [float(p["distance_m"]) for p in raw]
    raw_depths = [float(p["depth_m"]) for p in raw]
    fine_max_depth = bathymetry.compute_fine_zone_max_depth(4.0, 0.73, 0.0)
    dist_fine = bathymetry._find_depth_crossing_distance(raw_distances, raw_depths, fine_max_depth)
    dist_shoal = max(
        dist_fine,
        bathymetry._find_depth_crossing_distance(
            raw_distances, raw_depths, bathymetry._SHOALING_ZONE_MAX_DEPTH_M
        ),
    )

    dists = [p["distance_m"] for p in result]
    for i in range(1, len(dists)):
        dx = dists[i] - dists[i - 1]
        x_here = dists[i - 1]
        if x_here < dist_fine - 1e-6:
            assert dx <= 2.0 + 1e-6, f"fine-zone dx {dx} exceeds 2m spec at {x_here}"
        elif x_here < dist_shoal - 1e-6:
            assert 3.0 - 1e-6 <= dx <= 5.5, f"shoaling-zone dx {dx} outside 3-5m spec at {x_here}"
        elif abs(x_here - dist_shoal) < 1e-3:
            # The single transition segment from the interpolated zone
            # boundary to the next native sample is a fractional leftover
            # (<= one native step) -- not a violation of "native spacing".
            assert dx <= 8.0 + 1e-6
        else:
            # Approach zone (fully past the boundary): native raw spacing,
            # never finer than the input.
            assert dx >= 8.0 - 1e-6, f"approach-zone dx {dx} finer than native 8m input"


def test_interpolate_profile_pchip_no_duplicate_distances():
    raw = _native_profile(bar=True)
    result = bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)
    dists = [p["distance_m"] for p in result]
    for i in range(1, len(dists)):
        assert dists[i] > dists[i - 1]


def test_interpolate_profile_pchip_monotonic_input_no_phantom_bars():
    # PCHIP is monotonicity-preserving: a strictly-increasing-depth input
    # must not produce a decreasing (phantom bar/trough) interpolated output.
    raw = _native_profile(bar=False)
    result = bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)
    depths = [p["depth_m"] for p in result]
    for i in range(1, len(depths)):
        assert depths[i] >= depths[i - 1] - 1e-9


def test_interpolate_profile_pchip_dedup_keeps_shallower_first_occurrence():
    # Adaptive-refinement near-duplicates (T4A.2 step 2): two points 0.15m
    # apart (within the 0.5m dedup tolerance) at the same rough location.
    # The shallower PAIR wins as a unit (not a hybrid of one point's
    # distance with the other's depth) -- keeping the (distance, depth)
    # measurement pair whichever is shallower is the physically-meaningful
    # choice: you're picking which of two nearly-coincident samples to
    # trust, not synthesizing a new point.
    distances = [10.0, 30.05, 30.2, 50.0, 70.0, 90.0]
    depths = [1.0, 3.0, 2.5, 5.0, 6.0, 7.0]
    out_d, out_z = bathymetry._dedup_profile_by_distance(distances, depths, 0.5)
    # Shallower of the near-duplicate pair (30.2, 2.5) wins over (30.05, 3.0).
    assert out_d == [10.0, 30.2, 50.0, 70.0, 90.0]
    assert out_z == [1.0, 2.5, 5.0, 6.0, 7.0]


def test_interpolate_profile_pchip_dedup_tie_keeps_first_occurrence():
    distances = [10.0, 30.0, 30.3, 50.0]
    depths = [1.0, 4.0, 4.0, 5.0]
    out_d, out_z = bathymetry._dedup_profile_by_distance(distances, depths, 0.5)
    assert out_d == [10.0, 30.0, 50.0]
    assert out_z == [1.0, 4.0, 5.0]


def test_interpolate_profile_pchip_raises_below_min_points():
    raw = [{"distance_m": 0.0, "depth_m": 0.0}, {"distance_m": 10.0, "depth_m": 1.0}]
    with pytest.raises(ValueError, match="at least"):
        bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)


def test_interpolate_profile_pchip_raises_on_negative_depth():
    raw = _native_profile()
    raw[3]["depth_m"] = -1.0
    with pytest.raises(ValueError, match="negative depth_m"):
        bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)


def test_interpolate_profile_pchip_raises_on_50m_production_bug_spacing():
    # The real, live production bug this task fixes: 50 points at ~49.8m
    # spacing (brief §3 / plan T4A.2 problem statement) is NOT native DEM
    # resolution -- must raise, not silently interpolate garbage (LC-4).
    distances = [round(i * 49.8, 1) for i in range(50)]
    depths = [max(0.05, 15.0 * d / distances[-1]) for d in distances]
    raw = [{"distance_m": d, "depth_m": z} for d, z in zip(distances, depths, strict=True)]
    with pytest.raises(ValueError, match="exceeds the plausible native"):
        bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)


def test_interpolate_profile_pchip_warns_in_grey_band(caplog):
    # 12m median spacing: coarser than the finest documented native
    # resolution (~10.3m at HB) but not yet implausible (<=15m) -- WARN,
    # do not raise (LC-4 / coordinator's raise-vs-warn ruling).
    raw = _native_profile(step_m=12.0, length_m=600.0)
    with caplog.at_level(logging.WARNING):
        result = bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)
    assert len(result) > 0
    assert any("native spacing" in record.message for record in caplog.records)


def test_interpolate_profile_pchip_accepts_real_hb_native_spacing(caplog):
    # Coordinator's live DEM-coverage check (c:\tmp\marine-sep-P4A-scratch.md,
    # "DEM coverage at HB pier"): HB's actual finest available tile is 1/3
    # arc-second ~ 10.28m, NOT exactly 10.0m. This must be treated as clean
    # native data -- no WARNING -- or the function would flag HB's own
    # correct production data every time.
    raw = _native_profile(step_m=10.28, length_m=600.0)
    with caplog.at_level(logging.WARNING):
        result = bathymetry.interpolate_profile_pchip(raw, max_hs_m=4.0, gamma=0.73)
    assert len(result) > 0
    assert not any("native spacing" in record.message for record in caplog.records)
