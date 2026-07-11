"""Unit tests for enrichment/solunar.py (T4.1, Marine-Surf-Fishing plan).

Covers per the T4.1 brief:
  - Known-date solunar computation at Wrightsville Beach, NC.
  - Standard vs. new/full period-duration modulation (major ±1.5h/±2h,
    minor ±1h/±1.5h — API-MANUAL.md §17 "Period duration modulation").
  - Moon-phase intensity mapping (new/full=1.0, quarter=0.7, else=0.5).
  - Multi-day calls produce distinct SolunarTimes objects.
  - High-latitude graceful handling when moonrise/moonset don't occur.

Ephemeris: these tests require de421.bsp to have been loaded via
wire_ephemeris_directory() first — same CLEARSKIES_EPHEMERIS_DIR-driven
skip-if-missing fixture pattern as tests/test_almanac_unit.py (duplicated
here rather than imported, since that module doesn't export it and this
file must not modify test_almanac_unit.py).

Test dates were chosen by running compute_moon_phases(2026, ..., month=7)
for Wrightsville Beach and picking real calendar dates that land in each
illumination band (see T4.1 dev-agent transcript for the reconnaissance
script); this avoids hardcoding illumination values that don't correspond
to an actual Skyfield-computed date.
"""

from __future__ import annotations

import os
from datetime import date, datetime

import pytest

# ---------------------------------------------------------------------------
# Ephemeris wiring fixture (mirrors tests/test_almanac_unit.py)
# ---------------------------------------------------------------------------

_DEFAULT_EPH_DIRS = [
    "/var/cache/weewx-clearskies/skyfield/",
    os.path.expanduser("~/.cache/weewx-clearskies/skyfield/"),
]


def _find_ephemeris_dir() -> str | None:
    """Find a directory containing de421.bsp, or None if not available."""
    env_dir = os.environ.get("CLEARSKIES_EPHEMERIS_DIR", "").strip()
    if env_dir:
        candidate = os.path.join(env_dir, "de421.bsp")
        if os.path.exists(candidate):
            return env_dir

    for d in _DEFAULT_EPH_DIRS:
        if os.path.exists(os.path.join(d, "de421.bsp")):
            return d
    return None


@pytest.fixture(scope="module", autouse=False)
def wired_ephemeris() -> None:
    """Wire the ephemeris for tests that require it.

    Skips if de421.bsp cannot be found. The module-level cache means the
    ephemeris is only loaded once per test session.
    """
    eph_dir = _find_ephemeris_dir()
    if eph_dir is None:
        pytest.skip(
            "de421.bsp not found; set CLEARSKIES_EPHEMERIS_DIR env var to a "
            "directory containing de421.bsp to run solunar compute tests"
        )
    from weewx_clearskies_api.services.almanac import (
        reset_cache,
        wire_ephemeris_directory,
    )
    reset_cache()
    wire_ephemeris_directory(eph_dir)


# ---------------------------------------------------------------------------
# Skip gate for skyfield import
# ---------------------------------------------------------------------------

_SKYFIELD_MISSING = False
try:
    import skyfield  # noqa: F401
except ImportError:
    _SKYFIELD_MISSING = True

skip_if_no_skyfield = pytest.mark.skipif(
    _SKYFIELD_MISSING, reason="skyfield not installed; skip solunar compute tests"
)

# ---------------------------------------------------------------------------
# Shared test fixtures — locations
# ---------------------------------------------------------------------------

_WRIGHTSVILLE_LAT = 34.21
_WRIGHTSVILLE_LON = -77.79
_WRIGHTSVILLE_TZ = "America/New_York"

_BARROW_LAT = 71.29
_BARROW_LON = -156.79
_BARROW_TZ = "America/Anchorage"

_VALID_PHASE_NAMES = {
    "new", "waxing_crescent", "first_quarter", "waxing_gibbous",
    "full", "waning_gibbous", "last_quarter", "waning_crescent",
}


def _period_duration_minutes(period: dict[str, str]) -> float:
    start = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
    return (end - start).total_seconds() / 60.0


@skip_if_no_skyfield
@pytest.mark.usefixtures("wired_ephemeris")
class TestKnownDate:
    """Solunar computation for a known date at a known location."""

    def test_wrightsville_beach_2026_07_09(self) -> None:
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(
            date(2026, 7, 9), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ
        )

        assert result.date == "2026-07-09"
        assert isinstance(result.moonTransit, str) and result.moonTransit
        assert isinstance(result.moonUnderfoot, str) and result.moonUnderfoot

        assert len(result.majorPeriods) == 2
        for period in result.majorPeriods:
            assert period["start"] < period["end"]

        assert 1 <= len(result.minorPeriods) <= 2

        assert result.moonPhase in _VALID_PHASE_NAMES
        assert 0.0 <= result.moonIllumination <= 1.0
        assert result.intensity in (0.5, 0.7, 1.0)


@skip_if_no_skyfield
@pytest.mark.usefixtures("wired_ephemeris")
class TestPeriodDurations:
    """Period duration modulation: standard vs. new/full moon windows."""

    def test_standard_duration_at_quarter_moon(self) -> None:
        """2026-07-21 at Wrightsville Beach is first-quarter (illum ~0.55):
        standard windows apply (major +/-1.5h = 180min, minor +/-1h = 120min).
        """
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(
            date(2026, 7, 21), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ
        )
        assert 0.45 <= result.moonIllumination <= 0.55, (
            f"expected quarter-moon illumination, got {result.moonIllumination}"
        )

        for period in result.majorPeriods:
            assert abs(_period_duration_minutes(period) - 180.0) <= 5
        for period in result.minorPeriods:
            assert abs(_period_duration_minutes(period) - 120.0) <= 5

    def test_widened_duration_near_full_moon(self) -> None:
        """2026-07-29 at Wrightsville Beach is full (illum ~0.998):
        widened windows apply (major +/-2h = 240min, minor +/-1.5h = 180min).
        """
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(
            date(2026, 7, 29), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ
        )
        assert result.moonIllumination > 0.95, (
            f"expected near-full illumination, got {result.moonIllumination}"
        )

        for period in result.majorPeriods:
            assert abs(_period_duration_minutes(period) - 240.0) <= 5
        for period in result.minorPeriods:
            assert abs(_period_duration_minutes(period) - 180.0) <= 5


@skip_if_no_skyfield
@pytest.mark.usefixtures("wired_ephemeris")
class TestIntensityMapping:
    """Moon-phase intensity: new/full=1.0, quarter=0.7, else=0.5."""

    def test_near_new_moon_intensity_is_1(self) -> None:
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(
            date(2026, 7, 14), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ
        )
        assert result.moonIllumination < 0.05
        assert result.intensity == 1.0

    def test_near_full_moon_intensity_is_1(self) -> None:
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(
            date(2026, 7, 29), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ
        )
        assert result.moonIllumination > 0.95
        assert result.intensity == 1.0

    def test_quarter_moon_intensity_is_0_7(self) -> None:
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(
            date(2026, 7, 21), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ
        )
        assert 0.45 <= result.moonIllumination <= 0.55
        assert result.intensity == 0.7

    def test_gibbous_intensity_is_0_5(self) -> None:
        """2026-07-24 at Wrightsville Beach is waxing-gibbous (illum ~0.82):
        neither new/full nor quarter band -> default intensity 0.5.
        """
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(
            date(2026, 7, 24), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ
        )
        assert not (result.moonIllumination < 0.05 or result.moonIllumination > 0.95)
        assert not (0.45 <= result.moonIllumination <= 0.55)
        assert result.intensity == 0.5


@skip_if_no_skyfield
@pytest.mark.usefixtures("wired_ephemeris")
class TestMultiDay:
    """Calling compute_solunar for consecutive dates yields distinct results."""

    def test_three_consecutive_days_are_distinct(self) -> None:
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        dates = [date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 11)]
        results = [
            compute_solunar(d, _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _WRIGHTSVILLE_TZ)
            for d in dates
        ]

        assert len(results) == 3
        assert [r.date for r in results] == ["2026-07-09", "2026-07-10", "2026-07-11"]

        # Transit times must differ day to day (moon transits ~50 min later
        # each day) -- confirms these are independently computed, not cached
        # copies of a single result.
        transit_times = {r.moonTransit for r in results}
        assert len(transit_times) == 3


@skip_if_no_skyfield
@pytest.mark.usefixtures("wired_ephemeris")
class TestHighLatitudeGraceful:
    """High-latitude locations may lack moonrise/moonset on a given day."""

    def test_barrow_alaska_does_not_crash(self) -> None:
        from weewx_clearskies_api.enrichment.solunar import compute_solunar

        result = compute_solunar(date(2026, 7, 9), _BARROW_LAT, _BARROW_LON, _BARROW_TZ)

        assert result.date == "2026-07-09"
        # Moon transit/underfoot are geometric (not horizon-gated) and must
        # always be present, even when rise/set are absent.
        assert result.moonTransit
        assert result.moonUnderfoot
        assert len(result.majorPeriods) == 2

        # minorPeriods may be shorter than 2 (or empty) when moonrise and/or
        # moonset don't occur that day -- must not raise. The count should
        # match exactly how many of {moonrise, moonset} are non-None.
        assert len(result.minorPeriods) <= 2
        expected_minor_count = sum(
            1 for v in (result.moonrise, result.moonset) if v is not None
        )
        assert len(result.minorPeriods) == expected_minor_count
