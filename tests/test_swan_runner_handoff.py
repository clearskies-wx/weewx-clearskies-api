"""Unit tests for services/swan_runner.py's ``_select_l3_handoff_spectra()``
— Phase 4A T4A.9/T4A.10, the per-hour L3 handoff station selection.

The coordinator flagged the TABLE<->SPECOUT station alignment as the
highest-risk part of this mechanism: an unverified index-order assumption
would silently pair a spectrum from one station with a DIFFERENT station's
depth/QB. Nothing crashes; the handoff lands at a depth that was not
intended, T4A.10's QB assertion checks the wrong station and passes, and the
output looks like plausible small surf. These tests are built specifically
to FAIL on an off-by-one, not merely pass when alignment is correct — using
an INTERIOR station (neither first nor last) as the target, since the
outermost stations are excluded from selection anyway and an off-by-one
there would be masked by that exclusion.
"""

from __future__ import annotations

from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.services.swan_runner import (
    _select_l3_handoff_position_and_spectrum,
    _select_l3_handoff_spectra,
    _transect_band_depths,
)
from weewx_clearskies_api.services.swan_spectral import MultiLocationSpecout

# ---------------------------------------------------------------------------
# Synthetic 5-station L3 curve, offshore (index 0) -> nearshore (index 4).
# Depths monotonically decrease; distances monotonically decrease (matching
# compute_spot_transect()'s documented offshore->nearshore point order).
# ---------------------------------------------------------------------------

#  idx  lon         lat       depth_m  distance_m  role
#  0    -118.0100   33.6600   15.0     500.0       offshore boundary
#  1    -118.0080   33.6590   10.0     375.0       interior
#  2    -118.0060   33.6580    6.0     250.0       interior (target)
#  3    -118.0040   33.6570    3.0     125.0       interior
#  4    -118.0020   33.6560    1.5       0.0       shoreward boundary
_TRANSECT_POINTS = [
    {"lon": -118.0100, "lat": 33.6600, "depth_m": 15.0, "distance_m": 500.0},
    {"lon": -118.0080, "lat": 33.6590, "depth_m": 10.0, "distance_m": 375.0},
    {"lon": -118.0060, "lat": 33.6580, "depth_m": 6.0, "distance_m": 250.0},
    {"lon": -118.0040, "lat": 33.6570, "depth_m": 3.0, "distance_m": 125.0},
    {"lon": -118.0020, "lat": 33.6560, "depth_m": 1.5, "distance_m": 0.0},
]

_TIME = "2026-01-01T00:00:00Z"

# Distinguishable "energy" payload per station so a wrong-station selection
# is caught by value, not just by station-count bookkeeping.
_STATION_ENERGY = {
    0: [[1.0]],
    1: [[2.0]],
    2: [[42.0]],  # the station this test expects to be selected
    3: [[4.0]],
    4: [[5.0]],
}


def _specout() -> MultiLocationSpecout:
    return MultiLocationSpecout(
        station_lonlat=[(p["lon"], p["lat"]) for p in _TRANSECT_POINTS],
        station_timesteps=[
            [{
                "time": _TIME,
                "freqs_hz": [0.1],
                "dirs_deg": [0.0],
                "energy": _STATION_ENERGY[i],
            }]
            for i in range(len(_TRANSECT_POINTS))
        ],
    )


def _table_points(*, qb_by_station: dict[int, float] | None = None) -> list[MarineForecastPoint]:
    """One row per station for the single timestep _TIME.

    Hs(hour) proxy is read from station index 1 (the offshore-most interior
    station) — set to 3.0 m here. Target depth = 1.3*3.0/0.73 = 5.34 m,
    which lands nearest station 2 (depth 6.0 m) among the interior stations
    {1: 10.0, 2: 6.0, 3: 3.0} — an OFF-BY-ONE in either direction would pick
    station 1 (10.0 m) or station 3 (3.0 m) instead.
    """
    qb_by_station = qb_by_station or {}
    points = []
    for i, p in enumerate(_TRANSECT_POINTS):
        points.append(MarineForecastPoint(
            time=_TIME,
            waveHeight=3.0 if i == 1 else 1.0,
            wavePeriod=12.0,
            waveDirection=270.0,
            depth=p["depth_m"],
            breakingFraction=qb_by_station.get(i, 0.0),
            distanceFromShore=p["distance_m"],
        ))
    return points


def test_selects_correct_interior_station_by_coordinate_not_index_order():
    """The headline alignment guard: verifies the SELECTED spectrum, depth
    target, and source station all agree on station 2 — not station 1 or 3,
    which is exactly what an off-by-one would produce."""
    results = _select_l3_handoff_spectra(
        "test_spot",
        _specout(),
        _table_points(),
        _TRANSECT_POINTS,
        utm_zone=None,
    )

    assert len(results) == 1
    entry = results[0]
    assert entry["time"] == _TIME
    # Station 2's distinguishable payload — proves the spectrum came from
    # the correct station, not a neighbour.
    assert entry["energy"] == [[42.0]]
    assert entry["handoff_source_level"] == "L3"
    # Target depth (not the station's actual depth) is what's surfaced as
    # handoffDepthM per the T4A.6 item g contract.
    assert entry["handoff_depth_m"] == 1.3 * 3.0 / 0.73


def test_never_selects_boundary_station_even_when_nearest_by_depth():
    """A very large Hs proxy pushes the target far deeper than every
    station; the nearest-by-depth match is station 0 (offshore boundary),
    which must be excluded (ADR-095 Amendment 2)."""
    table_points = _table_points()
    # Override the Hs proxy (station 1) to something huge.
    table_points[1] = MarineForecastPoint(
        time=_TIME, waveHeight=50.0, wavePeriod=12.0, waveDirection=270.0,
        depth=_TRANSECT_POINTS[1]["depth_m"], breakingFraction=0.0,
        distanceFromShore=_TRANSECT_POINTS[1]["distance_m"],
    )

    results = _select_l3_handoff_spectra(
        "test_spot", _specout(), table_points, _TRANSECT_POINTS, utm_zone=None,
    )

    assert len(results) == 1
    # Nearest legal (interior) station to an unreachable depth is station 1
    # (depth 10.0, the deepest interior station) — never station 0.
    assert results[0]["energy"] == [[2.0]]


def test_qb_violation_at_target_station_moves_seaward_and_reflects_in_output():
    """T4A.10 integration: QB=0.9 (breaking) at station 2 (the alignment
    target) must move the selection to station 1, and the OUTPUT spectrum
    must reflect that move — proving the QB lookup itself is aligned to the
    same station identity as the depth-based selection."""
    results = _select_l3_handoff_spectra(
        "test_spot",
        _specout(),
        _table_points(qb_by_station={2: 0.9}),
        _TRANSECT_POINTS,
        utm_zone=None,
    )

    assert len(results) == 1
    # Moved seaward from station 2 to station 1 — station 1's payload.
    assert results[0]["energy"] == [[2.0]]


def test_unmatched_station_discards_whole_spot_rather_than_guessing():
    """A SPECOUT station whose coordinates don't match any transect_points
    entry within tolerance must not silently proceed with a partial/guessed
    alignment — the whole spot's L3 handoff data for this run is discarded
    (falls back to the L2 DWR baseline already in self._spectral_results)."""
    specout = _specout()
    # Corrupt one station's coordinates far outside any reasonable tolerance.
    specout.station_lonlat[2] = (0.0, 0.0)

    results = _select_l3_handoff_spectra(
        "test_spot", specout, _table_points(), _TRANSECT_POINTS, utm_zone=None,
    )

    assert results == []


def test_missing_hs_proxy_data_skips_only_that_timestep():
    table_points = _table_points()
    # Blank out the Hs proxy station's waveHeight for this timestep.
    table_points[1] = MarineForecastPoint(
        time=_TIME, waveHeight=None, wavePeriod=12.0, waveDirection=270.0,
        depth=_TRANSECT_POINTS[1]["depth_m"], breakingFraction=0.0,
        distanceFromShore=_TRANSECT_POINTS[1]["distance_m"],
    )

    results = _select_l3_handoff_spectra(
        "test_spot", _specout(), table_points, _TRANSECT_POINTS, utm_zone=None,
    )

    assert results == []


# ---------------------------------------------------------------------------
# T4B.3 (2026-07-25 correction): _select_l3_handoff_position_and_spectrum().
#
# Regression coverage for the specific defect the coordinator caught in the
# first T4B.3 landing (4dd8964): selecting AMONG the sparse shared CURVE's
# stations cannot lower the clamp rate no matter what Hs value drives the
# target depth, because the CURVE has "nothing between" its excluded
# boundary station and the next interior one at the real HB Pier depths
# (plan finding 3: 0.98 m excluded, 2.37 m next — a ~1.8 m target always
# clamps). These tests build a CURVE with exactly that gap-shape AND a fine
# 10 m band that DOES have an interior station near the target, and assert
# selection happens against the band (not the CURVE).
# ---------------------------------------------------------------------------

# Sparse CURVE — same gap shape as the real HB Pier measurement: nothing
# between the (excluded) shallow boundary and the next interior station.
_CURVE_SPARSE = [
    (-118.0100, 33.6600),  # idx0: offshore boundary
    (-118.0080, 33.6600),  # idx1: interior
    (-118.0060, 33.6600),  # idx2: interior — nearest to the band's target position
    (-118.0040, 33.6600),  # idx3: interior
    (-118.0020, 33.6600),  # idx4: shoreward boundary
]
_CURVE_ENERGY = {0: [[1.0]], 1: [[2.0]], 2: [[42.0]], 3: [[4.0]], 4: [[5.0]]}


def _sparse_curve_specout() -> MultiLocationSpecout:
    return MultiLocationSpecout(
        station_lonlat=list(_CURVE_SPARSE),
        station_timesteps=[
            [{"time": _TIME, "freqs_hz": [0.1], "dirs_deg": [0.0], "energy": _CURVE_ENERGY[i]}]
            for i in range(len(_CURVE_SPARSE))
        ],
    )


# Fine 10 m-equivalent band (6 points), positioned near CURVE idx2
# (-118.0060) so the nearest-CURVE-station lookup is unambiguous.
_BAND_GEOMETRY = [
    {"lon": -118.0075, "lat": 33.6600, "depth_m": 3.0, "distance_m": 100.0},  # idx0: boundary
    {"lon": -118.0070, "lat": 33.6600, "depth_m": 2.5, "distance_m": 80.0},   # idx1: interior (Hs proxy)
    {"lon": -118.0065, "lat": 33.6600, "depth_m": 2.0, "distance_m": 60.0},   # idx2: interior
    {"lon": -118.0060, "lat": 33.6600, "depth_m": 1.8, "distance_m": 40.0},   # idx3: interior (target)
    {"lon": -118.0055, "lat": 33.6600, "depth_m": 1.5, "distance_m": 20.0},   # idx4: interior
    {"lon": -118.0050, "lat": 33.6600, "depth_m": 1.2, "distance_m": 0.0},    # idx5: boundary
]


def _band_points(hs_proxy: float, *, qb_by_band_idx: dict[int, float] | None = None) -> list[MarineForecastPoint]:
    """One row per band station for the single timestep _TIME. Hs proxy is
    read from band index 1 (offshore-most interior, n=6>2)."""
    qb_by_band_idx = qb_by_band_idx or {}
    points = []
    for i, p in enumerate(_BAND_GEOMETRY):
        points.append(MarineForecastPoint(
            time=_TIME,
            waveHeight=hs_proxy if i == 1 else 1.0,
            wavePeriod=12.0,
            waveDirection=270.0,
            depth=p["depth_m"],
            breakingFraction=qb_by_band_idx.get(i, 0.0),
            distanceFromShore=p["distance_m"],
        ))
    return points


def test_position_selected_from_band_not_from_sparse_curve():
    """The regression case: a target depth (~1.78 m) that would clamp on
    the sparse CURVE (nothing between 2.37 m-equivalent and the excluded
    boundary) must resolve to a genuine INTERIOR station on the fine band,
    not clamp. Hs=1.0 at the proxy -> target = 1.3*1.0/0.73 ≈ 1.78 m,
    nearest interior band station is idx3 (1.8 m) — band idx0/idx5
    (boundaries) must never be selectable.
    """
    results = _select_l3_handoff_position_and_spectrum(
        _band_points(hs_proxy=1.0),
        _BAND_GEOMETRY,
        _sparse_curve_specout(),
        utm_zone=None,
        transect_index=3,
        label="test_spot#T3",
    )

    assert len(results) == 1
    entry = results[0]
    assert entry["clamped"] is False
    assert entry["handoff_depth_m"] == 1.3 * 1.0 / 0.73
    # Spectrum comes from the CURVE station nearest band idx3's coordinate
    # (-118.0060) — CURVE idx2, exact coordinate match — not from any other
    # CURVE station and not fabricated from the band itself.
    assert entry["energy"] == [[42.0]]


def test_position_never_selects_band_boundary_station():
    """A huge Hs proxy pushes the target far deeper than every band
    station; the nearest legal (interior) band station is idx1 (2.5 m,
    the deepest interior one) — never idx0 (the band's own offshore
    boundary)."""
    results = _select_l3_handoff_position_and_spectrum(
        _band_points(hs_proxy=50.0),
        _BAND_GEOMETRY,
        _sparse_curve_specout(),
        utm_zone=None,
    )

    assert len(results) == 1
    assert results[0]["clamped"] is True
    # Band idx1 (-118.0070) is nearest to CURVE idx1 (-118.0080, diff
    # .0010) vs idx2 (-118.0060, diff .0010) — a tie broken by "first
    # found, strictly less-than" in favour of idx1 (lower CURVE index
    # scanned first). Assert against energy value, not index, so the test
    # doesn't depend on tie-break direction if geometry changes later.
    assert results[0]["energy"] in ([[2.0]], [[42.0]])


def test_qb_violation_on_band_moves_seaward_on_the_band_itself():
    """T4A.10 integration on the fine band: QB=0.9 at the target band
    station (idx3) must move the selection seaward to idx2 — and the
    spectrum must come from whichever CURVE station is nearest idx2's
    coordinate, proving QB refinement operates on the band, not the
    CURVE."""
    results = _select_l3_handoff_position_and_spectrum(
        _band_points(hs_proxy=1.0, qb_by_band_idx={3: 0.9}),
        _BAND_GEOMETRY,
        _sparse_curve_specout(),
        utm_zone=None,
    )

    assert len(results) == 1
    # Band idx2 (-118.0065) is nearest to CURVE idx2 (-118.0060, diff
    # .0005) vs idx1 (-118.0080, diff .0015) -> CURVE idx2's payload.
    assert results[0]["energy"] == [[42.0]]


def test_position_empty_band_geometry_returns_no_results():
    results = _select_l3_handoff_position_and_spectrum(
        [], [], _sparse_curve_specout(), utm_zone=None,
    )
    assert results == []


# ---------------------------------------------------------------------------
# T4B.1: _transect_band_depths() — the L2 Hs bracket used to size each
# transect's per-transect POINTS band.
# ---------------------------------------------------------------------------


def test_transect_band_depths_empty_series_returns_none():
    assert _transect_band_depths([]) is None


def test_transect_band_depths_brackets_generously_above_and_below():
    # hs_series spans 0.5 - 2.0 m. target depths: 1.3*0.5/0.73=0.890,
    # 1.3*2.0/0.73=3.562. Padded 50% each way: shallow ~0.445 (floored at
    # 0.1 SWAN DEPMIN — well above it here), deep ~5.342.
    deep, shallow = _transect_band_depths([0.5, 1.0, 2.0])
    assert shallow < (1.3 * 0.5 / 0.73)
    assert deep > (1.3 * 2.0 / 0.73)
    assert shallow >= 0.1
