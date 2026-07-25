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
    parse_specout_file,
    parse_specout_file_multi,
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
