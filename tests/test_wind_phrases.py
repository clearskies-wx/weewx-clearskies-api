"""Unit tests for wind phrase generation (ADR-082 settled decision #11).

Covers weewx_clearskies_api/sse/gfe/wind_phrases.py: the full vector phrase
(wind_phrase) — null wind, equal min/max, ranges, direction prefixing, and
gust suppression/reporting — plus the standalone hybrid Beaufort/GFE scale
(wind_descriptor) and the marine gale/storm/hurricane descriptor
(marine_wind_phrase).

Module under test: weewx_clearskies_api/sse/gfe/wind_phrases.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import wind_phrases as wp

LOCALE = "en"


class TestWindPhraseNullWind:
    def test_wind_phrase_none_max_returns_null_phrase(self):
        assert wp.wind_phrase(None, None, None, None, LOCALE) == "light winds"

    def test_wind_phrase_below_null_threshold_returns_null_phrase(self):
        # WIND_NULL_THRESHOLD is 5 mph; 3 mph is below it.
        assert wp.wind_phrase(None, 3, None, None, LOCALE) == "light winds"


class TestWindPhraseMagnitude:
    def test_wind_phrase_equal_min_and_max_uses_around(self):
        assert wp.wind_phrase(10, 10, None, None, LOCALE) == "around 10 mph"

    def test_wind_phrase_range_uses_min_to_max(self):
        assert wp.wind_phrase(10, 20, None, None, LOCALE) == "10 to 20 mph"

    def test_wind_phrase_missing_min_uses_up_to(self):
        assert wp.wind_phrase(None, 15, None, None, LOCALE) == "up to 15 mph"

    def test_wind_phrase_min_below_null_threshold_uses_up_to(self):
        assert wp.wind_phrase(3, 15, None, None, LOCALE) == "up to 15 mph"

    def test_wind_phrase_direction_prefixes_magnitude(self):
        assert wp.wind_phrase(10, 20, None, "NW", LOCALE) == "NW winds 10 to 20 mph"

    def test_wind_phrase_no_direction_omits_direction_prefix(self):
        # Empty-string direction is falsy, same as None — no prefix either way.
        assert wp.wind_phrase(10, 20, None, "", LOCALE) == "10 to 20 mph"


class TestWindPhraseGusts:
    def test_wind_phrase_gust_suppressed_when_difference_at_threshold(self):
        # WIND_GUST_DIFFERENCE is 10; a diff of exactly 10 does not qualify
        # (condition is strictly "> WIND_GUST_DIFFERENCE").
        assert wp.wind_phrase(None, 20, 30, None, LOCALE) == "up to 20 mph"

    def test_wind_phrase_gust_suppressed_below_difference_threshold(self):
        assert wp.wind_phrase(None, 20, 28, None, LOCALE) == "up to 20 mph"

    def test_wind_phrase_gust_reported_above_difference_threshold(self):
        result = wp.wind_phrase(None, 20, 32, None, LOCALE)
        assert result == "up to 20 mph with gusts to around 32 mph"


class TestWindDescriptorHybridScale:
    def test_wind_descriptor_calm_at_zero(self):
        assert wp.wind_descriptor(0, LOCALE) == "Calm"

    def test_wind_descriptor_just_below_strong_breeze_upper_bound(self):
        assert wp.wind_descriptor(29, LOCALE) == "Strong Breeze"

    def test_wind_descriptor_at_windy_lower_boundary(self):
        assert wp.wind_descriptor(30, LOCALE) == "Windy"

    def test_wind_descriptor_at_very_windy_lower_boundary(self):
        assert wp.wind_descriptor(40, LOCALE) == "Very Windy"

    def test_wind_descriptor_at_strong_winds_lower_boundary(self):
        assert wp.wind_descriptor(50, LOCALE) == "Strong Winds"

    def test_wind_descriptor_at_hurricane_force_boundary(self):
        # 74 is the exclusive upper bound of "strong_winds" and therefore
        # the first speed that resolves to hurricane force.
        assert wp.wind_descriptor(74, LOCALE) == "Hurricane Force Winds"

    def test_wind_descriptor_far_above_hurricane_threshold_still_resolves(self):
        assert wp.wind_descriptor(999, LOCALE) == "Hurricane Force Winds"


class TestMarineWindPhrase:
    def test_marine_wind_phrase_below_gale_threshold_returns_none(self):
        assert wp.marine_wind_phrase(33, LOCALE) is None

    def test_marine_wind_phrase_at_gale_threshold_34_knots(self):
        assert wp.marine_wind_phrase(34, LOCALE) == "Gales"

    def test_marine_wind_phrase_at_storm_force_threshold_45_knots(self):
        assert wp.marine_wind_phrase(45, LOCALE) == "Storm Force"

    def test_marine_wind_phrase_at_hurricane_force_threshold_64_knots(self):
        assert wp.marine_wind_phrase(64, LOCALE) == "Hurricane Force"

    def test_marine_wind_phrase_between_gale_and_storm_force_stays_gales(self):
        assert wp.marine_wind_phrase(40, LOCALE) == "Gales"


class TestWindPhraseLocale:
    def test_wind_descriptor_non_english_locale_resolves_french_label(self):
        assert wp.wind_descriptor(29, "fr") == "Vent frais"

    def test_wind_phrase_non_english_locale_resolves_french_direction_template(self):
        result = wp.wind_phrase(10, 20, None, "NW", "fr")
        assert result == "vents du NW 10 à 20 mph"


if __name__ == "__main__":
    pytest.main([__file__])
