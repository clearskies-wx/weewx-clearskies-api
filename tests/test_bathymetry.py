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
