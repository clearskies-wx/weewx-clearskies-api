"""Unit tests for services/surf_1d_pipeline.py — Phase 4A T4A.9 contract
additions (LC-R2 "Data contract you must emit, for B3 (T4A.6 item g)").

Covers only the new surface: ``run_pipeline()``'s ``handoff_by_transect``
parameter and the ``TransectResult.handoff_depth_m`` /
``.handoff_source_level`` fields it populates. The rest of the pipeline
(RSS combination, break-point aggregation, peel angle, etc.) is pre-existing
behaviour, exercised by ``tests/test_surf_endpoint.py`` — not re-verified
here per "Simple means simple."
"""

from __future__ import annotations

from weewx_clearskies_api.services.surf_1d_pipeline import run_pipeline
from weewx_clearskies_api.services.swan_formats import TransectInfo
from weewx_clearskies_api.services.transect_handoff import L2_REFERENCE_DEPTH_M

# Simple monotonic bathymetric profile shared by every synthetic transect:
# offshore (15 m) -> nearshore (1 m), 30 points.
_BATHY_PROFILE = [
    {"distance_m": float(500 - i * 15), "depth_m": max(1.0, 15.0 - i * 0.5)}
    for i in range(30)
]


def _transect(index: int, *, handoff_depth_m: float = 10.0) -> TransectInfo:
    return TransectInfo(
        index=index,
        origin_lat=33.66,
        origin_lon=-118.00,
        bearing_deg=210.0,
        handoff_depth_m=handoff_depth_m,
        is_structure_affected=False,
        bathymetric_profile=list(_BATHY_PROFILE),
    )


def _run(transects, handoff_by_transect=None):
    return run_pipeline(
        specout_data=None,
        transects=transects,
        tide_level=0.0,
        beach_facing=210.0,
        bulk_hs=1.5,
        bulk_tp=12.0,
        bulk_dir=210.0,
        handoff_by_transect=handoff_by_transect,
    )


def test_handoff_fields_present_and_used_when_supplied():
    transects = [_transect(0), _transect(1)]
    handoff_by_transect = {0: (1.78, "L3"), 1: (15.0, "L2")}

    result = _run(transects, handoff_by_transect)

    assert not result.degraded or result.per_transect  # sanity: pipeline ran
    by_index = {tr.transect_index: tr for tr in result.per_transect}
    assert by_index[0].handoff_depth_m == 1.78
    assert by_index[0].handoff_source_level == "L3"
    assert by_index[1].handoff_depth_m == 15.0
    assert by_index[1].handoff_source_level == "L2"


def test_handoff_fields_fall_back_when_not_supplied():
    transects = [_transect(0, handoff_depth_m=L2_REFERENCE_DEPTH_M)]

    result = _run(transects, handoff_by_transect=None)

    by_index = {tr.transect_index: tr for tr in result.per_transect}
    assert by_index[0].handoff_depth_m == L2_REFERENCE_DEPTH_M
    assert by_index[0].handoff_source_level == "L2"


def test_handoff_fields_fall_back_for_transect_missing_from_mapping():
    transects = [_transect(0), _transect(1, handoff_depth_m=L2_REFERENCE_DEPTH_M)]
    # Only transect 0 has a real per-hour selection this call.
    handoff_by_transect = {0: (3.6, "L3")}

    result = _run(transects, handoff_by_transect)

    by_index = {tr.transect_index: tr for tr in result.per_transect}
    assert by_index[0].handoff_depth_m == 3.6
    assert by_index[0].handoff_source_level == "L3"
    # Transect 1 wasn't in the mapping — falls back to its TransectInfo
    # placeholder, labelled "L2".
    assert by_index[1].handoff_depth_m == L2_REFERENCE_DEPTH_M
    assert by_index[1].handoff_source_level == "L2"


def test_handoff_fields_vary_per_transect_same_hour():
    # T4A.6 item g: the field is per-transect per-hour, not a single global
    # value — different transects may resolve to different depths/sources
    # within the SAME pipeline call (same forecast hour).
    transects = [_transect(0), _transect(1), _transect(2)]
    handoff_by_transect = {0: (1.78, "L3"), 1: (7.12, "L3"), 2: (15.0, "L2")}

    result = _run(transects, handoff_by_transect)

    depths = {tr.transect_index: tr.handoff_depth_m for tr in result.per_transect}
    sources = {tr.transect_index: tr.handoff_source_level for tr in result.per_transect}
    assert depths == {0: 1.78, 1: 7.12, 2: 15.0}
    assert sources == {0: "L3", 1: "L3", 2: "L2"}
