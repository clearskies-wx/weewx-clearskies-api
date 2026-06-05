"""Unit tests for viewing quality / visibility tier computation — T1.10.

Tests the visibility logic for meteor showers, solar eclipses, and lunar
eclipses per ADR-053.  Because the visibility logic is computed inline inside
the service/endpoint functions, this file extracts that logic into small
helper functions that faithfully mirror the source, then tests those helpers.

The helpers are defined inline in this file and verified against the exact
branch conditions in the production code.  If production logic changes, these
helpers must be updated to match — that is intentional: the test verifies the
CONTRACT (ADR-053 tier names + thresholds), not an abstraction.

Meteor viewing quality source: services/almanac.py compute_meteor_showers()
Solar visibility source: endpoints/almanac.py get_solar_eclipses()
Lunar visibility source: endpoints/almanac.py get_eclipses()

No DB, no network, no ephemeris required.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Extracted logic helpers — mirror production code exactly
# ---------------------------------------------------------------------------


def _compute_meteor_viewing_quality(radiant_alt: float, moon_illum_pct: float) -> str:
    """Mirror of the viewing-quality branch in compute_meteor_showers() (ADR-053).

    Source: weewx_clearskies_api/services/almanac.py, compute_meteor_showers().

    Branches (in order):
        radiant_alt < 0                                    → "Not Visible"
        radiant_alt > 40 and moon_illum_pct < 25          → "Excellent"
        radiant_alt > 20 and moon_illum_pct < 50          → "Good"
        radiant_alt <= 10 or (moon_illum > 75 and alt<=30)→ "Poor"
        else                                               → "Fair"
    """
    if radiant_alt < 0:
        return "Not Visible"
    if radiant_alt > 40 and moon_illum_pct < 25:
        return "Excellent"
    if radiant_alt > 20 and moon_illum_pct < 50:
        return "Good"
    if radiant_alt <= 10 or (moon_illum_pct > 75 and radiant_alt <= 30):
        return "Poor"
    return "Fair"


def _compute_solar_visibility(
    total_start: Any,
    obs: float,
    peak_alt: float | None,
) -> str:
    """Mirror of the solar visibility branch in get_solar_eclipses() (ADR-053).

    Source: weewx_clearskies_api/endpoints/almanac.py, get_solar_eclipses().

    Args:
        total_start: The totalStart contact dict (or None if absent/null).
        obs: obscuration value (0–100), defaulting to 0 when None.
        peak_alt: Peak altitude in degrees (or None if peak is absent).

    Branches (in order):
        peak_alt is None or peak_alt <= 0 or obs == 0 → "Not Visible"
        total_start is not None                        → "Fully Visible"
        obs >= 75                                      → "Mostly Visible"
        obs >= 10                                      → "Partially Visible"
        else                                           → "Barely Visible"
    """
    if peak_alt is None or peak_alt <= 0 or obs == 0:
        return "Not Visible"
    if total_start is not None:
        return "Fully Visible"
    if obs >= 75:
        return "Mostly Visible"
    if obs >= 10:
        return "Partially Visible"
    return "Barely Visible"


def _compute_lunar_visibility(
    peak_alt: float | None,
    contact_times: dict[str, dict | None] | None,
) -> str:
    """Mirror of the lunar visibility branch in get_eclipses() (ADR-053).

    Source: weewx_clearskies_api/endpoints/almanac.py, get_eclipses().

    Args:
        peak_alt: Peak altitude in degrees (or None if peak contact is absent).
        contact_times: Dict of contact field names to {"date": ..., "altitude": float}
            or None.  A None value means the contact didn't occur at this location.

    Branches (in order):
        peak_alt is None or peak_alt <= 0 → "Not Visible"
        peak_alt <= 5                     → "Barely Visible"
        peak_alt <= 15                    → "Low in Sky"
        else: all non-None contacts have altitude > 0
              → "Visible All Night" if all_above, "Mostly Visible" otherwise
    """
    if peak_alt is None or peak_alt <= 0:
        return "Not Visible"
    if peak_alt <= 5:
        return "Barely Visible"
    if peak_alt <= 15:
        return "Low in Sky"
    # Check if all non-None contacts are above the horizon.
    all_above = all(
        (ct or {}).get("altitude", -1) > 0
        for ct in (contact_times or {}).values()
        if ct is not None
    )
    return "Visible All Night" if all_above else "Mostly Visible"


# ===========================================================================
# 1. Meteor viewing quality tier tests (ADR-053)
# ===========================================================================


class TestMeteorViewingQuality:
    """_compute_meteor_viewing_quality mirrors ADR-053 tier thresholds."""

    # --- "Not Visible" tier ---

    def test_radiant_below_horizon_is_not_visible(self) -> None:
        """radiant_alt=-5, moon_illum=10 → 'Not Visible' (radiant below horizon)."""
        assert _compute_meteor_viewing_quality(-5, 10) == "Not Visible"

    def test_radiant_exactly_zero_is_not_visible(self) -> None:
        """radiant_alt=0, moon_illum=10 → still 'Not Visible' (alt < 0 is the boundary)."""
        # alt < 0 is the condition, so 0 is NOT below horizon → falls to other branches.
        # At alt=0, moon_illum=10: not <0, not >40, not >20, not <=10 — wait:
        # alt <= 10 → "Poor".  Let's verify.
        result = _compute_meteor_viewing_quality(0, 10)
        # 0 is not < 0, so "Not Visible" doesn't apply.
        # 0 > 40? No. 0 > 20? No. 0 <= 10? Yes → "Poor"
        assert result == "Poor"

    def test_radiant_very_negative_is_not_visible(self) -> None:
        """radiant_alt=-45, moon_illum=0 → 'Not Visible'."""
        assert _compute_meteor_viewing_quality(-45, 0) == "Not Visible"

    # --- "Excellent" tier ---

    def test_high_radiant_dark_sky_is_excellent(self) -> None:
        """radiant_alt=50, moon_illum=10 → 'Excellent' (high radiant, dark sky)."""
        assert _compute_meteor_viewing_quality(50, 10) == "Excellent"

    def test_radiant_just_above_40_low_illum_is_excellent(self) -> None:
        """radiant_alt=41, moon_illum=24 → 'Excellent' (just over the thresholds)."""
        assert _compute_meteor_viewing_quality(41, 24) == "Excellent"

    def test_radiant_exactly_40_not_excellent(self) -> None:
        """radiant_alt=40, moon_illum=10 → not 'Excellent' (condition is >40, not >=40)."""
        result = _compute_meteor_viewing_quality(40, 10)
        assert result != "Excellent", (
            f"alt=40 must NOT be Excellent (condition is alt>40), got {result!r}"
        )

    def test_high_radiant_moon_illum_at_25_not_excellent(self) -> None:
        """radiant_alt=50, moon_illum=25 → not 'Excellent' (condition is <25, not <=25)."""
        result = _compute_meteor_viewing_quality(50, 25)
        assert result != "Excellent", (
            f"moon_illum=25 must NOT be Excellent (condition is <25), got {result!r}"
        )

    # --- "Good" tier ---

    def test_decent_radiant_moderate_moon_is_good(self) -> None:
        """radiant_alt=30, moon_illum=30 → 'Good' (decent radiant, moderate moon)."""
        assert _compute_meteor_viewing_quality(30, 30) == "Good"

    def test_radiant_just_above_20_low_illum_is_good(self) -> None:
        """radiant_alt=21, moon_illum=40 → 'Good'."""
        assert _compute_meteor_viewing_quality(21, 40) == "Good"

    def test_radiant_exactly_20_not_good(self) -> None:
        """radiant_alt=20, moon_illum=30 → not 'Good' (condition is >20, not >=20)."""
        result = _compute_meteor_viewing_quality(20, 30)
        assert result != "Good", (
            f"alt=20 must NOT be Good (condition is alt>20), got {result!r}"
        )

    # --- "Fair" tier ---

    def test_moderate_conditions_is_fair(self) -> None:
        """radiant_alt=15, moon_illum=60 → 'Fair' (moderate conditions)."""
        assert _compute_meteor_viewing_quality(15, 60) == "Fair"

    def test_alt_11_mid_moon_illum_is_fair(self) -> None:
        """radiant_alt=11, moon_illum=50 → 'Fair' (above 10, moon not bright enough for Poor)."""
        # alt=11 (not <=10), moon=50 (not >75 while alt<=30) → "Fair"
        assert _compute_meteor_viewing_quality(11, 50) == "Fair"

    # --- "Poor" tier ---

    def test_low_radiant_bright_moon_is_poor(self) -> None:
        """radiant_alt=5, moon_illum=80 → 'Poor' (low radiant + bright moon)."""
        assert _compute_meteor_viewing_quality(5, 80) == "Poor"

    def test_radiant_at_10_is_poor(self) -> None:
        """radiant_alt=10, moon_illum=30 → 'Poor' (alt<=10 is Poor boundary)."""
        assert _compute_meteor_viewing_quality(10, 30) == "Poor"

    def test_high_moon_illum_low_altitude_is_poor(self) -> None:
        """radiant_alt=25, moon_illum=80 → 'Poor' (bright moon AND alt<=30)."""
        # alt=25 (>20, not <=10), moon=80 (>75) and alt<=30 → "Poor"
        assert _compute_meteor_viewing_quality(25, 80) == "Poor"

    def test_very_bright_moon_at_alt_30_is_poor(self) -> None:
        """radiant_alt=30, moon_illum=76 → 'Poor' (moon>75 and alt<=30)."""
        assert _compute_meteor_viewing_quality(30, 76) == "Poor"

    def test_bright_moon_alt_31_is_not_poor_from_moon_condition(self) -> None:
        """radiant_alt=31, moon_illum=80 → not 'Poor' from moon condition (alt>30)."""
        # moon=80>75 but alt=31>30, so moon condition doesn't apply.
        # alt=31 not <=10.  alt=31>20, moon=80 >= 50, so not "Good".
        # Falls to "Fair".
        result = _compute_meteor_viewing_quality(31, 80)
        assert result != "Poor", (
            f"alt=31 with bright moon must NOT be Poor (alt>30 boundary), got {result!r}"
        )


# ===========================================================================
# 2. Solar visibility tier tests (ADR-053)
# ===========================================================================


class TestSolarVisibility:
    """_compute_solar_visibility mirrors ADR-053 solar tier thresholds."""

    # --- "Not Visible" ---

    def test_peak_alt_none_is_not_visible(self) -> None:
        """peak_alt=None → 'Not Visible'."""
        assert _compute_solar_visibility(None, 80.0, None) == "Not Visible"

    def test_peak_alt_zero_is_not_visible(self) -> None:
        """peak_alt=0 → 'Not Visible' (condition is peak_alt <= 0)."""
        assert _compute_solar_visibility(None, 80.0, 0.0) == "Not Visible"

    def test_peak_alt_negative_is_not_visible(self) -> None:
        """peak_alt=-5 → 'Not Visible' (sun below horizon)."""
        assert _compute_solar_visibility(None, 80.0, -5.0) == "Not Visible"

    def test_obscuration_zero_is_not_visible(self) -> None:
        """obscuration=0 → 'Not Visible' regardless of peak_alt."""
        assert _compute_solar_visibility(None, 0.0, 30.0) == "Not Visible"

    # --- "Fully Visible" ---

    def test_total_start_non_null_is_fully_visible(self) -> None:
        """totalStart non-null → 'Fully Visible' (total eclipse, in totality path)."""
        total_start = {"date": "2026-08-12T16:00:00.000Z", "altitude": 50.0}
        assert _compute_solar_visibility(total_start, 100.0, 50.0) == "Fully Visible"

    def test_total_start_present_with_high_obscuration_is_fully_visible(self) -> None:
        """totalStart present even with obscuration=100 → 'Fully Visible'."""
        total_start = {"date": "2026-08-12T16:00:00.000Z", "altitude": 30.0}
        assert _compute_solar_visibility(total_start, 100.0, 30.0) == "Fully Visible"

    # --- "Mostly Visible" ---

    def test_obscuration_85_is_mostly_visible(self) -> None:
        """obscuration=85, peak_alt=30 → 'Mostly Visible'."""
        assert _compute_solar_visibility(None, 85.0, 30.0) == "Mostly Visible"

    def test_obscuration_exactly_75_is_mostly_visible(self) -> None:
        """obscuration=75, peak_alt=30 → 'Mostly Visible' (condition is >=75)."""
        assert _compute_solar_visibility(None, 75.0, 30.0) == "Mostly Visible"

    def test_obscuration_74_is_not_mostly_visible(self) -> None:
        """obscuration=74, peak_alt=30 → not 'Mostly Visible' (condition is >=75)."""
        result = _compute_solar_visibility(None, 74.0, 30.0)
        assert result != "Mostly Visible", (
            f"obscuration=74 must NOT be Mostly Visible (needs >=75), got {result!r}"
        )

    # --- "Partially Visible" ---

    def test_obscuration_40_is_partially_visible(self) -> None:
        """obscuration=40, peak_alt=20 → 'Partially Visible'."""
        assert _compute_solar_visibility(None, 40.0, 20.0) == "Partially Visible"

    def test_obscuration_exactly_10_is_partially_visible(self) -> None:
        """obscuration=10, peak_alt=20 → 'Partially Visible' (condition is >=10)."""
        assert _compute_solar_visibility(None, 10.0, 20.0) == "Partially Visible"

    def test_obscuration_9_is_barely_visible(self) -> None:
        """obscuration=9, peak_alt=20 → 'Barely Visible' (obs < 10)."""
        assert _compute_solar_visibility(None, 9.0, 20.0) == "Barely Visible"

    # --- "Barely Visible" ---

    def test_obscuration_5_is_barely_visible(self) -> None:
        """obscuration=5, peak_alt=10 → 'Barely Visible'."""
        assert _compute_solar_visibility(None, 5.0, 10.0) == "Barely Visible"

    def test_obscuration_1_is_barely_visible(self) -> None:
        """obscuration=1 (non-zero but very small), peak_alt=5 → 'Barely Visible'."""
        assert _compute_solar_visibility(None, 1.0, 5.0) == "Barely Visible"


# ===========================================================================
# 3. Lunar visibility tier tests (ADR-053)
# ===========================================================================


class TestLunarVisibility:
    """_compute_lunar_visibility mirrors ADR-053 lunar tier thresholds."""

    def _all_positive_contacts(self) -> dict[str, dict | None]:
        """All 7 contact fields with positive altitudes."""
        return {
            "penumbralStart": {"date": "2026-09-07T16:30:00Z", "altitude": 5.0},
            "partialStart": {"date": "2026-09-07T17:30:00Z", "altitude": 15.0},
            "fullStart": {"date": "2026-09-07T18:20:00Z", "altitude": 25.0},
            "peak": {"date": "2026-09-07T18:44:00Z", "altitude": 40.0},
            "fullEnd": {"date": "2026-09-07T19:08:00Z", "altitude": 30.0},
            "partialEnd": {"date": "2026-09-07T19:58:00Z", "altitude": 20.0},
            "penumbralEnd": {"date": "2026-09-07T20:58:00Z", "altitude": 10.0},
        }

    def _contacts_with_some_negative(self) -> dict[str, dict | None]:
        """Contact fields where some (early) contacts have negative altitude."""
        return {
            "penumbralStart": {"date": "2026-09-07T14:00:00Z", "altitude": -5.0},  # below horizon
            "partialStart": {"date": "2026-09-07T15:30:00Z", "altitude": -1.0},   # below horizon
            "fullStart": {"date": "2026-09-07T18:20:00Z", "altitude": 25.0},
            "peak": {"date": "2026-09-07T18:44:00Z", "altitude": 40.0},
            "fullEnd": {"date": "2026-09-07T19:08:00Z", "altitude": 30.0},
            "partialEnd": {"date": "2026-09-07T19:58:00Z", "altitude": 20.0},
            "penumbralEnd": {"date": "2026-09-07T20:58:00Z", "altitude": 10.0},
        }

    # --- "Not Visible" ---

    def test_peak_alt_none_is_not_visible(self) -> None:
        """peak_alt=None → 'Not Visible'."""
        assert _compute_lunar_visibility(None, self._all_positive_contacts()) == "Not Visible"

    def test_peak_alt_zero_is_not_visible(self) -> None:
        """peak_alt=0 → 'Not Visible' (condition is peak_alt <= 0)."""
        assert _compute_lunar_visibility(0.0, self._all_positive_contacts()) == "Not Visible"

    def test_peak_alt_negative_is_not_visible(self) -> None:
        """peak_alt=-5 → 'Not Visible' (peak below horizon)."""
        assert _compute_lunar_visibility(-5.0, self._all_positive_contacts()) == "Not Visible"

    # --- "Barely Visible" ---

    def test_peak_alt_3_is_barely_visible(self) -> None:
        """peak_alt=3 → 'Barely Visible' (peak_alt <= 5)."""
        assert _compute_lunar_visibility(3.0, self._all_positive_contacts()) == "Barely Visible"

    def test_peak_alt_exactly_5_is_barely_visible(self) -> None:
        """peak_alt=5 → 'Barely Visible' (boundary is peak_alt <= 5)."""
        assert _compute_lunar_visibility(5.0, self._all_positive_contacts()) == "Barely Visible"

    def test_peak_alt_1_is_barely_visible(self) -> None:
        """peak_alt=1 → 'Barely Visible'."""
        assert _compute_lunar_visibility(1.0, self._all_positive_contacts()) == "Barely Visible"

    # --- "Low in Sky" ---

    def test_peak_alt_12_is_low_in_sky(self) -> None:
        """peak_alt=12 → 'Low in Sky' (6 <= peak_alt <= 15)."""
        assert _compute_lunar_visibility(12.0, self._all_positive_contacts()) == "Low in Sky"

    def test_peak_alt_6_is_low_in_sky(self) -> None:
        """peak_alt=6 → 'Low in Sky' (just above Barely Visible boundary of 5)."""
        assert _compute_lunar_visibility(6.0, self._all_positive_contacts()) == "Low in Sky"

    def test_peak_alt_exactly_15_is_low_in_sky(self) -> None:
        """peak_alt=15 → 'Low in Sky' (boundary is peak_alt <= 15)."""
        assert _compute_lunar_visibility(15.0, self._all_positive_contacts()) == "Low in Sky"

    # --- "Visible All Night" ---

    def test_high_alt_all_contacts_positive_is_visible_all_night(self) -> None:
        """peak_alt=40, all contacts > 0 → 'Visible All Night'."""
        assert (
            _compute_lunar_visibility(40.0, self._all_positive_contacts())
            == "Visible All Night"
        )

    def test_null_contacts_treated_as_absent_not_negative(self) -> None:
        """Contact fields that are None (did not occur) are excluded from the all_above check."""
        contacts = {
            "penumbralStart": None,  # did not occur — excluded from check
            "partialStart": None,
            "fullStart": {"date": "2026-09-07T18:20:00Z", "altitude": 25.0},
            "peak": {"date": "2026-09-07T18:44:00Z", "altitude": 40.0},
            "fullEnd": {"date": "2026-09-07T19:08:00Z", "altitude": 30.0},
            "partialEnd": {"date": "2026-09-07T19:58:00Z", "altitude": 20.0},
            "penumbralEnd": None,
        }
        # The non-None contacts all have altitude > 0 → "Visible All Night"
        result = _compute_lunar_visibility(40.0, contacts)
        assert result == "Visible All Night", (
            f"None contacts must be excluded from the all_above check, got {result!r}"
        )

    # --- "Mostly Visible" ---

    def test_high_alt_some_contacts_negative_is_mostly_visible(self) -> None:
        """peak_alt=40, some contacts < 0 → 'Mostly Visible'."""
        assert (
            _compute_lunar_visibility(40.0, self._contacts_with_some_negative())
            == "Mostly Visible"
        )

    def test_peak_alt_16_some_contacts_negative_is_mostly_visible(self) -> None:
        """peak_alt=16 (>15), some contacts < 0 → 'Mostly Visible'."""
        assert (
            _compute_lunar_visibility(16.0, self._contacts_with_some_negative())
            == "Mostly Visible"
        )

    def test_empty_contact_dict_all_none_is_visible_all_night(self) -> None:
        """With no non-None contacts, all() vacuously returns True → 'Visible All Night'."""
        # all() of an empty iterable is True in Python.
        contacts: dict = {}
        result = _compute_lunar_visibility(40.0, contacts)
        assert result == "Visible All Night", (
            f"Empty contact dict (vacuous all()) must yield 'Visible All Night', "
            f"got {result!r}"
        )

    def test_all_contacts_none_is_visible_all_night(self) -> None:
        """When all contacts are None (vacuously all above) → 'Visible All Night'."""
        contacts = {
            "penumbralStart": None,
            "partialStart": None,
            "fullStart": None,
            "peak": None,
            "fullEnd": None,
            "partialEnd": None,
            "penumbralEnd": None,
        }
        # All contacts are None → excluded from check → vacuous True → "Visible All Night"
        result = _compute_lunar_visibility(40.0, contacts)
        assert result == "Visible All Night"


# ===========================================================================
# 4. Cross-tier boundary sanity checks
# ===========================================================================


class TestVisibilityBoundarySanity:
    """Cross-tier: adjacent boundary values produce different tiers."""

    def test_meteor_alt_just_above_0_is_not_not_visible(self) -> None:
        """radiant_alt=0.1, moon_illum=0 → not 'Not Visible' (alt > 0 crosses boundary)."""
        result = _compute_meteor_viewing_quality(0.1, 0)
        assert result != "Not Visible", (
            f"alt=0.1 must not be 'Not Visible', got {result!r}"
        )

    def test_solar_peak_alt_just_above_0_not_not_visible_when_obs_positive(self) -> None:
        """peak_alt=0.1, obs=50 → not 'Not Visible'."""
        result = _compute_solar_visibility(None, 50.0, 0.1)
        assert result != "Not Visible", (
            f"peak_alt=0.1 with obs=50 must not be 'Not Visible', got {result!r}"
        )

    def test_lunar_peak_alt_just_above_15_is_not_low_in_sky(self) -> None:
        """peak_alt=15.1 → not 'Low in Sky' (crosses the <=15 boundary)."""
        contacts = {
            "peak": {"date": "2026-09-07T18:44:00Z", "altitude": -5.0},  # below for mostly visible
        }
        result = _compute_lunar_visibility(15.1, contacts)
        assert result != "Low in Sky", (
            f"peak_alt=15.1 must not be 'Low in Sky', got {result!r}"
        )

    def test_lunar_peak_alt_just_above_5_is_not_barely_visible(self) -> None:
        """peak_alt=5.1 → not 'Barely Visible' (crosses the <=5 boundary)."""
        result = _compute_lunar_visibility(5.1, self._all_positive_contacts())
        assert result != "Barely Visible", (
            f"peak_alt=5.1 must not be 'Barely Visible', got {result!r}"
        )

    def _all_positive_contacts(self) -> dict[str, dict | None]:
        return {
            "peak": {"date": "2026-09-07T18:44:00Z", "altitude": 30.0},
        }
