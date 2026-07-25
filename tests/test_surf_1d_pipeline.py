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


# ---------------------------------------------------------------------------
# partition_source / watershed_partitions — T4B.2
#
# Build-and-measure only (trigger 1, not approved). These tests cover the
# selector's plumbing, not a claim that watershed partitioning is correct or
# adopted: (a) the new parameters do not change default behaviour at all,
# (b) the watershed path is reachable when explicitly selected, (c) the
# height-descending sort + wind-sea-discard documented in
# _watershed_partitions_to_pipeline_format() actually happens.
# ---------------------------------------------------------------------------

_WATERSHED_PARTITIONS = [
    # Deliberately NOT already height-sorted, and includes the wind-sea
    # partition (index 1) out of "natural SWAN order" position, so a test
    # asserting descending-height order after conversion cannot pass by
    # accident of input order.
    {
        "partition_index": 2,
        "is_wind_sea": False,
        "height": 2.9,
        "period": 12.0,
        "direction": 184.0,
        "spread": 20.0,
        "energy": (2.9 / 4.0) ** 2,
        "classification": "swell",
    },
    {
        "partition_index": 1,
        "is_wind_sea": True,
        "height": 0.4,
        "period": 5.0,
        "direction": 270.0,
        "spread": 30.0,
        "energy": (0.4 / 4.0) ** 2,
        "classification": "wind_swell",
    },
    {
        "partition_index": 3,
        "is_wind_sea": False,
        "height": 0.7,
        "period": 23.0,
        "direction": 196.0,
        "spread": 8.0,
        "energy": (0.7 / 4.0) ** 2,
        "classification": "groundswell",
    },
]


def test_partition_source_defaults_to_neighbourhood_unchanged():
    """Omitting the new parameters must reproduce prior behaviour exactly —
    the coordinator's explicit constraint on this addition."""
    transects = [_transect(0)]

    result_implicit = run_pipeline(
        specout_data=None,
        transects=transects,
        tide_level=0.0,
        beach_facing=210.0,
        bulk_hs=1.5,
        bulk_tp=12.0,
        bulk_dir=210.0,
    )
    result_explicit = run_pipeline(
        specout_data=None,
        transects=transects,
        tide_level=0.0,
        beach_facing=210.0,
        bulk_hs=1.5,
        bulk_tp=12.0,
        bulk_dir=210.0,
        partition_source="neighbourhood",
        watershed_partitions=None,
    )

    assert result_implicit.best_peak_face_height_m == result_explicit.best_peak_face_height_m
    assert result_implicit.spot_average_face_height_m == result_explicit.spot_average_face_height_m
    assert result_implicit.degraded == result_explicit.degraded
    assert len(result_implicit.per_partition_breaks) == len(result_explicit.per_partition_breaks)


def test_watershed_partition_source_runs_and_sorts_by_descending_height():
    transects = [_transect(0)]

    result = run_pipeline(
        specout_data=None,
        transects=transects,
        tide_level=0.0,
        beach_facing=210.0,
        partition_source="watershed",
        watershed_partitions=_WATERSHED_PARTITIONS,
    )

    assert not result.degraded
    assert len(result.per_partition_breaks) == 3
    heights = [b.height_m for b in result.per_partition_breaks]
    assert heights == sorted(heights, reverse=True)
    assert heights[0] == 2.9  # largest partition first, regardless of input order


def test_watershed_partition_source_empty_list_degrades_like_no_specout():
    transects = [_transect(0)]

    result = run_pipeline(
        specout_data=None,
        transects=transects,
        tide_level=0.0,
        beach_facing=210.0,
        partition_source="watershed",
        watershed_partitions=[],
    )

    assert result.degraded
    assert result.per_transect == []
