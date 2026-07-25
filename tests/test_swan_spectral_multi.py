"""Unit tests for services/swan_spectral.py's ``parse_specout_file_multi()``
— Phase 4A T4A.9, the multi-location SPECOUT parser.

Verified against the SWAN 41.51 User Manual, Appendix D "Formal description
of the 1D- and 2D-spectral file" (read on librewxr, `/tmp/swanuse.txt`): for
a 2D spectrum, each timestep repeats, for each declared location, one of
FACTOR+matrix / ZERO / NODATA — with NO per-location index marker (that only
exists for 1D spectra). These tests build synthetic files matching that
format and confirm the parser (a) reads multiple locations correctly,
(b) exposes station coordinates for coordinate-based alignment, and
(c) fails loudly rather than silently truncating on a count mismatch —
the exact defect found in the pre-existing single-location parser when
handed a multi-location file.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.services.swan_spectral import (
    SpecoutParseError,
    compute_total_m0,
    parse_specout_file,
    parse_specout_file_multi,
    parse_table_pt_partitions,
    watershed_partitions_to_component_format,
)

_HEADER = """SWAN   1                                Swan standard spectral file, version
$ test fixture
TIME                                    time-dependent data
     1                                  time coding option
LOCATIONS                               locations in x-y-space
     2                                  number of locations
    100.00        200.00
    150.00        250.00
AFREQ                                   absolute frequencies in Hz
     2                                  number of frequencies
    0.0500
    0.1000
NDIR                                    spectral nautical directions in degr
     2                                  number of directions
    30.0000
    60.0000
QUANT
     1                                  number of quantities in table
VaDens                                  variance densities in m2/Hz/degr
m2/Hz/degr                              unit
   -0.9900E+02                          exception value
"""


def _two_location_timestep(
    station0_factor: str = "FACTOR\n    1.0\n    1.0 2.0\n    3.0 4.0\n",
    station1_factor: str = "FACTOR\n    1.0\n    5.0 6.0\n    7.0 8.0\n",
) -> str:
    return "20260101.000000\n" + station0_factor + station1_factor


def test_parses_two_locations_one_timestep():
    text = _HEADER + _two_location_timestep()
    result = parse_specout_file_multi(text)

    assert result.station_lonlat == [(100.0, 200.0), (150.0, 250.0)]
    assert len(result.station_timesteps) == 2
    assert len(result.station_timesteps[0]) == 1
    assert len(result.station_timesteps[1]) == 1
    assert result.station_timesteps[0][0]["energy"] == [[1.0, 2.0], [3.0, 4.0]]
    assert result.station_timesteps[1][0]["energy"] == [[5.0, 6.0], [7.0, 8.0]]
    assert result.station_timesteps[0][0]["time"] == "2026-01-01T00:00:00Z"


def test_two_timesteps_stay_aligned_per_station():
    text = _HEADER + _two_location_timestep() + "20260101.010000\n" + (
        "FACTOR\n    1.0\n    9.0 9.0\n    9.0 9.0\n"
        "FACTOR\n    1.0\n    0.0 0.0\n    0.0 0.0\n"
    )
    result = parse_specout_file_multi(text)

    assert len(result.station_timesteps[0]) == 2
    assert len(result.station_timesteps[1]) == 2
    assert result.station_timesteps[0][1]["energy"] == [[9.0, 9.0], [9.0, 9.0]]
    assert result.station_timesteps[1][1]["energy"] == [[0.0, 0.0], [0.0, 0.0]]


def test_nodata_location_gets_zero_matrix_not_missing_entry():
    text = _HEADER + _two_location_timestep(station1_factor="NODATA\n")
    result = parse_specout_file_multi(text)

    assert len(result.station_timesteps[1]) == 1
    assert result.station_timesteps[1][0]["energy"] == [[0.0, 0.0], [0.0, 0.0]]


def test_zero_location_gets_zero_matrix():
    text = _HEADER + _two_location_timestep(station0_factor="ZERO\n")
    result = parse_specout_file_multi(text)

    assert result.station_timesteps[0][0]["energy"] == [[0.0, 0.0], [0.0, 0.0]]


def test_truncated_timestep_raises_not_silently_truncates():
    """The core defect this function exists to fix: a file that declares 2
    locations but only supplies 1 must raise, never silently return 1."""
    text = _HEADER + "20260101.000000\nFACTOR\n    1.0\n    1.0 2.0\n    3.0 4.0\n"
    with pytest.raises(SpecoutParseError):
        parse_specout_file_multi(text)


def test_unknown_block_keyword_raises():
    text = _HEADER + "20260101.000000\nFACTOR\n    1.0\n    1.0 2.0\n    3.0 4.0\nGARBAGE\n"
    with pytest.raises(SpecoutParseError):
        parse_specout_file_multi(text)


def test_empty_file_raises():
    with pytest.raises(SpecoutParseError):
        parse_specout_file_multi("")


def test_single_location_parser_unchanged_by_the_multi_location_addition():
    """Constraint from the round brief: the L2 deep-water reference path
    (genuinely single-location) must keep working byte-for-byte unchanged."""
    single_header = _HEADER.replace("     2                                  number of locations\n"
                                     "    100.00        200.00\n"
                                     "    150.00        250.00\n",
                                     "     1                                  number of locations\n"
                                     "    100.00        200.00\n")
    text = single_header + "20260101.000000\nFACTOR\n    1.0\n    1.0 2.0\n    3.0 4.0\n"
    result = parse_specout_file(text)

    assert len(result) == 1
    assert result[0]["energy"] == [[1.0, 2.0], [3.0, 4.0]]


# ---------------------------------------------------------------------------
# parse_table_pt_partitions() / compute_total_m0() — T4B.2
#
# Fixtures built directly from the documented column layout
# (docs/reference/swan-commands-extract.md "Spectral partitioning output
# (PT* quantities)"): each keyword expands to exactly 10 columns
# (<PREFIX>01..<PREFIX>10), PTDIR's exception value is -999, every other
# PT* quantity's is -9. This is real SWAN TABLE ASCII shape, not an
# approximation of it — the "% <header tokens>" line and space-separated
# data row are exactly what _parse_transect_table() (swan_runner.py) already
# parses in production for the non-PT* columns.
# ---------------------------------------------------------------------------

_PT_COLUMNS = (
    ["TIME", "XP", "YP"]
    + [f"HsPT{k:02d}" for k in range(1, 11)]
    + [f"TpPT{k:02d}" for k in range(1, 11)]
    + [f"DrPT{k:02d}" for k in range(1, 11)]
    + [f"DsPT{k:02d}" for k in range(1, 11)]
)


def _pt_header() -> str:
    return "%" + " ".join(_PT_COLUMNS) + "\n"


def _pt_row(
    time: str,
    xp: float,
    yp: float,
    hs: list[float],
    tp: list[float],
    dr: list[float],
    ds: list[float],
) -> str:
    assert len(hs) == len(tp) == len(dr) == len(ds) == 10
    values = [time, str(xp), str(yp)] + [str(v) for v in hs + tp + dr + ds]
    return " ".join(values) + "\n"


def test_parses_two_partitions_wind_sea_and_swell():
    # Partition 1 = wind sea, partition 2 = swell, partitions 3-10 absent.
    hs = [0.30, 1.80] + [-9.0] * 8
    tp = [6.0, 14.0] + [-9.0] * 8
    dr = [270.0, 184.0] + [-999.0] * 8
    ds = [25.0, 8.0] + [-9.0] * 8
    text = _pt_header() + _pt_row("20260101.000000", 100.0, 200.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 1
    row = rows[0]
    assert row["time"] == "2026-01-01T00:00:00Z"
    assert row["xp"] == 100.0
    assert row["yp"] == 200.0
    assert len(row["partitions"]) == 2

    p1, p2 = row["partitions"]
    assert p1["partition_index"] == 1
    assert p1["is_wind_sea"] is True
    assert p1["height"] == 0.30
    assert p1["period"] == 6.0
    assert p1["direction"] == 270.0
    assert p1["classification"] == "wind_swell"

    assert p2["partition_index"] == 2
    assert p2["is_wind_sea"] is False
    assert p2["height"] == 1.80
    assert p2["period"] == 14.0
    assert p2["direction"] == 184.0
    assert p2["classification"] == "groundswell"


def test_absent_partitions_are_omitted_not_zero():
    """LC-4B-4: absent partitions carry the exception value — not zero and
    not blank. A partition slot at its exception value must not appear in
    the output at all (never as a fabricated height-0 partition)."""
    hs = [0.5] + [-9.0] * 9
    tp = [10.0] + [-9.0] * 9
    dr = [100.0] + [-999.0] * 9
    ds = [12.0] + [-9.0] * 9
    text = _pt_header() + _pt_row("20260101.000000", 0.0, 0.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 1
    assert len(rows[0]["partitions"]) == 1
    assert all(p["height"] != 0.0 for p in rows[0]["partitions"])


def test_uniform_sentinel_assumption_would_fail():
    """LC-4B-4's exact failure mode: a parser assuming ONE sentinel value
    across all PT* quantities misreads absent partitions as real data.

    PTDIR's exception is -999, not -9. A direction reading of exactly -9.0
    is not the real DrPT exception value — a correct per-quantity parser
    must keep it as literal data. A parser that (wrongly) checked every PT*
    column against a single -9 sentinel would instead discard this real
    reading as if it were absent — exactly the "-9 m wave height is
    obviously wrong [...] but a partition direction of -9 deg is a
    plausible-looking northerly and would not [be caught]" scenario the
    plan calls out.
    """
    hs = [0.5, 1.0] + [-9.0] * 8
    tp = [10.0, 11.0] + [-9.0] * 8
    # Partition 2's direction is exactly the OTHER prefixes' sentinel value
    # (-9), not DrPT's own (-999). A uniform-sentinel parser would drop it;
    # the correct per-prefix parser must not.
    dr = [100.0, -9.0] + [-999.0] * 8
    ds = [12.0, 15.0] + [-9.0] * 8
    text = _pt_header() + _pt_row("20260101.000000", 0.0, 0.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 1
    assert len(rows[0]["partitions"]) == 2
    p2 = rows[0]["partitions"][1]
    assert p2["height"] == 1.0
    # -9.0 is real DrPT data here (not DrPT's exception value), so it must
    # survive the parse unchanged rather than being nulled out.
    assert p2["direction"] == -9.0

    # And the reverse: a genuinely absent partition's DrPT slot (-999) must
    # not leak through as a literal "-999 degree" direction on any
    # partition that IS returned.
    for p in rows[0]["partitions"]:
        assert p["direction"] != -999.0


def test_no_partitions_row_omitted():
    hs = [-9.0] * 10
    tp = [-9.0] * 10
    dr = [-999.0] * 10
    ds = [-9.0] * 10
    text = _pt_header() + _pt_row("20260101.000000", 0.0, 0.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert rows == []


def test_two_rows_two_stations_stay_independent():
    hs_a = [1.0] + [-9.0] * 9
    hs_b = [2.0, 0.5] + [-9.0] * 8
    tp = [10.0, 10.0] + [-9.0] * 8
    dr = [180.0, 190.0] + [-999.0] * 8
    ds = [10.0, 10.0] + [-9.0] * 8
    text = (
        _pt_header()
        + _pt_row("20260101.000000", 0.0, 0.0, hs_a, tp, dr, ds)
        + _pt_row("20260101.000000", 1.0, 1.0, hs_b, tp, dr, ds)
    )

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 2
    assert len(rows[0]["partitions"]) == 1
    assert len(rows[1]["partitions"]) == 2
    assert rows[0]["xp"] == 0.0
    assert rows[1]["xp"] == 1.0


def test_compute_total_m0_matches_manual_integration():
    freqs_hz = [0.05, 0.10]
    dirs_deg = [30.0, 60.0]
    energy = [[1.0, 2.0], [3.0, 4.0]]

    # Edge bins use single-sided spacing (matches decompose_spectrum()'s own
    # midpoint-spacing convention): df = 0.05 for both bins (2-bin edge
    # case), dd = 30.0 for both direction bins.
    df = 0.05
    dd = 30.0
    expected = (1.0 + 2.0 + 3.0 + 4.0) * df * dd

    assert compute_total_m0(freqs_hz, dirs_deg, energy) == pytest.approx(expected)


def test_compute_total_m0_zero_for_degenerate_spectrum():
    assert compute_total_m0([0.1], [30.0], [[1.0]]) == 0.0
    assert compute_total_m0([], [], []) == 0.0


# ---------------------------------------------------------------------------
# parse_table_pt_partitions() zero-based absence detection — T4B.2 (a)
#
# Real SWAN TABLE output signals an absent partition with HsPT0k == 0.00000,
# NOT the documented -9/-999 sentinel (measured directly against a real
# TABLE run: 1273 rows, 24 PT columns, zero sentinels observed — see
# MARINE-SERVICE-SEPARATION-PLAN.md T4B.2 pre-round verification). These
# tests use that real encoding; the sentinel-based fixtures above continue
# to exercise the belt-and-braces sentinel check.
# ---------------------------------------------------------------------------


def test_absent_partitions_encoded_as_zero_are_omitted():
    """The primary absence signal: HsPT0k written as an exact 0.00000 (the
    real SWAN encoding), not the documented sentinel."""
    hs = [0.62] + [0.0] * 9
    tp = [11.0] + [0.0] * 9
    dr = [190.0] + [0.0] * 9
    ds = [10.0] + [0.0] * 9
    text = _pt_header() + _pt_row("20260101.000000", 50.0, 60.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 1
    assert len(rows[0]["partitions"]) == 1
    assert rows[0]["partitions"][0]["height"] == 0.62


def test_one_real_partition_among_zero_encoded_absences_yields_exactly_one():
    """The wind-sea slot (partition 1) itself can be legitimately absent
    (zero) while a swell (partition 2) is present — SWAN numbering is
    preserved (partition_index stays 2, not renumbered to 1)."""
    hs = [0.0, 1.42] + [0.0] * 8
    tp = [0.0, 9.5] + [0.0] * 8
    dr = [0.0, 205.0] + [0.0] * 8
    ds = [0.0, 18.0] + [0.0] * 8
    text = _pt_header() + _pt_row("20260101.000000", 10.0, 20.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 1
    partitions = rows[0]["partitions"]
    assert len(partitions) == 1
    assert partitions[0]["partition_index"] == 2
    assert partitions[0]["is_wind_sea"] is False
    assert partitions[0]["height"] == 1.42


def test_zero_encoded_wind_sea_and_swell_both_present():
    """Deliverable (d): the wind-sea partition is index 1 and flagged,
    using the real zero encoding rather than the documented sentinel."""
    hs = [0.35, 2.10] + [0.0] * 8
    tp = [5.5, 13.0] + [0.0] * 8
    dr = [280.0, 200.0] + [0.0] * 8
    ds = [28.0, 9.0] + [0.0] * 8
    text = _pt_header() + _pt_row("20260101.000000", 0.0, 0.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 1
    partitions = rows[0]["partitions"]
    assert len(partitions) == 2
    assert partitions[0]["partition_index"] == 1
    assert partitions[0]["is_wind_sea"] is True
    assert partitions[1]["partition_index"] == 2
    assert partitions[1]["is_wind_sea"] is False


def test_no_partitions_row_omitted_zero_encoded():
    hs = [0.0] * 10
    tp = [0.0] * 10
    dr = [0.0] * 10
    ds = [0.0] * 10
    text = _pt_header() + _pt_row("20260101.000000", 0.0, 0.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert rows == []


def test_watershed_partitions_energy_closure_not_duplicated():
    """T4B.2: unlike decompose_spectrum()'s +/-4-bin windows (measured
    energy-closure ratio up to 227% across 65/65 multi-component real
    timesteps — see MARINE-SERVICE-SEPARATION-PLAN.md T4B.2 "EVIDENCE
    2026-07-25"), SWAN's own PT* partitions assign every energy-containing
    spectral bin to exactly one partition — no bin is double-counted.

    This builds a full 2-D spectrum with two well-separated single-cell
    energy peaks, uses compute_total_m0() to derive each peak's own exact
    m0 contribution (so the fixture is internally self-consistent, not
    hand-tuned), and asserts the parsed partitions' back-solved m0 sums to
    the spectrum's actual total — the property decompose_spectrum() was
    measured to violate (closure > 100%, sometimes > 200%).
    """
    freqs_hz = [0.05, 0.10, 0.15]
    dirs_deg = [0.0, 120.0, 240.0]

    energy_a = [[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    m0_a = compute_total_m0(freqs_hz, dirs_deg, energy_a)
    hs_a = 4.0 * m0_a ** 0.5

    energy_b = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
    m0_b = compute_total_m0(freqs_hz, dirs_deg, energy_b)
    hs_b = 4.0 * m0_b ** 0.5

    energy_total = [
        [a + b for a, b in zip(row_a, row_b)]
        for row_a, row_b in zip(energy_a, energy_b)
    ]
    total_m0 = compute_total_m0(freqs_hz, dirs_deg, energy_total)

    hs = [hs_a, hs_b] + [0.0] * 8
    tp = [8.0, 12.0] + [0.0] * 8
    dr = [10.0, 250.0] + [0.0] * 8
    ds = [15.0, 15.0] + [0.0] * 8
    text = _pt_header() + _pt_row("20260101.000000", 0.0, 0.0, hs, tp, dr, ds)

    rows = parse_table_pt_partitions(text)

    assert len(rows) == 1
    partitions = rows[0]["partitions"]
    assert len(partitions) == 2

    summed_m0 = sum(p["energy"] for p in partitions)
    assert summed_m0 == pytest.approx(total_m0, rel=1e-6)


# ---------------------------------------------------------------------------
# watershed_partitions_to_component_format() — T4B.2
# ---------------------------------------------------------------------------


def test_watershed_partitions_to_component_format_sorts_and_drops_wind_sea_flag():
    partitions = [
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
            "partition_index": 2,
            "is_wind_sea": False,
            "height": 2.9,
            "period": 12.0,
            "direction": 184.0,
            "spread": 20.0,
            "energy": (2.9 / 4.0) ** 2,
            "classification": "swell",
        },
    ]

    components = watershed_partitions_to_component_format(partitions)

    assert len(components) == 2
    # Re-sorted by descending height (decompose_spectrum()'s own contract) —
    # the input order above was wind-sea-first, not height-descending.
    assert components[0]["height"] == 2.9
    assert components[1]["height"] == 0.4
    for c in components:
        assert set(c.keys()) == {
            "height", "period", "direction", "energy", "frequencyRange", "classification",
        }
        assert c["frequencyRange"] == [0.0, 0.0]


def test_watershed_partitions_to_component_format_drops_incomplete_partitions():
    partitions = [
        {
            "partition_index": 1,
            "is_wind_sea": True,
            "height": 0.4,
            "period": None,
            "direction": None,
            "spread": None,
            "energy": 0.01,
            "classification": "wind_swell",
        },
    ]

    assert watershed_partitions_to_component_format(partitions) == []
