"""Unit tests for snow and ice accumulation phrasing (ADR-082, GFE SS9).

Covers weewx_clearskies_api/sse/gfe/snow_ice_phrases.py: the PoP gate on
snow_phrase, the SNOW_ACCUMULATION_TIERS ladder (no/little-or-no/up-to/
around/exact-range), descriptive_snow's category bands, and
ICE_ACCUMULATION_TIERS's fractional-inch phrases through the final
integer-rounded tier.

Module under test: weewx_clearskies_api/sse/gfe/snow_ice_phrases.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import snow_ice_phrases as sip

LOCALE = "en"


class TestSnowPhrasePopGate:
    def test_snow_phrase_below_pop_threshold_returns_none(self):
        # POP_SNOW_LOWER_THRESHOLD is 60.
        assert sip.snow_phrase(4, 3, 50, LOCALE) is None

    def test_snow_phrase_at_pop_threshold_is_not_gated(self):
        assert sip.snow_phrase(4, 3, 60, LOCALE) is not None


class TestSnowPhraseAccumulationTiers:
    def test_snow_phrase_below_half_inch_reports_no_accumulation(self):
        assert sip.snow_phrase(0.3, 0.1, 70, LOCALE) == "No snow accumulation"

    def test_snow_phrase_below_one_inch_reports_little_or_no_accumulation(self):
        assert sip.snow_phrase(0.8, 0.2, 70, LOCALE) == "Little or no snow accumulation"

    def test_snow_phrase_narrow_low_range_reports_up_to_max(self):
        # max < 3, min < 1.
        assert sip.snow_phrase(2.5, 0.5, 70, LOCALE) == "up to 2.5 inches"

    def test_snow_phrase_small_delta_reports_around_max(self):
        # max - min = 1, under the delta_lt=2 tier condition.
        assert sip.snow_phrase(4, 3, 70, LOCALE) == "around 4 inches"

    def test_snow_phrase_wide_range_reports_exact_min_to_max(self):
        # max - min = 6, falls through to the "otherwise" tier.
        assert sip.snow_phrase(8, 2, 70, LOCALE) == "of 2 to 8 inches"


class TestDescriptiveSnow:
    def test_descriptive_snow_under_one_inch_has_no_descriptor(self):
        assert sip.descriptive_snow(0.5, LOCALE) is None

    def test_descriptive_snow_light_band(self):
        assert sip.descriptive_snow(1.5, LOCALE) == "Light snow accumulations"

    def test_descriptive_snow_moderate_band(self):
        assert sip.descriptive_snow(3, LOCALE) == "Moderate snow accumulations"

    def test_descriptive_snow_heavy_band_unbounded(self):
        assert sip.descriptive_snow(10, LOCALE) == "Heavy snow accumulations"


class TestIcePhraseFractionalTiers:
    def test_ice_phrase_below_one_quarter(self):
        assert sip.ice_phrase(0.1, LOCALE) == "less than one quarter of an inch"

    def test_ice_phrase_at_one_quarter_boundary_rolls_to_next_tier(self):
        # upper_bound is exclusive: 0.2 does not satisfy "< 0.2" for the
        # first tier, so it resolves through the second tier's phrase.
        assert sip.ice_phrase(0.2, LOCALE) == "one quarter of an inch"

    def test_ice_phrase_at_one_half_boundary_rolls_to_next_tier(self):
        assert sip.ice_phrase(0.4, LOCALE) == "one half of an inch"

    def test_ice_phrase_at_three_quarters_boundary_rolls_to_next_tier(self):
        assert sip.ice_phrase(0.7, LOCALE) == "three quarters of an inch"

    def test_ice_phrase_at_one_inch_boundary_rolls_to_next_tier(self):
        assert sip.ice_phrase(0.9, LOCALE) == "one inch"

    def test_ice_phrase_at_one_and_a_half_boundary_rolls_to_next_tier(self):
        assert sip.ice_phrase(1.3, LOCALE) == "one and a half inches"

    def test_ice_phrase_at_final_boundary_uses_integer_rounding(self):
        # 1.8 is the last named tier's exclusive upper bound: the open-ended
        # final tier rounds to the nearest whole integer.
        assert sip.ice_phrase(1.8, LOCALE) == "2 inches"

    def test_ice_phrase_above_final_boundary_rounds_to_nearest_integer(self):
        assert sip.ice_phrase(2.3, LOCALE) == "2 inches"


class TestSnowIcePhraseLocale:
    def test_snow_phrase_non_english_locale_uses_french_decimal_comma(self):
        assert sip.snow_phrase(2.5, 0.5, 70, "fr") == "jusqu'à 2,5 pouces"

    def test_ice_phrase_non_english_locale_resolves_french_wording(self):
        assert sip.ice_phrase(0.1, "fr") == "moins d'un quart de pouce"


if __name__ == "__main__":
    pytest.main([__file__])
