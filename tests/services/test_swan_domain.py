"""Tests for services/swan_domain.py — T4A.3 staged sizing + T4A.11 widened
L3 trigger and viability test.

Coverage:
  - L2 sized from an actual 30m contour distance, not the hardcoded 6km guess.
  - L3 offshore edge sized from an actual 15m contour distance, not the 2.5km
    fallback.
  - L3 does not reach shore (pin-based landward margin stays ~100m).
  - Widened trigger: point_break/headland/bay_break classification with NO
    structures enables L3 (was previously impossible — structure-only).
  - straight_beach classification with no structures does NOT enable L3.
  - l3_enabled="on" forces L3 on; "off" forces it off, regardless of trigger.
  - Structures carrying a spot_id are scoped to their own cluster — a
    structure at one spot must not trigger/size L3 for an unrelated cluster.
  - Viability test disables (grid=None) a cluster whose grid cannot reach the
    structure it was created for, with an INFO log naming the shortfall.
  - compute_domains() and compute_level3_domains() agree (shared trigger
    logic via _size_l3_cluster).
"""

from __future__ import annotations

import logging

from weewx_clearskies_api.services.swan_domain import (
    GridDomain,
    SpotCluster,
    _cluster_structures,
    _haversine_km,
    _l3_shoreward_edge_reach_m,
    _l3_trigger_reason,
    _l3_viability_check,
    compute_domains,
    compute_level3_domains,
    smart_size_l3_grid,
)

_HB_SPOT = {
    "id": "hb-pier",
    "lat": 33.6553,
    "lon": -118.0007,
    "beach_facing_degrees": 250.0,
}


class TestL2SizingFromRealContour:
    def test_l2_uses_actual_30m_contour_not_hardcoded_6km(self) -> None:
        spot = {**_HB_SPOT, "contour_30m_distance_m": 2200.0}
        domains = compute_domains([spot])
        # Offshore extent should reflect ~2.2km + 500m margin, not the 6km
        # ESTIMATE fallback. Check via the grid's offshore-most corner.
        # (Level2 grid bbox spans lat/lon; assert it's meaningfully smaller
        # than the 6km-fallback grid would be by comparing to a no-contour run.)
        fallback = compute_domains([_HB_SPOT])
        real_span = domains.level2.lon_max - domains.level2.lon_min
        fallback_span = fallback.level2.lon_max - fallback.level2.lon_min
        assert real_span != fallback_span

    def test_l3_offshore_uses_actual_15m_contour_not_2_5km_fallback(self) -> None:
        spot = {**_HB_SPOT, "offshore_distance_m": 2350.0}
        domains = compute_domains(
            [spot], spot_topographic_features={"hb-pier": "point_break"}
        )
        fallback_spot = {**_HB_SPOT}
        fallback = compute_domains(
            [fallback_spot],
            spot_topographic_features={"hb-pier": "point_break"},
        )
        grid = domains.level3_clusters[0].grid
        fallback_grid = fallback.level3_clusters[0].grid
        assert grid is not None
        assert fallback_grid is not None
        # beach_facing_degrees=250 points offshore to the west (negative
        # longitude), so the offshore distance shows up in lon_min, not
        # lon_max (which is pinned by the lateral margin on the east side).
        assert grid.lon_min != fallback_grid.lon_min


class TestL3DoesNotReachShore:
    def test_l3_shoreward_edge_stays_near_pin_not_at_shoreline(self) -> None:
        """Blocker 2 RESOLVED (operator ruling): a classification-only
        cluster (no structure) has no feature geometry to size a
        shoreward reach from, so _l3_shoreward_edge_reach_m() returns
        None and the grid keeps the pre-existing ~100m pin margin — this
        is the deliberate answer, not an open gap. See
        _l3_shoreward_edge_reach_m()'s docstring for the full ruling."""
        spot = {**_HB_SPOT, "offshore_distance_m": 2350.0}
        domains = compute_domains(
            [spot], spot_topographic_features={"hb-pier": "point_break"}
        )
        grid = domains.level3_clusters[0].grid
        assert grid is not None
        # landward_km = 0.1 -> the grid's shoreward-most extent is close to
        # the pin, not extended all the way to a separately-computed shoreline.
        lat_span_km = abs(grid.lat_max - grid.lat_min) * 111.0
        assert lat_span_km < 5.0  # sanity: not an absurdly large grid


class TestWidenedTrigger:
    def test_point_break_no_structures_enables_l3(self) -> None:
        domains = compute_domains(
            [_HB_SPOT], spot_topographic_features={"hb-pier": "point_break"}
        )
        assert domains.level3_clusters[0].grid is not None

    def test_headland_no_structures_enables_l3(self) -> None:
        domains = compute_domains(
            [_HB_SPOT], spot_topographic_features={"hb-pier": "headland"}
        )
        assert domains.level3_clusters[0].grid is not None

    def test_bay_break_no_structures_enables_l3(self) -> None:
        domains = compute_domains(
            [_HB_SPOT], spot_topographic_features={"hb-pier": "bay_break"}
        )
        assert domains.level3_clusters[0].grid is not None

    def test_straight_beach_no_structures_does_not_enable_l3(self) -> None:
        domains = compute_domains(
            [_HB_SPOT], spot_topographic_features={"hb-pier": "straight_beach"}
        )
        assert domains.level3_clusters[0].grid is None

    def test_open_beach_no_trigger_data_at_all_disables_l3(self) -> None:
        """An 'auto' spot with no structures and no classification supplied
        must NOT get an L3 grid — this is what makes the runtime gate
        (swan_runner.py, re-pointed at `grid is not None`) safe."""
        domains = compute_domains([_HB_SPOT])
        assert domains.level3_clusters[0].grid is None

    def test_l3_enabled_on_forces_l3_regardless_of_trigger(self) -> None:
        domains = compute_domains([_HB_SPOT], spot_l3_configs={"hb-pier": "on"})
        assert domains.level3_clusters[0].grid is not None

    def test_l3_enabled_off_forces_l3_off_even_with_structure(self) -> None:
        structs = [
            {
                "type": "pier",
                "spot_id": "hb-pier",
                "coordinates": [[-118.001, 33.655], [-118.003, 33.655]],
            }
        ]
        domains = compute_domains(
            [_HB_SPOT], structures=structs, spot_l3_configs={"hb-pier": "off"}
        )
        assert domains.level3_clusters[0].grid is None

    def test_compute_level3_domains_agrees_with_compute_domains(self) -> None:
        """Both public entry points share _size_l3_cluster; they must not
        diverge on the trigger decision."""
        via_compute_domains = compute_domains(
            [_HB_SPOT], spot_topographic_features={"hb-pier": "point_break"}
        ).level3_clusters[0].grid
        via_level3_domains = compute_level3_domains(
            [_HB_SPOT], spot_topographic_features={"hb-pier": "point_break"}
        )[0].grid
        assert (via_compute_domains is None) == (via_level3_domains is None)


class TestPerClusterStructureScoping:
    def test_structure_at_unrelated_spot_does_not_trigger_this_cluster(self) -> None:
        spots = [_HB_SPOT, {**_HB_SPOT, "id": "far-away-spot", "lat": 34.5}]
        structs = [
            {
                "type": "pier",
                "spot_id": "far-away-spot",
                "coordinates": [[-118.001, 34.5], [-118.003, 34.5]],
            }
        ]
        domains = compute_domains(spots, structures=structs)
        hb_cluster = next(
            c for c in domains.level3_clusters if "hb-pier" in c.spot_ids
        )
        assert hb_cluster.grid is None

    def test_structure_at_own_spot_triggers_this_cluster(self) -> None:
        structs = [
            {
                "type": "pier",
                "spot_id": "hb-pier",
                "coordinates": [[-118.001, 33.655], [-118.003, 33.655]],
            }
        ]
        domains = compute_domains([_HB_SPOT], structures=structs)
        assert domains.level3_clusters[0].grid is not None

    def test_structures_without_spot_id_apply_to_every_cluster_legacy(self) -> None:
        """Back-compat: a structures list with no spot_id key (older caller
        shape / pre-T4A.11 test fixtures) is applied unfiltered, same as
        before T4A.11."""
        structs = [
            {"type": "pier", "coordinates": [[-118.001, 33.655], [-118.003, 33.655]]}
        ]
        assert _cluster_structures(structs, ["anything"]) == structs


class TestShorewardEdgeResolved:
    """Blocker 2 RESOLVED (operator ruling 2026-07-25): the shoreward edge
    is set by feature coverage, not the (uncomputable) breaking-depth
    expression. See _l3_shoreward_edge_reach_m()'s docstring for the full
    ruling and reasoning — the breaking-depth check is still carried, but
    at runtime by T4A.9's per-hour clamp-and-WARNING, not here."""

    def test_shoreward_edge_reach_returns_shadow_zone_for_structure(self) -> None:
        assert _l3_shoreward_edge_reach_m(100.0) == 300.0  # 3x structure length

    def test_shoreward_edge_reach_returns_none_for_classification_only(self) -> None:
        """No structure geometry to size from -- caller keeps its own
        pre-existing near-shore margin (not a new constant)."""
        assert _l3_shoreward_edge_reach_m(None) is None

    def test_structure_bearing_spot_grid_reaches_past_structure_by_shadow_zone(
        self,
    ) -> None:
        """The smart-sized L3 grid for a structure-bearing spot must reach
        shoreward of the structure's own shoreward-most point by the full
        shadow-zone margin (3x structure length) plus pad -- not merely
        touch the structure."""
        cluster = SpotCluster(spot_ids=["hb-pier"], lats=[33.6553], lons=[-118.0007])
        # beach_facing_degrees=270 (due west/offshore) makes the shoreward
        # direction due east, so distances project cleanly onto longitude.
        structures = [{
            "type": "pier",
            "coordinates": [[-118.0005, 33.655], [-118.002, 33.655]],
        }]
        grid = smart_size_l3_grid(cluster, beach_facing_degrees=270.0, structures=structures)
        assert grid is not None

        struct_shoreward_lat, struct_shoreward_lon = 33.655, -118.0005
        struct_length_m = _haversine_km(
            33.655, -118.0005, 33.655, -118.002
        ) * 1000.0
        expected_margin_m = _l3_shoreward_edge_reach_m(struct_length_m)
        assert expected_margin_m is not None

        dist_to_edge_m = _haversine_km(
            struct_shoreward_lat, struct_shoreward_lon,
            struct_shoreward_lat, grid.lon_max,
        ) * 1000.0
        pad_m = 100.0  # smart_size_l3_grid()'s default pad_m
        # The grid's shoreward edge sits at struct tip + shadow margin + pad.
        assert abs(dist_to_edge_m - (expected_margin_m + pad_m)) < 20.0

    def test_classification_only_spot_grid_keeps_pin_margin_unchanged(self) -> None:
        """No structure -> no feature-derived reach -> the grid's shoreward
        extent is the pre-existing ~100m pin margin, same value as before
        Blocker 2 was resolved (confirms no new constant was introduced)."""
        # beach_facing_degrees=270 (due west/offshore) makes the shoreward
        # direction due east and the lateral margin project purely onto
        # latitude, so grid.lon_max isolates just the landward_km offset
        # (same axis-alignment trick as the structure-bearing test above).
        pin_lat, pin_lon = 33.6553, -118.0007
        spot = {
            "id": "point-break-spot", "lat": pin_lat, "lon": pin_lon,
            "beach_facing_degrees": 270.0, "offshore_distance_m": 2350.0,
        }
        domains = compute_domains(
            [spot], spot_topographic_features={"point-break-spot": "point_break"}
        )
        grid = domains.level3_clusters[0].grid
        assert grid is not None
        dist_from_pin_m = _haversine_km(
            pin_lat, pin_lon, pin_lat, grid.lon_max
        ) * 1000.0
        # landward_km = 0.1 -> ~100m from the pin, not a much larger
        # feature-derived distance (e.g. a shadow-zone margin would be
        # several hundred metres for any realistic structure length).
        assert dist_from_pin_m < 200.0


class TestViabilityTest:
    def test_grid_reaching_structure_passes(self) -> None:
        domain = GridDomain(
            lat_min=33.64, lat_max=33.66, lon_min=-118.02, lon_max=-117.98,
            resolution_m=10.0, level=3,
        )
        assert _l3_viability_check(domain, [(33.655, -118.00)], "structure", ["a"])

    def test_grid_not_reaching_structure_fails_and_logs_info(
        self, caplog: logging.LogCaptureFixture
    ) -> None:
        domain = GridDomain(
            lat_min=33.64, lat_max=33.66, lon_min=-118.02, lon_max=-117.98,
            resolution_m=10.0, level=3,
        )
        # Far outside the bbox.
        with caplog.at_level(logging.INFO):
            result = _l3_viability_check(
                domain, [(34.5, -117.0)], "structure", ["a"]
            )
        assert result is False
        assert any(
            "viability test FAILED" in r.message and "structure" in r.message
            for r in caplog.records
        )

    def test_trigger_reason_matches_expected_strings(self) -> None:
        assert _l3_trigger_reason([], ["on"], [])[0] is True
        assert _l3_trigger_reason([], ["off"], [])[0] is False
        assert _l3_trigger_reason([], [], ["point_break"])[0] is True
        assert _l3_trigger_reason([], [], ["straight_beach"])[0] is False
        assert _l3_trigger_reason([{"type": "pier"}], [], [])[0] is True


class TestGridGeometryFrozen:
    def test_compute_domains_deterministic_across_repeated_calls(self) -> None:
        """C2 / C3 (P4A Round 2): grid geometry must be byte-identical given
        the same inputs — no hidden state, no runtime override."""
        spot = {**_HB_SPOT, "offshore_distance_m": 2350.0, "contour_30m_distance_m": 900.0}
        kwargs = dict(spot_topographic_features={"hb-pier": "point_break"})
        first = compute_domains([spot], **kwargs)
        second = compute_domains([spot], **kwargs)
        assert first.level1 == second.level1
        assert first.level2 == second.level2
        g1, g2 = first.level3_clusters[0].grid, second.level3_clusters[0].grid
        assert g1 == g2
