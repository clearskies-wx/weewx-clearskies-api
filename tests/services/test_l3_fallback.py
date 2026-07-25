"""Tests for the L2 deep-water-reference fallback (P4A Round 2 scope
addition, ADR-093 Amendment 2 §4: "That spot runs L1 -> L2 -> SwellTrack
from L2's ~15 m reference, as an open beach does.").

Coverage:
  - _bulk_params_from_components(): quadrature Hs combination, dominant
    component's period/direction, empty-input handling.
  - _l3_fallback_points_from_dwr(): builds one MarineForecastPoint per
    timestep from the DWR spectral baseline, skips timesteps with no
    usable energy, never fabricates a value it cannot derive.

These test the routing logic in isolation (no SWAN subprocess, no file
I/O) — the DWR SPECOUT parsing itself (self._spectral_results population)
is exercised by the existing SPECOUT parsing tests in test_swan_runner.py
and is B1-owned code this round; this file only covers the NEW
fallback-construction path added adjacent to it.
"""

from __future__ import annotations

from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.services.swan_runner import (
    _bulk_params_from_components,
    _l3_fallback_points_from_dwr,
)


class TestBulkParamsFromComponents:
    def test_single_component_returns_its_own_values(self) -> None:
        hs, tp, direction = _bulk_params_from_components(
            [{"height": 1.5, "period": 12.0, "direction": 270.0}]
        )
        assert hs == 1.5
        assert tp == 12.0
        assert direction == 270.0

    def test_two_components_combine_in_quadrature(self) -> None:
        # Hs_total = sqrt(Hs1^2 + Hs2^2)
        hs, _, _ = _bulk_params_from_components(
            [
                {"height": 1.0, "period": 14.0, "direction": 260.0},
                {"height": 1.0, "period": 8.0, "direction": 200.0},
            ]
        )
        assert hs is not None
        assert abs(hs - (2.0 ** 0.5)) < 1e-9

    def test_period_and_direction_come_from_dominant_component(self) -> None:
        _, tp, direction = _bulk_params_from_components(
            [
                {"height": 0.3, "period": 8.0, "direction": 200.0},
                {"height": 2.0, "period": 14.0, "direction": 260.0},
            ]
        )
        assert tp == 14.0
        assert direction == 260.0

    def test_empty_components_returns_all_none(self) -> None:
        assert _bulk_params_from_components([]) == (None, None, None)

    def test_zero_height_components_return_all_none(self) -> None:
        assert _bulk_params_from_components(
            [{"height": 0.0, "period": 10.0, "direction": 180.0}]
        ) == (None, None, None)


class TestL3FallbackPointsFromDWR:
    def test_builds_one_point_per_timestep_with_energy(self) -> None:
        entries = [
            {
                "time": "2026-07-25T00:00:00Z",
                "components": [{"height": 1.2, "period": 13.0, "direction": 255.0}],
            },
            {
                "time": "2026-07-25T01:00:00Z",
                "components": [{"height": 1.4, "period": 13.5, "direction": 250.0}],
            },
        ]
        points = _l3_fallback_points_from_dwr(entries)
        assert len(points) == 2
        assert all(isinstance(p, MarineForecastPoint) for p in points)
        assert points[0].time == "2026-07-25T00:00:00Z"
        assert points[0].waveHeight == 1.2
        assert points[0].wavePeriod == 13.0
        assert points[0].waveDirection == 255.0
        # DWR point is nominally the 15m depth reference by construction
        # (_compute_15m_point) -- not an estimate for this fallback path.
        assert points[0].depth == 15.0

    def test_skips_timesteps_with_no_components(self) -> None:
        entries = [
            {"time": "2026-07-25T00:00:00Z", "components": []},
            {
                "time": "2026-07-25T01:00:00Z",
                "components": [{"height": 0.8, "period": 10.0, "direction": 240.0}],
            },
        ]
        points = _l3_fallback_points_from_dwr(entries)
        assert len(points) == 1
        assert points[0].time == "2026-07-25T01:00:00Z"

    def test_skips_entries_with_no_time(self) -> None:
        entries = [{"components": [{"height": 1.0, "period": 10.0, "direction": 200.0}]}]
        assert _l3_fallback_points_from_dwr(entries) == []

    def test_empty_input_returns_empty_list(self) -> None:
        assert _l3_fallback_points_from_dwr([]) == []

    def test_wind_fields_are_none_not_fabricated(self) -> None:
        """DWR SPECOUT carries no wind data -- the fallback must not guess."""
        entries = [
            {
                "time": "2026-07-25T00:00:00Z",
                "components": [{"height": 1.0, "period": 10.0, "direction": 200.0}],
            }
        ]
        pt = _l3_fallback_points_from_dwr(entries)[0]
        assert pt.windSpeed is None
        assert pt.windDirection is None
        assert pt.distanceFromShore is None
