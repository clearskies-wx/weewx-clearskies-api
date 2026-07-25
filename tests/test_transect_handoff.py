"""Unit tests for services/transect_handoff.py — Phase 4A, T4A.9 + T4A.10.

Covers:
  - compute_transect_shadows(): geometric shadow classification survives the
    T4A.9 rewrite unchanged (guards coordinator constraint C1 — the shadow
    computation must not be deleted or weakened when the depth rule changes).
  - select_hourly_handoff(): the per-forecast-hour handoff formula
    (1.3 * Hs / gamma), boundary-cell clamping (ADR-095 Amendment 2), and the
    L2 fallback when a transect has no L3 grid.
  - refine_handoff_with_qb(): T4A.10's runtime breaking-zone assertion,
    extending select_hourly_handoff()'s lineage rather than a second scanner
    (LC-R2-5) — clean QB is a no-op, a violation moves seaward, and an
    unrecoverable violation raises HandoffBreakingError with the greppable
    "SWAN handoff" ERROR log and increments the /metrics counter.
  - The 5 m floor (_MIN_HANDOFF_DEPTH_M) is gone (LC-R2-3, operator-approved
    removal watched in testing): the shallow end (~1 m swell -> ~1.8 m) must
    resolve correctly rather than being clamped to 5 m.
  - C2: select_hourly_handoff()/refine_handoff_with_qb() never touch grid
    geometry — compute_domains() output is byte-identical before and after
    a simulated 72-hour cycle of per-hour lookups.
"""

from __future__ import annotations

import math

import pytest

from weewx_clearskies_api.config.marine_config import StructureConfig
from weewx_clearskies_api.metrics import HANDOFF_QB_VIOLATIONS_TOTAL
from weewx_clearskies_api.services import transect_handoff as th
from weewx_clearskies_api.services.transect_handoff import (
    L2_REFERENCE_DEPTH_M,
    HandoffBreakingError,
    TransectShadow,
    compute_transect_shadows,
    refine_handoff_with_qb,
    select_hourly_handoff,
)

_M_PER_DEG_LAT = 111_320.0


# ---------------------------------------------------------------------------
# Shared HB-Pier-like fixture (mirrors the module's own self-test geometry)
# ---------------------------------------------------------------------------

PIER_SHORE_LAT = 33.6592
PIER_SHORE_LON = -118.0027
PIER_SEA_LAT = 33.6571
PIER_SEA_LON = -118.0048
PIER_DEPTH_M = 7.0

BEACH_FACING_DEG = 210.0
SHORE_BEARING_DEG = 300.0
ORIGIN_LAT = 33.6593
ORIGIN_LON = -118.0013


def _mock_bathy(lat: float, lon: float) -> float:
    if abs(lat - PIER_SEA_LAT) < 1e-4 and abs(lon - PIER_SEA_LON) < 1e-4:
        return PIER_DEPTH_M
    if abs(lat - PIER_SHORE_LAT) < 1e-4 and abs(lon - PIER_SHORE_LON) < 1e-4:
        return 0.5
    dist_lat = (lat - PIER_SHORE_LAT) * _M_PER_DEG_LAT
    dist_lon = (
        (lon - PIER_SHORE_LON) * _M_PER_DEG_LAT * math.cos(math.radians(PIER_SHORE_LAT))
    )
    dist_m = math.sqrt(dist_lat**2 + dist_lon**2)
    return min(15.0, max(0.0, dist_m * 0.02))


def _pier() -> StructureConfig:
    return StructureConfig(
        {
            "type": "pier",
            "material": "permeable",
            "length_m": 300.0,
            "bearing_degrees": 225.0,
            "distance_m": 0.0,
            "coordinates": [
                [PIER_SHORE_LON, PIER_SHORE_LAT],
                [PIER_SEA_LON, PIER_SEA_LAT],
            ],
        }
    )


def _transect_origins(n: int = 30) -> list[tuple[float, float]]:
    origins: list[tuple[float, float]] = []
    shore_rad = math.radians(SHORE_BEARING_DEG)
    for i in range(n):
        dist_m = i * 10.0
        dlat = math.cos(shore_rad) * dist_m / _M_PER_DEG_LAT
        dlon = math.sin(shore_rad) * dist_m / (
            _M_PER_DEG_LAT * math.cos(math.radians(ORIGIN_LAT))
        )
        origins.append((ORIGIN_LAT + dlat, ORIGIN_LON + dlon))
    return origins


# ---------------------------------------------------------------------------
# compute_transect_shadows() — C1 guard
# ---------------------------------------------------------------------------


def test_compute_transect_shadows_some_shadowed_some_open():
    origins = _transect_origins()
    bearings = [BEACH_FACING_DEG] * len(origins)

    results = compute_transect_shadows(
        transect_origins=origins,
        transect_bearings=bearings,
        structures=[_pier()],
        beach_facing_degrees=BEACH_FACING_DEG,
        bathymetry_profile_fn=_mock_bathy,
    )

    assert len(results) == len(origins)
    assert all(isinstance(r, TransectShadow) for r in results)
    n_shadowed = sum(1 for r in results if r.is_shadowed)
    # Same invariant as the pre-T4A.9 self-test: some shadowed, some open,
    # exact count is geometry-dependent.
    assert 1 <= n_shadowed <= len(results) - 1
    for r in results:
        if r.is_shadowed:
            assert r.shadowing_structures == ["pier(300m)"]
        else:
            assert r.shadowing_structures == []


def test_compute_transect_shadows_no_structures_all_open():
    origins = _transect_origins(5)
    bearings = [BEACH_FACING_DEG] * len(origins)

    results = compute_transect_shadows(
        transect_origins=origins,
        transect_bearings=bearings,
        structures=[],
        beach_facing_degrees=BEACH_FACING_DEG,
        bathymetry_profile_fn=_mock_bathy,
    )

    assert all(not r.is_shadowed for r in results)
    assert all(r.shadowing_structures == [] for r in results)


def test_compute_transect_shadows_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        compute_transect_shadows(
            transect_origins=[(33.0, -118.0)],
            transect_bearings=[1.0, 2.0],
            structures=[],
            beach_facing_degrees=210.0,
            bathymetry_profile_fn=_mock_bathy,
        )


def test_compute_transect_shadows_empty_returns_empty():
    assert (
        compute_transect_shadows(
            transect_origins=[],
            transect_bearings=[],
            structures=[],
            beach_facing_degrees=210.0,
            bathymetry_profile_fn=_mock_bathy,
        )
        == []
    )


# ---------------------------------------------------------------------------
# select_hourly_handoff() — T4A.9
# ---------------------------------------------------------------------------

# Synthetic L3 curve: 40 stations, offshore (15 m) -> shoreward (1.5 m),
# evenly spaced in depth — enough resolution to land close to any target.
_SYNTHETIC_STATIONS = [15.0 - i * (13.5 / 39.0) for i in range(40)]


def test_select_hourly_handoff_1m_swell_near_1_8m():
    sel = select_hourly_handoff(1.0, _SYNTHETIC_STATIONS)
    assert sel.source_level == "L3"
    # 1.3 * 1.0 / 0.73 = 1.7808 m (T4A.9 Accept criteria: ~1.8 m)
    assert sel.handoff_depth_m == pytest.approx(1.3 * 1.0 / 0.73, abs=1e-6)
    assert sel.station_depth_m == pytest.approx(1.78, abs=0.4)
    assert not sel.clamped


def test_select_hourly_handoff_4m_swell_near_7_1m():
    sel = select_hourly_handoff(4.0, _SYNTHETIC_STATIONS)
    assert sel.source_level == "L3"
    # 1.3 * 4.0 / 0.73 = 7.123 m (T4A.9 Accept criteria: ~7.1 m)
    assert sel.handoff_depth_m == pytest.approx(1.3 * 4.0 / 0.73, abs=1e-6)
    assert sel.station_depth_m == pytest.approx(7.12, abs=0.4)
    assert not sel.clamped


def test_select_hourly_handoff_1m_and_4m_resolve_to_different_stations():
    sel_1m = select_hourly_handoff(1.0, _SYNTHETIC_STATIONS)
    sel_4m = select_hourly_handoff(4.0, _SYNTHETIC_STATIONS)
    assert sel_1m.station_index != sel_4m.station_index
    assert sel_1m.station_depth_m < sel_4m.station_depth_m


def test_select_hourly_handoff_varies_across_hours():
    # T4A.9 Accept: handoff depth varies across the 72 forecast hours for the
    # same transect. Simulate a swell that changes hour to hour.
    hourly_hs = [1.0 + 3.0 * abs(math.sin(h / 12.0)) for h in range(72)]
    depths = {select_hourly_handoff(hs, _SYNTHETIC_STATIONS).handoff_depth_m for hs in hourly_hs}
    assert len(depths) > 1


@pytest.mark.parametrize("stations", [None, [], [1.0]])
def test_select_hourly_handoff_no_l3_falls_back_to_l2(stations):
    sel = select_hourly_handoff(2.0, stations)
    assert sel.source_level == "L2"
    assert sel.handoff_depth_m == L2_REFERENCE_DEPTH_M
    assert sel.station_depth_m == L2_REFERENCE_DEPTH_M
    assert sel.station_index is None
    assert not sel.clamped


def test_select_hourly_handoff_never_selects_boundary_station():
    # A very large Hs pushes the target depth deeper than every station —
    # the nearest match is the offshore boundary station (index 0). The
    # selection must clamp to the nearest INTERIOR station instead
    # (ADR-095 Amendment 2: no SPECOUT at an L3 boundary cell).
    sel = select_hourly_handoff(50.0, _SYNTHETIC_STATIONS)
    assert sel.source_level == "L3"
    assert sel.station_index not in (0, len(_SYNTHETIC_STATIONS) - 1)
    assert sel.clamped

    # And a near-zero Hs pushes the target shallower than every station —
    # nearest match is the shoreward boundary (index n-1); must also clamp.
    sel_shallow = select_hourly_handoff(0.001, _SYNTHETIC_STATIONS)
    assert sel_shallow.station_index not in (0, len(_SYNTHETIC_STATIONS) - 1)
    assert sel_shallow.clamped


def test_select_hourly_handoff_too_few_stations_falls_back_to_l2():
    sel = select_hourly_handoff(2.0, [10.0, 5.0])  # only 2 stations, no interior
    assert sel.source_level == "L2"
    assert sel.station_index is None


def test_no_min_handoff_depth_floor_symbol_remains():
    # LC-R2-3: the 5 m floor is removed outright, not just unused.
    assert not hasattr(th, "_MIN_HANDOFF_DEPTH_M")


def test_select_hourly_handoff_shallow_end_not_clamped_to_old_5m_floor():
    # LC-R2-3 watch condition: a small swell must resolve near its true
    # shallow target, not the old 5 m floor.
    sel = select_hourly_handoff(0.5, _SYNTHETIC_STATIONS)
    target = 1.3 * 0.5 / 0.73
    assert target < 5.0  # sanity: this really is a sub-5m case
    assert sel.station_depth_m < 5.0
    assert sel.handoff_depth_m == pytest.approx(target, abs=1e-6)


# ---------------------------------------------------------------------------
# refine_handoff_with_qb() — T4A.10
# ---------------------------------------------------------------------------


def test_refine_handoff_with_qb_clean_qb_is_noop():
    sel = select_hourly_handoff(1.0, _SYNTHETIC_STATIONS)
    qb = [0.0] * len(_SYNTHETIC_STATIONS)
    refined = refine_handoff_with_qb(sel, qb, _SYNTHETIC_STATIONS, transect_index=0, hour=1)
    assert refined == sel


def test_refine_handoff_with_qb_no_qb_data_is_noop():
    sel = select_hourly_handoff(1.0, _SYNTHETIC_STATIONS)
    for empty in (None, []):
        refined = refine_handoff_with_qb(
            sel, empty, _SYNTHETIC_STATIONS, transect_index=0, hour=1
        )
        assert refined == sel


def test_refine_handoff_with_qb_l2_selection_is_noop_even_with_qb_data():
    sel = select_hourly_handoff(2.0, None)  # forces L2 fallback
    assert sel.source_level == "L2"
    qb = [0.9] * 40  # would be a violation if this were treated as L3
    refined = refine_handoff_with_qb(sel, qb, None, transect_index=0, hour=1)
    assert refined == sel


def test_refine_handoff_with_qb_moves_seaward_on_violation(caplog):
    sel = select_hourly_handoff(1.0, _SYNTHETIC_STATIONS)
    idx = sel.station_index
    assert idx is not None and idx > 1

    qb = [0.0] * len(_SYNTHETIC_STATIONS)
    qb[idx] = 0.15  # inject a breaking-zone violation at the selected station
    qb[idx - 1] = 0.01  # clean one station seaward

    with caplog.at_level("WARNING"):
        refined = refine_handoff_with_qb(
            sel, qb, _SYNTHETIC_STATIONS, transect_index=3, hour=6
        )

    assert refined.station_index == idx - 1
    assert refined.station_depth_m == pytest.approx(_SYNTHETIC_STATIONS[idx - 1])
    assert refined.clamped is True
    assert any("SWAN handoff" in rec.message for rec in caplog.records)


def test_refine_handoff_with_qb_unrecoverable_raises_and_logs_error(caplog):
    sel = select_hourly_handoff(1.0, _SYNTHETIC_STATIONS)
    qb = [0.9] * len(_SYNTHETIC_STATIONS)  # every station is "breaking"

    before = HANDOFF_QB_VIOLATIONS_TOTAL.labels(transect_index="7")._value.get()

    with caplog.at_level("ERROR"), pytest.raises(HandoffBreakingError) as excinfo:
        refine_handoff_with_qb(
            sel, qb, _SYNTHETIC_STATIONS, transect_index=7, hour=14
        )

    err = excinfo.value
    assert err.transect_index == 7
    assert err.hour == 14
    assert err.qb == pytest.approx(0.9)
    assert any(
        "SWAN handoff" in rec.message and rec.levelname == "ERROR"
        for rec in caplog.records
    )

    after = HANDOFF_QB_VIOLATIONS_TOTAL.labels(transect_index="7")._value.get()
    assert after == before + 1


def test_refine_handoff_with_qb_never_moves_to_boundary_station():
    # Even when scanning for a clean station, index 0 (offshore boundary)
    # must never be selected.
    sel = select_hourly_handoff(1.0, _SYNTHETIC_STATIONS)
    idx = sel.station_index
    qb = [0.9] * len(_SYNTHETIC_STATIONS)
    qb[1] = 0.9  # even the shallowest legal interior station is dirty
    qb[0] = 0.0  # only the boundary station (illegal) is clean

    with pytest.raises(HandoffBreakingError):
        refine_handoff_with_qb(
            sel, qb, _SYNTHETIC_STATIONS, transect_index=0, hour=1,
            max_deepening_stations=idx,
        )


# ---------------------------------------------------------------------------
# C2 — grid geometry must never move. compute_domains() output is
# byte-identical before and after a simulated 72-hour per-hour-selection
# cycle (this module never calls into grid sizing at all).
# ---------------------------------------------------------------------------


def test_transect_handoff_module_never_imports_grid_sizing():
    """Static guard: the per-hour selection code must not import anything
    from swan_domain (grid construction) — it is a pure lookup against
    station depths supplied by the caller (C2)."""
    import inspect

    source = inspect.getsource(th)
    assert "swan_domain" not in source
    assert "compute_domains" not in source
    assert "CGRID" not in source


def test_compute_domains_byte_identical_across_simulated_cycle():
    from weewx_clearskies_api.services.swan_domain import compute_domains

    spot_locations = [
        {
            "id": "huntington-city-beach-pier",
            "lat": PIER_SHORE_LAT,
            "lon": PIER_SHORE_LON,
            "beach_facing_degrees": BEACH_FACING_DEG,
            "contour_30m_distance_m": 1800.0,
            "offshore_distance_m": 900.0,
        }
    ]

    before = compute_domains(spot_locations)

    # Simulate a 72-hour forecast cycle of per-hour handoff selection —
    # nothing in this loop may touch grid sizing.
    for hour in range(72):
        hs = 1.0 + 3.0 * abs(math.sin(hour / 11.0))
        sel = select_hourly_handoff(hs, _SYNTHETIC_STATIONS)
        qb = [0.0] * len(_SYNTHETIC_STATIONS)
        refine_handoff_with_qb(
            sel, qb, _SYNTHETIC_STATIONS, transect_index=0, hour=hour
        )

    after = compute_domains(spot_locations)

    assert before == after


# ---------------------------------------------------------------------------
# T4B.1: breaking_margin_depth_m() — the formula lifted out of
# select_hourly_handoff() for setup-time per-transect band sizing.
# ---------------------------------------------------------------------------


def test_breaking_margin_depth_m_matches_select_hourly_handoff_formula():
    """Same equation, same constants, same result as the inline expression
    select_hourly_handoff() used before this extraction — not a new formula
    (rules/coding.md DRY; CLAUDE.md trigger 1 test: same equation, only how
    it's reused)."""
    for hs in (0.5, 1.0, 2.0, 4.0):
        assert th.breaking_margin_depth_m(hs) == pytest.approx(1.3 * hs / 0.73)


def test_breaking_margin_depth_m_respects_custom_gamma_and_margin():
    assert th.breaking_margin_depth_m(2.0, gamma=0.5, margin=1.0) == pytest.approx(4.0)


def test_select_hourly_handoff_still_uses_breaking_margin_depth_m():
    """select_hourly_handoff()'s target depth (when it has stations to
    select among) must equal breaking_margin_depth_m()'s output for the
    same Hs — proving the internal call wasn't left inconsistent after the
    extraction."""
    hs = 1.7
    sel = select_hourly_handoff(hs, _SYNTHETIC_STATIONS)
    assert sel.handoff_depth_m == pytest.approx(th.breaking_margin_depth_m(hs))
