"""Vocabulary tests for endpoints/beach_profile.py (T4A.1).

Scope: assert the model's vocabulary (distance/depth/hs) is used
consistently by ``_build_transect_profile()`` across BOTH response paths —
the envelope points (``transect[]``) and the break points
(``breakPoints[]``) — and that the array key is ``transect`` (never
``hsEnvelope``). Both the single-transect and ``transect_index=all`` routes
call the same builder, so one set of assertions against the builder covers
both paths (per the brief's Do step 1 note that this is exactly the point).

Live provider / full-endpoint calls (SWAN fetch, remote SwellTrack) are
intentionally not exercised here, matching the "no live-network tests in
CI" convention used by tests/test_surf_endpoint.py. ``_build_transect_profile()``
is a pure function of its arguments, so it is fully unit-testable with
constructed fixtures.
"""

from __future__ import annotations

import numpy as np
import pytest

from weewx_clearskies_api.endpoints.beach_profile import _build_transect_profile
from weewx_clearskies_api.services.surf_1d_analytical import BreakPoint
from weewx_clearskies_api.services.surf_1d_pipeline import (
    PartitionBreakInfo,
    PartitionBreakResult,
    PipelineResult,
    TransectResult,
)
from weewx_clearskies_api.services.swan_formats import TransectInfo


def _make_fixture() -> tuple[TransectResult, PipelineResult, list[TransectInfo]]:
    """One partition, one transect, one primary + one secondary break point."""
    break_points = [
        BreakPoint(
            distance_m=50.0, depth_m=2.0, hs_m=1.5, breaker_type="plunging", iribarren=0.6
        ),
        BreakPoint(
            distance_m=120.0, depth_m=4.0, hs_m=1.0, breaker_type="spilling", iribarren=0.3
        ),
    ]
    pbr = PartitionBreakResult(
        partition_index=0,
        break_points=break_points,
        face_height_m=1.905,  # 1.5 * 1.27
        hs_at_break_m=1.5,
    )
    tr = TransectResult(
        transect_index=0,
        is_structure_affected=False,
        hs_total_profile=np.array([2.0, 1.8, 1.5]),
        distances=np.array([200.0, 100.0, 50.0]),
        depths=np.array([8.0, 4.0, 2.0]),
        per_partition=[pbr],
        best_face_height_m=1.905,
    )
    pbi = PartitionBreakInfo(
        partition_index=0,
        period_s=12.0,
        direction_deg=200.0,
        height_m=1.905,
        classification="swell",
        mean_break_distance_m=50.0,
        mean_face_height_m=1.905,
        peak_face_height_m=1.905,
        mean_break_depth_m=2.0,
        dominant_breaker_type="plunging",
    )
    pipeline_result = PipelineResult(
        best_peak_face_height_m=1.905,
        spot_average_face_height_m=1.905,
        peel_angle_deg=None,
        peel_classification=None,
        peel_direction=None,
        transect_count=1,
        open_transect_count=1,
        per_transect=[tr],
        per_partition_breaks=[pbi],
        degraded=False,
    )
    # Empty bathymetric_profile deliberately skips the run_1d_analytical()
    # call inside _build_transect_profile() — wave shapes/surf zones/jacking
    # factors are out of scope for this vocabulary-only test, and
    # surf_1d_analytical.py is owned by a different task this round.
    spot_transects = [
        TransectInfo(
            index=0,
            origin_lat=34.0,
            origin_lon=-118.5,
            bearing_deg=270.0,
            handoff_depth_m=10.0,
            is_structure_affected=False,
        )
    ]
    return tr, pipeline_result, spot_transects


def _build(**overrides) -> dict:
    tr, pipeline_result, spot_transects = _make_fixture()
    pbi_by_partition = {pbi.partition_index: pbi for pbi in pipeline_result.per_partition_breaks}
    kwargs = {
        "tr": tr,
        "pipeline_result": pipeline_result,
        "spot_transects": spot_transects,
        "beach_facing_degrees": 270.0,
        "h_unit": "meter",
        "d_unit": "meter",
        "pbi_by_partition": pbi_by_partition,
    }
    kwargs.update(overrides)
    return _build_transect_profile(**kwargs)


# ---------------------------------------------------------------------------
# 1. Array key is "transect", never "hsEnvelope"
# ---------------------------------------------------------------------------


def test_array_key_is_transect_not_hs_envelope():
    profile = _build()
    assert "transect" in profile
    assert "hsEnvelope" not in profile


# ---------------------------------------------------------------------------
# 2. Envelope points use distance/depth/hs — not distanceFromShore/waveHeight
# ---------------------------------------------------------------------------


def test_envelope_points_use_model_vocabulary():
    profile = _build()
    assert profile["transect"], "fixture must produce at least one envelope point"
    for point in profile["transect"]:
        assert set(point.keys()) == {"distance", "depth", "hs"}


# ---------------------------------------------------------------------------
# 3. Break points use distance/depth/hs/faceHeight/breakerType
# ---------------------------------------------------------------------------


def test_break_points_use_model_vocabulary():
    profile = _build()
    assert profile["breakPoints"], "fixture must produce at least one break point"
    for bp in profile["breakPoints"]:
        assert "distance" in bp
        assert "depth" in bp
        assert "hs" in bp
        assert "faceHeight" in bp
        assert "breakerType" in bp
        assert "distanceFromShore" not in bp
        assert "waveHeight" not in bp


# ---------------------------------------------------------------------------
# 4. Break points are sorted offshore-to-shore by "distance" (not the old key)
# ---------------------------------------------------------------------------


def test_break_points_sorted_by_distance_descending():
    profile = _build()
    distances = [bp["distance"] for bp in profile["breakPoints"]]
    assert distances == sorted(distances, reverse=True)


# ---------------------------------------------------------------------------
# 5. Unit conversion still applies to the renamed "hs" key
# ---------------------------------------------------------------------------


def test_hs_values_are_unit_converted():
    profile_m = _build(h_unit="meter", d_unit="meter")
    profile_ft = _build(h_unit="foot", d_unit="foot")
    hs_m = profile_m["transect"][0]["hs"]
    hs_ft = profile_ft["transect"][0]["hs"]
    assert hs_m > 0.0  # fixture's first envelope point has non-zero Hs
    assert hs_ft == pytest.approx(hs_m * 3.28084, rel=0.01)
