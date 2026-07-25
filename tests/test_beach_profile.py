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

import json

import numpy as np
import pytest
from fastapi import HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation, SurfSpotConfig
from weewx_clearskies_api.endpoints.beach_profile import _build_transect_profile
from weewx_clearskies_api.services import station as station_mod
from weewx_clearskies_api.services import units as units_mod
from weewx_clearskies_api.services.station import StationInfo
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
        # T4A.9 (B1) landed these as required fields mid-round — real per-hour
        # handoff depth/source, always present on a real TransectResult.
        # Fixture values chosen to be distinguishable from the API's other
        # numbers so a wiring bug (wrong field read) shows up as a wrong
        # number, not a coincidentally-right one.
        handoff_depth_m=1.83,
        handoff_source_level="L3",
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


# ---------------------------------------------------------------------------
# 6. Per-hour handoff depth/source reach the response (T4A.6 item g / LC-R2-13)
#
# handoff_depth_m / handoff_source_level are now REQUIRED fields on
# TransectResult (B1's T4A.9 landed mid-round — see the fixture above) ->
# handoffDepthM / handoffSourceLevel on the response. These tests prove the
# wiring end to end using the exact names LC-R2-13 fixed with B1. If either
# side renames its attribute, this fails loudly instead of the response
# silently going back to null.
# ---------------------------------------------------------------------------


def test_handoff_fields_reach_response():
    profile = _build()
    assert profile["handoffDepthM"] == pytest.approx(1.83)
    assert profile["handoffSourceLevel"] == "L3"


def test_handoff_fields_are_unit_converted():
    profile_m = _build(h_unit="meter", d_unit="meter")
    profile_ft = _build(h_unit="foot", d_unit="foot")
    assert profile_m["handoffDepthM"] == pytest.approx(1.83)
    assert profile_ft["handoffDepthM"] == pytest.approx(1.83 * 3.28084, rel=0.01)
    # Source level is a passthrough label, never unit-converted.
    assert profile_m["handoffSourceLevel"] == "L3"
    assert profile_ft["handoffSourceLevel"] == "L3"


def test_handoff_fields_degrade_to_null_when_attribute_genuinely_absent():
    """_handoff_fields() guards against a caller that lacks the attribute
    entirely (getattr default), independent of whether the real
    TransectResult dataclass can construct one — it no longer can (B1 made
    both fields required positional args), so this exercises the defensive
    path directly against a stand-in object rather than the real dataclass.
    """
    from types import SimpleNamespace

    from weewx_clearskies_api.endpoints.beach_profile import _handoff_fields

    stub = SimpleNamespace()  # deliberately has neither attribute
    depth, source = _handoff_fields(stub, "meter")
    assert depth is None
    assert source is None


# ---------------------------------------------------------------------------
# 7. Endpoint-level: get_beach_profile() actually BUILDS AND PASSES
#    handoff_by_transect (T4A.6 item g, reopened 2026-07-25 — adversarial
#    audit finding, coordinator-verified).
#
# The tests above only exercise _build_transect_profile()'s serialization —
# they construct a TransectResult with handoff_depth_m already set, so they
# pass whether or not get_beach_profile() ever builds handoff_by_transect at
# all. That is exactly why the endpoint could silently fall back to
# TransectInfo.handoff_depth_m (documented as a SETUP-TIME PLACEHOLDER ONLY,
# swan_formats.py, MUST NOT be read as "the" handoff) while every test here
# stayed green. This section calls the real get_beach_profile() route
# function with monkeypatched provider/pipeline boundaries — mirroring the
# pattern in tests/test_surf_endpoint.py's
# test_surfbeat_precompute_block_runs_without_nameerror — and asserts on
# what _run_pipeline() was actually called with.
#
# No live network is exercised (PROVIDER-MANUAL §11): swan.fetch(),
# _compute_spot_transects(), and _run_pipeline() are all monkeypatched to
# deterministic in-process fakes.
# ---------------------------------------------------------------------------

_EP_LOCATION_ID = "test-handoff-spot"
_EP_TIME_ISO = "2026-07-25T12:00:00Z"

# The real per-hour selection published on the SPECOUT entry by
# swan_runner.py (_select_l3_handoff_spectra() -> "handoff_depth_m" /
# "handoff_source_level", swan_runner.py:877) — what the endpoint SHOULD
# read and pass through.
_EP_REAL_HANDOFF_DEPTH_M = 2.47
_EP_REAL_HANDOFF_SOURCE = "L3"

# TransectInfo's setup-time placeholder (swan_formats.py's documented
# L2_REFERENCE_DEPTH_M) — deliberately a DIFFERENT number from the real
# value above, so a wiring bug that silently falls back to this instead of
# the real per-hour selection is caught as a wrong number, not a
# coincidentally-right one.
_EP_PLACEHOLDER_DEPTH_M = 15.0


def _ep_make_location() -> MarineLocation:
    location = MarineLocation(
        _EP_LOCATION_ID,
        {
            "name": "Test Handoff Spot",
            "lat": "33.6534",
            "lon": "-118.0039",
            "activities": ["surf"],
            "ndbc_station_ids": [],
            "coops_station_ids": [],
            "nws_marine_zone_id": None,
        },
    )
    location.validate()
    return location


def _ep_make_surf_spot_config() -> SurfSpotConfig:
    config = SurfSpotConfig({"bottom_type": "sand", "topographic_feature": "straight_beach"})
    config.validate(_EP_LOCATION_ID)
    return config


def _ep_fake_swan_fetch(*, spot_id):
    return {
        "forecast": [
            {
                "time": _EP_TIME_ISO,
                "distanceFromShore": 500.0,
                "waveHeight": 1.2,
                "wavePeriod": 12.0,
                "waveDirection": 238.0,
            }
        ],
        "spectral": [
            {
                "time": _EP_TIME_ISO,
                # The real per-hour selection this test asserts gets read
                # and passed through — see swan_runner.py:877.
                "handoff_depth_m": _EP_REAL_HANDOFF_DEPTH_M,
                "handoff_source_level": _EP_REAL_HANDOFF_SOURCE,
            }
        ],
    }


def _ep_fake_compute_spot_transects(**_kwargs):
    return [
        TransectInfo(
            index=0,
            origin_lat=33.6534,
            origin_lon=-118.0039,
            bearing_deg=238.0,
            # Setup-time placeholder — must NOT be what reaches the response
            # when a real per-hour selection is available (see module docstring).
            handoff_depth_m=_EP_PLACEHOLDER_DEPTH_M,
            is_structure_affected=False,
        )
    ]


@pytest.fixture(autouse=True)
def _ep_reset_state():
    """Reset beach_profile module state + station/units caches around each test."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    beach_profile_mod._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()
    yield
    beach_profile_mod._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()


def _ep_wire_station() -> None:
    station_mod.reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="test-station",
        name="Test Station",
        latitude=33.6534,
        longitude=-118.0039,
        altitude=5.0,
        timezone="America/Los_Angeles",
        timezone_offset_minutes=-420,
        unit_system="US",
        hardware=None,
    )


def test_endpoint_passes_real_handoff_selection_to_pipeline(monkeypatch):
    """The bug: get_beach_profile() built NO handoff_by_transect at any of
    its three pipeline call sites, so run_pipeline() always fell back to
    TransectInfo.handoff_depth_m (the setup-time placeholder). This test
    calls the real endpoint function and would FAIL against the pre-fix
    code (asserted below to have actually failed pre-fix, not merely
    reasoned about) because handoff_by_transect would arrive empty and
    _default_handoff would substitute the 15.0 m placeholder for the real
    2.47 m per-hour selection.
    """
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _ep_wire_station()
    units_mod.set_units_block({}, "US")

    location = _ep_make_location()
    surf_config = _ep_make_surf_spot_config()
    beach_profile_mod.wire_beach_profile_config(
        MarineConfig(locations=[location], surf_spots={_EP_LOCATION_ID: surf_config})
    )

    captured_calls: list[dict] = []

    def _fake_run_pipeline(**kwargs):
        captured_calls.append(kwargs)
        # Minimal valid (non-degraded) result so the endpoint completes.
        tr = TransectResult(
            transect_index=0,
            is_structure_affected=False,
            hs_total_profile=np.array([1.0]),
            distances=np.array([50.0]),
            depths=np.array([2.0]),
            per_partition=[None],
            best_face_height_m=0.0,
            handoff_depth_m=_EP_REAL_HANDOFF_DEPTH_M,
            handoff_source_level=_EP_REAL_HANDOFF_SOURCE,
        )
        return PipelineResult(
            best_peak_face_height_m=0.0,
            spot_average_face_height_m=0.0,
            peel_angle_deg=None,
            peel_classification=None,
            peel_direction=None,
            transect_count=1,
            open_transect_count=1,
            per_transect=[tr],
            per_partition_breaks=[],
            degraded=False,
        )

    monkeypatch.setattr(
        beach_profile_mod, "_compute_spot_transects", _ep_fake_compute_spot_transects
    )
    monkeypatch.setattr(beach_profile_mod, "_run_pipeline", _fake_run_pipeline)
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch", _ep_fake_swan_fetch
    )

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    # 1. The real call path was actually exercised.
    assert len(captured_calls) == 1
    call = captured_calls[0]

    # 2. handoff_by_transect was built at all (the bug: it was never built,
    #    so run_pipeline() received no kwarg / an empty dict and fell back
    #    to the 15.0 m placeholder for every transect).
    assert "handoff_by_transect" in call
    assert call["handoff_by_transect"] == {0: (_EP_REAL_HANDOFF_DEPTH_M, _EP_REAL_HANDOFF_SOURCE)}

    # 3. And the number that got there is the REAL per-hour selection, not
    #    the setup-time placeholder — this is the assertion that catches a
    #    "plausible-looking wrong value" (the placeholder is itself a valid
    #    float, so only checking presence/shape would not catch a silent
    #    fallback).
    depth, source = call["handoff_by_transect"][0]
    assert depth == pytest.approx(_EP_REAL_HANDOFF_DEPTH_M)
    assert depth != pytest.approx(_EP_PLACEHOLDER_DEPTH_M)
    assert source == _EP_REAL_HANDOFF_SOURCE

    # 4. And the response itself reports the real value end to end.
    assert response["data"]["metadata"]["handoffDepthM"] is not None
    assert response["data"]["metadata"]["handoffDepthM"] != pytest.approx(_EP_PLACEHOLDER_DEPTH_M)


# ---------------------------------------------------------------------------
# 8. Endpoint-level: metadata.verticalDatum reads the real per-spot cache
#    (F3, reopened 2026-07-25 — auditor found it by grepping the field name
#    across the file, not by reading the change).
#
# Deliberately asserts a NON-"NAVD88" value. A fixture that happens to
# contain "NAVD88" would pass whether or not the code actually reads the
# cache or just kept the old hardcoded literal -- exactly how this defect
# survived the first pass (per the coordinator's standard for F2).
# ---------------------------------------------------------------------------


def _ep_minimal_pipeline_setup(monkeypatch, beach_profile_mod):
    """Shared minimal wiring for the verticalDatum tests below -- same
    station/config/swan-fetch/transects/pipeline fakes as the item-g test,
    factored out since these three tests only differ in the profile cache
    contents.
    """
    _ep_wire_station()
    units_mod.set_units_block({}, "US")

    location = _ep_make_location()
    surf_config = _ep_make_surf_spot_config()
    beach_profile_mod.wire_beach_profile_config(
        MarineConfig(locations=[location], surf_spots={_EP_LOCATION_ID: surf_config})
    )

    def _fake_run_pipeline(**kwargs):
        tr = TransectResult(
            transect_index=0,
            is_structure_affected=False,
            hs_total_profile=np.array([1.0]),
            distances=np.array([50.0]),
            depths=np.array([2.0]),
            per_partition=[None],
            best_face_height_m=0.0,
            handoff_depth_m=_EP_REAL_HANDOFF_DEPTH_M,
            handoff_source_level=_EP_REAL_HANDOFF_SOURCE,
        )
        return PipelineResult(
            best_peak_face_height_m=0.0,
            spot_average_face_height_m=0.0,
            peel_angle_deg=None,
            peel_classification=None,
            peel_direction=None,
            transect_count=1,
            open_transect_count=1,
            per_transect=[tr],
            per_partition_breaks=[],
            degraded=False,
        )

    monkeypatch.setattr(
        beach_profile_mod, "_compute_spot_transects", _ep_fake_compute_spot_transects
    )
    monkeypatch.setattr(beach_profile_mod, "_run_pipeline", _fake_run_pipeline)
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch", _ep_fake_swan_fetch
    )


def test_vertical_datum_reads_real_value_from_profile_cache(monkeypatch, tmp_path):
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    (tmp_path / f"{_EP_LOCATION_ID}.json").write_text(
        json.dumps({"vertical_datum": "MHW"}), encoding="utf-8"
    )
    monkeypatch.setattr(beach_profile_mod, "_PROFILE_CACHE_DIR", tmp_path)

    _ep_minimal_pipeline_setup(monkeypatch, beach_profile_mod)

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    # The actual assertion this test exists for: a real, non-default datum
    # (deliberately NOT "NAVD88") reaches the response. Pre-fix, this would
    # have been the hardcoded "NAVD88" regardless of what the cache said.
    assert response["data"]["metadata"]["verticalDatum"] == "MHW"


def test_vertical_datum_null_when_cache_missing(monkeypatch, tmp_path):
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    # tmp_path is empty -- no {spot_id}.json written.
    monkeypatch.setattr(beach_profile_mod, "_PROFILE_CACHE_DIR", tmp_path)

    _ep_minimal_pipeline_setup(monkeypatch, beach_profile_mod)

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    # Null, not a fallback to "NAVD88" or any other plausible-looking literal.
    assert response["data"]["metadata"]["verticalDatum"] is None


def test_vertical_datum_null_when_cache_says_unknown(monkeypatch, tmp_path):
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    (tmp_path / f"{_EP_LOCATION_ID}.json").write_text(
        json.dumps({"vertical_datum": "UNKNOWN"}), encoding="utf-8"
    )
    monkeypatch.setattr(beach_profile_mod, "_PROFILE_CACHE_DIR", tmp_path)

    _ep_minimal_pipeline_setup(monkeypatch, beach_profile_mod)

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    assert response["data"]["metadata"]["verticalDatum"] is None


# ---------------------------------------------------------------------------
# 9. SURF-PUBLISH-RESULTS-ONLY §3.2/§3.5/§3.6 — remote-mode profile fetch and
# null semantics.
#
# Remote mode: the local pipeline / compute-offload cascade is replaced by a
# call to the model host's own GET /surf/{spot_id}/profile. Bundled mode
# (already covered by section 7/8 above, which run with remote mode NOT
# configured) is unchanged.
# ---------------------------------------------------------------------------


def _rm_make_pipeline_result() -> PipelineResult:
    tr = TransectResult(
        transect_index=0,
        is_structure_affected=False,
        hs_total_profile=np.array([1.0]),
        distances=np.array([50.0]),
        depths=np.array([2.0]),
        per_partition=[None],
        best_face_height_m=0.0,
        handoff_depth_m=_EP_REAL_HANDOFF_DEPTH_M,
        handoff_source_level=_EP_REAL_HANDOFF_SOURCE,
    )
    return PipelineResult(
        best_peak_face_height_m=0.0,
        spot_average_face_height_m=0.0,
        peel_angle_deg=None,
        peel_classification=None,
        peel_direction=None,
        transect_count=1,
        open_transect_count=1,
        per_transect=[tr],
        per_partition_breaks=[],
        degraded=False,
    )


def _rm_wire_remote_endpoint(monkeypatch, beach_profile_mod) -> None:
    """Shared wiring for the remote-mode endpoint-level tests below."""
    _ep_wire_station()
    units_mod.set_units_block({}, "US")

    location = _ep_make_location()
    surf_config = _ep_make_surf_spot_config()
    beach_profile_mod.wire_beach_profile_config(
        MarineConfig(locations=[location], surf_spots={_EP_LOCATION_ID: surf_config})
    )
    monkeypatch.setattr(
        beach_profile_mod, "_compute_spot_transects", _ep_fake_compute_spot_transects
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch", _ep_fake_swan_fetch
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.is_remote_mode", lambda: True
    )


def test_remote_mode_success_queries_model_host_not_local_pipeline(monkeypatch):
    """§3.2/§3.5: remote mode calls swan.fetch_profile() -- never the local
    _run_pipeline() cascade, which is the courier round trip this brief
    eliminates."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _rm_wire_remote_endpoint(monkeypatch, beach_profile_mod)

    pipeline_result = _rm_make_pipeline_result()
    fetch_profile_calls: list[tuple[str, str]] = []

    def _fake_fetch_profile(*, spot_id, time_iso):
        fetch_profile_calls.append((spot_id, time_iso))
        return pipeline_result

    def _fail_if_local_pipeline_called(**_kwargs):
        raise AssertionError("local pipeline must not run in remote SWAN mode (§3.5)")

    monkeypatch.setattr(beach_profile_mod, "_run_pipeline", _fail_if_local_pipeline_called)
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch_profile", _fake_fetch_profile
    )

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    assert len(fetch_profile_calls) == 1
    assert fetch_profile_calls[0][0] == _EP_LOCATION_ID
    assert response["data"]["modelStatus"] == "ok"
    # US units wired -> handoffDepthM is converted to feet (matches
    # test_handoff_fields_are_unit_converted's convention above).
    assert response["data"]["handoffDepthM"] == pytest.approx(
        _EP_REAL_HANDOFF_DEPTH_M * 3.28084, rel=0.01
    )
    assert response["data"]["handoffSourceLevel"] == _EP_REAL_HANDOFF_SOURCE


def test_remote_mode_no_model_answer_best_mode_returns_200_null(monkeypatch):
    """§3.6: the model has no answer for this hour -> HTTP 200, null
    payload, modelStatus="unavailable" -- never a 404. Key set is IDENTICAL
    to the success ("best"/int) response, only nulled (lead correction:
    a typed client must not have to branch on which keys are present)."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _rm_wire_remote_endpoint(monkeypatch, beach_profile_mod)

    gap_reports: list = []
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch_profile", lambda **kw: None
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.report_gap",
        lambda **kw: gap_reports.append(kw),
    )

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    data = response["data"]
    assert data["modelStatus"] == "unavailable"
    assert data["locationId"] == _EP_LOCATION_ID
    assert data["timestep"] == _EP_TIME_ISO
    for key in (
        "transectIndex", "isStructureAffected", "transectBearingDeg",
        "transect", "breakPoints", "waveShapes", "surfZones",
        "jackingFactors", "handoffDepthM", "handoffSourceLevel",
    ):
        assert key in data, f"{key} missing from unavailable response (key set must match success)"
        assert data[key] is None
    assert data["perPartitionBreaks"] is None
    assert data["metadata"]["verticalDatum"] is None
    assert data["metadata"]["handoffDepthM"] is None

    assert len(gap_reports) == 1
    assert gap_reports[0]["endpoint"] == "profile"
    assert gap_reports[0]["spot_id"] == _EP_LOCATION_ID


def test_remote_mode_no_model_answer_all_mode_nulls_profiles_key(monkeypatch):
    """The transect_index=all success shape uses "profiles" (plural, a
    list) instead of the single-transect key set -- the null response must
    match THAT key set, not the single-transect one."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _rm_wire_remote_endpoint(monkeypatch, beach_profile_mod)
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch_profile", lambda **kw: None
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.report_gap", lambda **kw: None
    )

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="all")

    data = response["data"]
    assert data["modelStatus"] == "unavailable"
    assert data["profiles"] is None
    assert data["perPartitionBreaks"] is None
    # Single-transect-only keys must NOT appear in "all" mode's response,
    # success or failure -- a typed client keyed to transect_index=all's
    # shape should never see them.
    assert "transect" not in data
    assert "breakPoints" not in data


def test_no_swan_data_cached_returns_200_null_not_404(monkeypatch):
    """§3.6: "no SWAN data cached yet" is a model gap, not a configuration
    error -- 200/null, not 404. Applies in both modes; remote mode not
    configured here."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _ep_wire_station()
    units_mod.set_units_block({}, "US")
    location = _ep_make_location()
    surf_config = _ep_make_surf_spot_config()
    beach_profile_mod.wire_beach_profile_config(
        MarineConfig(locations=[location], surf_spots={_EP_LOCATION_ID: surf_config})
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch", lambda **kw: None
    )

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    assert response["data"]["modelStatus"] == "unavailable"
    assert response["data"]["timestep"] is None
    assert response["data"]["transect"] is None


def test_no_forecast_timesteps_returns_200_null_not_404(monkeypatch):
    """§3.6: SWAN data cached but no forecast timesteps -- also a model
    gap, not a configuration error."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _ep_wire_station()
    units_mod.set_units_block({}, "US")
    location = _ep_make_location()
    surf_config = _ep_make_surf_spot_config()
    beach_profile_mod.wire_beach_profile_config(
        MarineConfig(locations=[location], surf_spots={_EP_LOCATION_ID: surf_config})
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch",
        lambda **kw: {"forecast": [], "spectral": []},
    )

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    assert response["data"]["modelStatus"] == "unavailable"
    assert response["data"]["timestep"] is None


def test_bundled_mode_pipeline_produces_nothing_returns_200_null_not_404(monkeypatch):
    """§3.6: the 1D pipeline producing no usable per-transect output is a
    model gap in BOTH modes -- this reclassification is not remote-only.
    Exercised here with remote mode NOT configured (bundled)."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _ep_wire_station()
    units_mod.set_units_block({}, "US")
    location = _ep_make_location()
    surf_config = _ep_make_surf_spot_config()
    beach_profile_mod.wire_beach_profile_config(
        MarineConfig(locations=[location], surf_spots={_EP_LOCATION_ID: surf_config})
    )

    def _degraded_pipeline(**_kwargs):
        return PipelineResult(
            best_peak_face_height_m=0.0,
            spot_average_face_height_m=0.0,
            peel_angle_deg=None,
            peel_classification=None,
            peel_direction=None,
            transect_count=0,
            open_transect_count=0,
            per_transect=[],
            per_partition_breaks=[],
            degraded=True,
        )

    monkeypatch.setattr(
        beach_profile_mod, "_compute_spot_transects", _ep_fake_compute_spot_transects
    )
    monkeypatch.setattr(beach_profile_mod, "_run_pipeline", _degraded_pipeline)
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch", _ep_fake_swan_fetch
    )

    response = beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")

    assert response["data"]["modelStatus"] == "unavailable"


def test_no_transects_available_still_raises_404_not_200(monkeypatch):
    """§3.6 explicitly: "spot not configured or segment has zero length" is
    a CONFIGURATION error, not a model gap, and must stay 404 in both
    modes -- distinguishing the two is the entire point of §3.6."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    _ep_wire_station()
    units_mod.set_units_block({}, "US")
    location = _ep_make_location()
    surf_config = _ep_make_surf_spot_config()
    beach_profile_mod.wire_beach_profile_config(
        MarineConfig(locations=[location], surf_spots={_EP_LOCATION_ID: surf_config})
    )
    monkeypatch.setattr(beach_profile_mod, "_compute_spot_transects", lambda **kw: [])
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch", _ep_fake_swan_fetch
    )

    with pytest.raises(HTTPException) as exc_info:
        beach_profile_mod.get_beach_profile(_EP_LOCATION_ID, transect_index="best")
    assert exc_info.value.status_code == 404


def test_unknown_location_still_raises_404_not_200(monkeypatch):
    """Configuration error (unknown location) stays 404 -- untouched by
    §3.6."""
    import weewx_clearskies_api.endpoints.beach_profile as beach_profile_mod

    beach_profile_mod._marine_config = MarineConfig(locations=[])

    with pytest.raises(HTTPException) as exc_info:
        beach_profile_mod.get_beach_profile("nonexistent-spot", transect_index="best")
    assert exc_info.value.status_code == 404
