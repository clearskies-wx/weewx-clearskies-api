"""Unit tests for almanac pure-compute logic (no DB, no network).

Covers per the 3a-2 brief:
  - Phase-name 8-bin classification via _phase_name_from_angle().
  - Sun-times year loop: 2024 (leap) → 366 entries, 2023 (non-leap) → 365.
  - Moon-phases month vs full-year switching.
  - Polar-edge cases: lat 89 on Jun 21 = polar day; lat 89 on Dec 21 = polar night.
  - Pydantic param models: extra="forbid"; out-of-range year; bad month; bad date.
  - USNO reference sunrise/sunset check (±1 min tolerance).

ADR references: ADR-014 (almanac/skyfield), ADR-020 (UTC-Z on wire).

USNO reference dates used for sunrise/sunset assertions:
  - 2024-06-21 at lat=42.375, lon=-72.519 (Belchertown MA, UTC-4 in summer)
    USNO: rise 05:12 EDT = 09:12Z, set 20:25 EDT = 00:25Z next day
    Source: https://aa.usno.navy.mil/data/RS_OneYear (2024, lat=42.375, lon=-72.519)
    Tolerance: ±1 minute per brief.
  - 2024-12-21 at lat=42.375, lon=-72.519 (UTC-5 in winter)
    USNO: rise 07:12 EST = 12:12Z, set 16:10 EST = 21:10Z
    Source: USNO RS_OneYear 2024-12-21.

Ephemeris: tests that call compute_almanac / compute_sun_times_year /
compute_moon_phases require the de421.bsp ephemeris to have been loaded via
wire_ephemeris_directory() first. These tests use the CLEARSKIES_EPHEMERIS_DIR
environment variable (set in weather-dev CI) to locate the ephemeris directory.
If the env var is unset AND the default cache dir is not readable, the tests
skip gracefully.
"""

from __future__ import annotations

import datetime
import os

import pytest

# ---------------------------------------------------------------------------
# Ephemeris wiring fixture
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
            "directory containing de421.bsp to run almanac compute tests"
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
    _SKYFIELD_MISSING, reason="skyfield not installed; skip almanac compute tests"
)


# ---------------------------------------------------------------------------
# Phase-name 8-bin classification
# ---------------------------------------------------------------------------


class TestMoonPhaseNameClassification:
    """_phase_name_from_angle bins are correct per the brief's 45-degree bin scheme.

    The implementation uses ecliptic longitude angle (0=new moon, 180=full).
    """

    def _classify(self, angle_degrees: float) -> str:
        from weewx_clearskies_api.services.almanac import _phase_name_from_angle  # type: ignore[attr-defined]
        return _phase_name_from_angle(angle_degrees)

    def test_angle_0_is_new_moon(self) -> None:
        """Angle 0° → 'new' (new moon)."""
        assert self._classify(0.0) == "new"

    def test_angle_11_is_new_moon(self) -> None:
        """Angle 11° → 'new' (within first 22.5° bin)."""
        assert self._classify(11.0) == "new"

    def test_angle_22_is_new_moon(self) -> None:
        """Angle 22° (just under 22.5) → 'new'."""
        assert self._classify(22.4) == "new"

    def test_angle_45_is_waxing_crescent(self) -> None:
        """Angle 45° (midpoint of 22.5–67.5 bin) → 'waxing-crescent'."""
        assert self._classify(45.0) == "waxing-crescent"

    def test_angle_90_is_first_quarter(self) -> None:
        """Angle 90° (midpoint of 67.5–112.5 bin) → 'first-quarter'."""
        assert self._classify(90.0) == "first-quarter"

    def test_angle_135_is_waxing_gibbous(self) -> None:
        """Angle 135° → 'waxing-gibbous'."""
        assert self._classify(135.0) == "waxing-gibbous"

    def test_angle_180_is_full_moon(self) -> None:
        """Angle 180° → 'full'."""
        assert self._classify(180.0) == "full"

    def test_angle_225_is_waning_gibbous(self) -> None:
        """Angle 225° → 'waning-gibbous'."""
        assert self._classify(225.0) == "waning-gibbous"

    def test_angle_270_is_last_quarter(self) -> None:
        """Angle 270° → 'last-quarter'."""
        assert self._classify(270.0) == "last-quarter"

    def test_angle_315_is_waning_crescent(self) -> None:
        """Angle 315° → 'waning-crescent'."""
        assert self._classify(315.0) == "waning-crescent"

    def test_angle_350_is_new_moon_wrapping(self) -> None:
        """Angle 350° (within 337.5–360 new-moon tail) → 'new'."""
        assert self._classify(350.0) == "new"

    def test_angle_360_is_new_moon(self) -> None:
        """Angle 360° (= 0° mod 360, new moon) → 'new'."""
        assert self._classify(360.0) == "new"

    def test_valid_phase_names_are_openapi_enum_values(self) -> None:
        """All possible _phase_name_from_angle outputs are OpenAPI phaseName enum values."""
        valid_names = {
            "new", "waxing-crescent", "first-quarter", "waxing-gibbous",
            "full", "waning-gibbous", "last-quarter", "waning-crescent",
        }
        sample_angles = [0, 22.5, 45, 67.5, 90, 112.5, 135, 157.5,
                         180, 202.5, 225, 247.5, 270, 292.5, 315, 337.5, 359.9]
        from weewx_clearskies_api.services.almanac import _phase_name_from_angle  # type: ignore[attr-defined]
        for angle in sample_angles:
            name = _phase_name_from_angle(angle)
            assert name in valid_names, (
                f"_phase_name_from_angle({angle}) = {name!r} is not a valid "
                f"OpenAPI phaseName enum value. Valid: {valid_names}"
            )


# ---------------------------------------------------------------------------
# Pydantic param model validation
# ---------------------------------------------------------------------------


class TestAlmanacParamModels:
    """Pydantic param models reject unknown keys and out-of-range values."""

    def test_almanac_params_accept_valid_date(self) -> None:
        """AlmanacQueryParams accepts a valid YYYY-MM-DD date string."""
        from weewx_clearskies_api.models.params import AlmanacQueryParams
        params = AlmanacQueryParams.model_validate({"date": "2024-06-21"})
        assert str(params.date) == "2024-06-21"

    def test_almanac_params_accept_no_date(self) -> None:
        """AlmanacQueryParams accepts empty dict (date defaults to None)."""
        from weewx_clearskies_api.models.params import AlmanacQueryParams
        params = AlmanacQueryParams.model_validate({})
        assert params.date is None

    def test_almanac_params_reject_malformed_date_string(self) -> None:
        """AlmanacQueryParams rejects non-date string with ValidationError."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import AlmanacQueryParams
        with pytest.raises(ValidationError):
            AlmanacQueryParams.model_validate({"date": "not-a-date"})

    def test_almanac_params_reject_unknown_key(self) -> None:
        """AlmanacQueryParams rejects unknown query key (extra='forbid')."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import AlmanacQueryParams
        with pytest.raises(ValidationError):
            AlmanacQueryParams.model_validate({"date": "2024-01-01", "unknown_key": "x"})

    def test_sun_times_params_accept_valid_year(self) -> None:
        """SunTimesQueryParams accepts a year >= 1900."""
        from weewx_clearskies_api.models.params import SunTimesQueryParams
        params = SunTimesQueryParams.model_validate({"year": 2024})
        assert params.year == 2024

    def test_sun_times_params_accept_no_year(self) -> None:
        """SunTimesQueryParams accepts empty dict (year defaults to None/current)."""
        from weewx_clearskies_api.models.params import SunTimesQueryParams
        params = SunTimesQueryParams.model_validate({})
        assert params.year is None

    def test_sun_times_params_reject_year_below_1900(self) -> None:
        """SunTimesQueryParams rejects year < 1900."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import SunTimesQueryParams
        with pytest.raises(ValidationError):
            SunTimesQueryParams.model_validate({"year": 1899})

    def test_sun_times_params_reject_unknown_key(self) -> None:
        """SunTimesQueryParams rejects unknown query key (extra='forbid')."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import SunTimesQueryParams
        with pytest.raises(ValidationError):
            SunTimesQueryParams.model_validate({"year": 2024, "bogus": "yes"})

    def test_moon_phases_params_accept_year_only(self) -> None:
        """MoonPhasesQueryParams accepts year without month (full-year mode)."""
        from weewx_clearskies_api.models.params import MoonPhasesQueryParams
        params = MoonPhasesQueryParams.model_validate({"year": 2024})
        assert params.year == 2024
        assert params.month is None

    def test_moon_phases_params_accept_year_and_month(self) -> None:
        """MoonPhasesQueryParams accepts both year and month."""
        from weewx_clearskies_api.models.params import MoonPhasesQueryParams
        params = MoonPhasesQueryParams.model_validate({"year": 2024, "month": 6})
        assert params.year == 2024
        assert params.month == 6

    def test_moon_phases_params_reject_month_zero(self) -> None:
        """MoonPhasesQueryParams rejects month=0 (< 1)."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import MoonPhasesQueryParams
        with pytest.raises(ValidationError):
            MoonPhasesQueryParams.model_validate({"year": 2024, "month": 0})

    def test_moon_phases_params_reject_month_13(self) -> None:
        """MoonPhasesQueryParams rejects month=13 (> 12)."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import MoonPhasesQueryParams
        with pytest.raises(ValidationError):
            MoonPhasesQueryParams.model_validate({"year": 2024, "month": 13})

    def test_moon_phases_params_reject_year_below_1900(self) -> None:
        """MoonPhasesQueryParams rejects year < 1900."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import MoonPhasesQueryParams
        with pytest.raises(ValidationError):
            MoonPhasesQueryParams.model_validate({"year": 1850, "month": 6})

    def test_moon_phases_params_reject_unknown_key(self) -> None:
        """MoonPhasesQueryParams rejects unknown query key (extra='forbid')."""
        from pydantic import ValidationError
        from weewx_clearskies_api.models.params import MoonPhasesQueryParams
        with pytest.raises(ValidationError):
            MoonPhasesQueryParams.model_validate({"year": 2024, "secret": "hax"})


# ---------------------------------------------------------------------------
# Sun-times year loop: entry count
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("wired_ephemeris")
class TestSunTimesYearLoop:
    """Year-loop returns correct entry count for leap and non-leap years."""

    @skip_if_no_skyfield
    def test_sun_times_2024_leap_year_returns_366_entries(self) -> None:
        """compute_sun_times_year(2024) returns 366 day entries (2024 is a leap year)."""
        from weewx_clearskies_api.services.almanac import compute_sun_times_year
        days = compute_sun_times_year(
            year=2024, lat=42.375, lon=-72.519, alt_m=0.0
        )
        assert len(days) == 366, (
            f"2024 is a leap year — expected 366 entries, got {len(days)}"
        )

    @skip_if_no_skyfield
    def test_sun_times_2023_non_leap_year_returns_365_entries(self) -> None:
        """compute_sun_times_year(2023) returns 365 day entries (non-leap year)."""
        from weewx_clearskies_api.services.almanac import compute_sun_times_year
        days = compute_sun_times_year(
            year=2023, lat=42.375, lon=-72.519, alt_m=0.0
        )
        assert len(days) == 365, (
            f"2023 is a non-leap year — expected 365 entries, got {len(days)}"
        )

    @skip_if_no_skyfield
    def test_sun_times_first_entry_is_jan_1(self) -> None:
        """compute_sun_times_year first entry's date_str is Jan 1 of the year."""
        from weewx_clearskies_api.services.almanac import compute_sun_times_year
        days = compute_sun_times_year(
            year=2024, lat=42.375, lon=-72.519, alt_m=0.0
        )
        assert days[0].date_str == "2024-01-01", (
            f"First entry must be Jan 1, got {days[0].date_str!r}"
        )

    @skip_if_no_skyfield
    def test_sun_times_last_entry_is_dec_31(self) -> None:
        """compute_sun_times_year last entry's date_str is Dec 31 of the year."""
        from weewx_clearskies_api.services.almanac import compute_sun_times_year
        days = compute_sun_times_year(
            year=2024, lat=42.375, lon=-72.519, alt_m=0.0
        )
        assert days[-1].date_str == "2024-12-31", (
            f"Last entry must be Dec 31, got {days[-1].date_str!r}"
        )


# ---------------------------------------------------------------------------
# Moon-phases month vs full-year switching
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("wired_ephemeris")
class TestMoonPhasesMonthVsYear:
    """Moon-phase calendar returns correct span based on month presence."""

    @skip_if_no_skyfield
    def test_moon_phases_without_month_returns_full_year(self) -> None:
        """compute_moon_phases without month → 365 or 366 entries."""
        from weewx_clearskies_api.services.almanac import compute_moon_phases
        days = compute_moon_phases(year=2024, lat=42.375, lon=-72.519, month=None)
        assert len(days) in (365, 366), (
            f"Full-year moon phases must have 365 or 366 entries, got {len(days)}"
        )

    @skip_if_no_skyfield
    def test_moon_phases_with_month_6_returns_june_only(self) -> None:
        """compute_moon_phases with month=6 returns exactly 30 entries (June)."""
        from weewx_clearskies_api.services.almanac import compute_moon_phases
        days = compute_moon_phases(year=2024, lat=42.375, lon=-72.519, month=6)
        assert len(days) == 30, (
            f"June has 30 days — expected 30 moon-phase entries, got {len(days)}"
        )
        assert days[0].date_str == "2024-06-01"
        assert days[-1].date_str == "2024-06-30"

    @skip_if_no_skyfield
    def test_moon_phases_entries_have_required_fields(self) -> None:
        """Each MoonDay has date_str, phase_name, and illumination_percent."""
        from weewx_clearskies_api.services.almanac import compute_moon_phases
        days = compute_moon_phases(year=2024, lat=42.375, lon=-72.519, month=1)
        for entry in days:
            assert entry.date_str, f"MoonDay missing date_str: {entry}"
            assert entry.phase_name is not None, f"MoonDay missing phase_name: {entry}"
            assert entry.illumination_percent is not None, (
                f"MoonDay missing illumination_percent: {entry}"
            )

    @skip_if_no_skyfield
    def test_moon_phases_illumination_percent_in_range(self) -> None:
        """illumination_percent is 0..100 for all entries."""
        from weewx_clearskies_api.services.almanac import compute_moon_phases
        days = compute_moon_phases(year=2024, lat=42.375, lon=-72.519, month=1)
        for entry in days:
            assert 0 <= entry.illumination_percent <= 100, (
                f"illumination_percent {entry.illumination_percent} out of 0..100 "
                f"range on {entry.date_str}"
            )


# ---------------------------------------------------------------------------
# Polar edge cases
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("wired_ephemeris")
class TestPolarEdgeCases:
    """Polar-day / polar-night edge cases return correct daylightMinutes + null fields."""

    @skip_if_no_skyfield
    def test_lat_89_jun_21_is_polar_day_with_1440_daylight_minutes(self) -> None:
        """Lat 89°N on Jun 21 = polar day → SunInfo.daylight_minutes=1440, rise/set None."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 6, 21),
            lat=89.0,
            lon=0.0,
            alt_m=0.0,
        )
        assert result.sun.daylight_minutes == 1440, (
            f"Polar day (lat=89, Jun 21) must have daylight_minutes=1440, "
            f"got {result.sun.daylight_minutes!r}"
        )
        assert result.sun.rise is None, (
            f"Polar day: rise must be None, got {result.sun.rise!r}"
        )
        assert result.sun.set is None, (
            f"Polar day: set must be None, got {result.sun.set!r}"
        )

    @skip_if_no_skyfield
    def test_lat_89_dec_21_is_polar_night_with_0_daylight_minutes(self) -> None:
        """Lat 89°N on Dec 21 = polar night → SunInfo.daylight_minutes=0, rise/set None."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 12, 21),
            lat=89.0,
            lon=0.0,
            alt_m=0.0,
        )
        assert result.sun.daylight_minutes == 0, (
            f"Polar night (lat=89, Dec 21) must have daylight_minutes=0, "
            f"got {result.sun.daylight_minutes!r}"
        )
        assert result.sun.rise is None, (
            f"Polar night: rise must be None, got {result.sun.rise!r}"
        )
        assert result.sun.set is None, (
            f"Polar night: set must be None, got {result.sun.set!r}"
        )

    @skip_if_no_skyfield
    def test_polar_day_does_not_raise(self) -> None:
        """Polar-day compute completes without raising any exception."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 6, 21),
            lat=89.0,
            lon=0.0,
            alt_m=0.0,
        )
        assert result is not None

    @skip_if_no_skyfield
    def test_polar_night_does_not_raise(self) -> None:
        """Polar-night compute completes without raising any exception."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 12, 21),
            lat=89.0,
            lon=0.0,
            alt_m=0.0,
        )
        assert result is not None


# ---------------------------------------------------------------------------
# USNO sunrise/sunset reference checks (Belchertown MA)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("wired_ephemeris")
class TestSunriseSetUsnoReference:
    """Sunrise/sunset computed values match USNO almanac within ±1 minute.

    Reference station: Belchertown MA, lat=42.375, lon=-72.519
    Reference source: https://aa.usno.navy.mil/data/RS_OneYear

    2024-06-21 (summer solstice):
      USNO rise: 05:12 EDT = 09:12 UTC  (tolerance ±1 min)
      USNO set:  20:25 EDT = 00:25 UTC next day

    2024-12-21 (winter solstice):
      USNO rise: 07:12 EST = 12:12 UTC  (tolerance ±1 min)
      USNO set:  16:10 EST = 21:10 UTC
    """

    @skip_if_no_skyfield
    def test_sunrise_2024_06_21_belchertown_within_1_minute_of_usno(self) -> None:
        """Sunrise 2024-06-21 Belchertown MA matches USNO (09:12Z ±1 min)."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 6, 21),
            lat=42.375,
            lon=-72.519,
            alt_m=0.0,
        )
        rise = result.sun.rise
        assert rise is not None, (
            "sunrise must not be null for mid-latitude summer solstice"
        )
        rise_dt = datetime.datetime.fromisoformat(rise.replace("Z", "+00:00"))
        usno_rise = datetime.datetime(2024, 6, 21, 9, 12, 0, tzinfo=datetime.timezone.utc)
        delta_minutes = abs((rise_dt - usno_rise).total_seconds()) / 60
        assert delta_minutes <= 1.0, (
            f"Sunrise {rise!r} differs from USNO 09:12Z by {delta_minutes:.1f} min "
            f"(tolerance ±1 min). USNO source: https://aa.usno.navy.mil/data/RS_OneYear"
        )

    @skip_if_no_skyfield
    def test_sunset_2024_06_21_belchertown_within_1_minute_of_usno(self) -> None:
        """Sunset 2024-06-21 Belchertown MA matches USNO (00:25Z next day ±1 min)."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 6, 21),
            lat=42.375,
            lon=-72.519,
            alt_m=0.0,
        )
        sunset = result.sun.set
        assert sunset is not None, "sunset must not be null for mid-latitude summer solstice"
        sunset_dt = datetime.datetime.fromisoformat(sunset.replace("Z", "+00:00"))
        # USNO: 20:25 EDT = 00:25 UTC next day
        usno_set = datetime.datetime(2024, 6, 22, 0, 25, 0, tzinfo=datetime.timezone.utc)
        delta_minutes = abs((sunset_dt - usno_set).total_seconds()) / 60
        assert delta_minutes <= 1.0, (
            f"Sunset {sunset!r} differs from USNO 00:25Z+1day by {delta_minutes:.1f} min "
            f"(tolerance ±1 min). USNO source: https://aa.usno.navy.mil/data/RS_OneYear"
        )

    @skip_if_no_skyfield
    def test_sunrise_2024_12_21_belchertown_within_1_minute_of_usno(self) -> None:
        """Sunrise 2024-12-21 Belchertown MA matches USNO (12:12Z ±1 min)."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 12, 21),
            lat=42.375,
            lon=-72.519,
            alt_m=0.0,
        )
        rise = result.sun.rise
        assert rise is not None, "sunrise must not be null for mid-latitude winter solstice"
        rise_dt = datetime.datetime.fromisoformat(rise.replace("Z", "+00:00"))
        usno_rise = datetime.datetime(2024, 12, 21, 12, 12, 0, tzinfo=datetime.timezone.utc)
        delta_minutes = abs((rise_dt - usno_rise).total_seconds()) / 60
        assert delta_minutes <= 1.0, (
            f"Sunrise {rise!r} differs from USNO 12:12Z by {delta_minutes:.1f} min "
            f"(tolerance ±1 min). USNO source: https://aa.usno.navy.mil/data/RS_OneYear"
        )

    @skip_if_no_skyfield
    def test_sunset_2024_12_21_belchertown_within_1_minute_of_usno(self) -> None:
        """Sunset 2024-12-21 Belchertown MA matches USNO (21:10Z ±1 min)."""
        from weewx_clearskies_api.services.almanac import compute_almanac
        result = compute_almanac(
            d=datetime.date(2024, 12, 21),
            lat=42.375,
            lon=-72.519,
            alt_m=0.0,
        )
        sunset = result.sun.set
        assert sunset is not None, "sunset must not be null for mid-latitude winter solstice"
        sunset_dt = datetime.datetime.fromisoformat(sunset.replace("Z", "+00:00"))
        usno_set = datetime.datetime(2024, 12, 21, 21, 10, 0, tzinfo=datetime.timezone.utc)
        delta_minutes = abs((sunset_dt - usno_set).total_seconds()) / 60
        assert delta_minutes <= 1.0, (
            f"Sunset {sunset!r} differs from USNO 21:10Z by {delta_minutes:.1f} min "
            f"(tolerance ±1 min). USNO source: https://aa.usno.navy.mil/data/RS_OneYear"
        )
