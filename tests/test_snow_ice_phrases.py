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


class TestSnowPhraseMetric:
    def test_default_unit_system_is_us_and_unchanged(self):
        # unit_system omitted entirely -> identical to pre-fix behavior.
        assert sip.snow_phrase(2.5, 0.5, 70, LOCALE) == "up to 2.5 inches"

    def test_metric_tier_selection_uses_cm_converted_to_inches(self):
        # max=5cm (~1.97in, < 3in) and min=1cm (~0.39in, < 1in) satisfy the
        # "up_to_max" tier's inch-calibrated bounds only once converted from
        # cm; the phrase renders the original cm values, not the inches
        # used for tier selection.
        result = sip.snow_phrase(5, 1, 70, LOCALE, unit_system="METRIC")
        assert result == "up to 5 cm"

    def test_metric_renders_original_cm_value_not_converted_inches(self):
        # max=max=10cm -> delta=0 < 2in delta threshold -> "around_max",
        # rendered with the original cm value, not the inches conversion.
        result = sip.snow_phrase(10, 10, 70, LOCALE, unit_system="METRIC")
        assert result == "around 10 cm"

    def test_metric_wide_range_renders_min_to_max_in_cm(self):
        # max=30cm (~11.8in), min=5cm (~1.97in): delta ~9.8in >= 2in ->
        # falls through to the exact min-to-max tier.
        result = sip.snow_phrase(30, 5, 70, LOCALE, unit_system="METRIC")
        assert result == "of 5 to 30 cm"

    def test_metric_no_accumulation_tier_still_reached_via_cm_conversion(self):
        # max=1cm (~0.39in) < 0.5in -> "no accumulation" tier.
        assert (
            sip.snow_phrase(1, 0.5, 70, LOCALE, unit_system="METRIC")
            == "No snow accumulation"
        )

    def test_metricwx_behaves_the_same_as_metric_for_snow(self):
        result = sip.snow_phrase(10, 10, 70, LOCALE, unit_system="METRICWX")
        assert result == "around 10 cm"

    def test_metric_french_locale_unit_word_is_cm_not_pouces(self):
        result = sip.snow_phrase(10, 10, 70, "fr", unit_system="METRIC")
        assert result == "environ 10 cm"


class TestIcePhraseMetric:
    def test_default_unit_system_is_us_and_unchanged(self):
        assert sip.ice_phrase(0.1, LOCALE) == "less than one quarter of an inch"

    def test_metric_bypasses_fractional_tiers_and_renders_mm(self):
        # 6mm ~= 0.236in, which under the US tier ladder would resolve to
        # "less than one quarter of an inch" -- but METRIC bypasses the
        # fractional English tiers entirely (no natural cm/mm equivalent)
        # and renders the integer-rounded mm value instead.
        assert sip.ice_phrase(6, LOCALE, unit_system="METRIC") == "6 mm"

    def test_metric_rounds_to_nearest_whole_mm(self):
        assert sip.ice_phrase(12.6, LOCALE, unit_system="METRIC") == "13 mm"

    def test_metricwx_behaves_the_same_as_metric_for_ice(self):
        assert sip.ice_phrase(6, LOCALE, unit_system="METRICWX") == "6 mm"

    def test_metric_does_not_apply_the_10x_cm_conversion_factor(self):
        # Regression guard: ice_accumulation is millimeters, not
        # centimeters, for METRIC/METRICWX (confirmed with lead 2026-07-06).
        # 25.4mm is exactly 1 inch; converting it as if it were 25.4cm
        # (10 inches) would be a 10x error and would NOT round-trip back to
        # "25 mm" -- it would produce a wildly different phrase via the
        # cm-scaled inches value feeding the wrong tier math.
        assert sip.ice_phrase(25.4, LOCALE, unit_system="METRIC") == "25 mm"

    def test_metric_french_locale_unit_word_is_mm_not_pouce(self):
        assert sip.ice_phrase(6, "fr", unit_system="METRIC") == "6 mm"


if __name__ == "__main__":
    pytest.main([__file__])
