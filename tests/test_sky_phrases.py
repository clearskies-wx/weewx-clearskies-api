"""Unit tests for sky-coverage phrase generation (ADR-082, GFE SS1).

Covers weewx_clearskies_api/sse/gfe/sky_phrases.py: bucket-boundary lookup
(sky_key/sky_phrase), pop-driven suppression (sky_pop_suppression), and
adjacent-transition trend phrasing (sky_trend_phrase).

Module under test: weewx_clearskies_api/sse/gfe/sky_phrases.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import sky_phrases
from weewx_clearskies_api.sse.gfe.thresholds import POP_SKY_LOWER_THRESHOLD

LOCALE = "en"


class TestSkyPhraseBucketBoundaries:
    """SKY_VALUE_LIST buckets: (5, 25, 50, 69, 87, 100), inclusive upper bound."""

    def test_sky_phrase_at_boundary_5_percent_daytime_is_sunny(self):
        assert sky_phrases.sky_phrase(5, True, LOCALE) == "Sunny"

    def test_sky_phrase_just_above_5_percent_daytime_still_sunny(self):
        # 6 falls in the (5, 25] bucket, which is also "sunny" by day.
        assert sky_phrases.sky_phrase(6, True, LOCALE) == "Sunny"

    def test_sky_phrase_at_boundary_25_percent_night_is_mostly_clear(self):
        assert sky_phrases.sky_phrase(25, False, LOCALE) == "Mostly Clear"

    def test_sky_phrase_at_boundary_50_percent_daytime_is_mostly_sunny(self):
        assert sky_phrases.sky_phrase(50, True, LOCALE) == "Mostly Sunny"

    def test_sky_phrase_just_above_50_percent_daytime_is_partly_sunny(self):
        assert sky_phrases.sky_phrase(51, True, LOCALE) == "Partly Sunny"

    def test_sky_phrase_at_boundary_69_percent_night_is_mostly_cloudy(self):
        assert sky_phrases.sky_phrase(69, False, LOCALE) == "Mostly Cloudy"

    def test_sky_phrase_at_boundary_87_percent_daytime_is_mostly_cloudy(self):
        assert sky_phrases.sky_phrase(87, True, LOCALE) == "Mostly Cloudy"

    def test_sky_phrase_just_above_87_percent_daytime_is_cloudy(self):
        assert sky_phrases.sky_phrase(88, True, LOCALE) == "Cloudy"

    def test_sky_phrase_at_boundary_100_percent_is_cloudy_both_day_and_night(self):
        assert sky_phrases.sky_phrase(100, True, LOCALE) == "Cloudy"
        assert sky_phrases.sky_phrase(100, False, LOCALE) == "Cloudy"

    def test_sky_phrase_out_of_range_above_100_falls_back_to_cloudiest_bucket(self):
        # Defensive fallback per sky_key()'s docstring: unclamped provider
        # data above 100 should not raise, and should read as fully cloudy.
        assert sky_phrases.sky_phrase(150, True, LOCALE) == "Cloudy"


class TestSkyPhraseDayVsNightLabels:
    def test_sky_phrase_zero_percent_day_is_sunny_night_is_clear(self):
        assert sky_phrases.sky_phrase(0, True, LOCALE) == "Sunny"
        assert sky_phrases.sky_phrase(0, False, LOCALE) == "Clear"

    def test_sky_key_returns_locale_independent_keys(self):
        assert sky_phrases.sky_key(10, True) == "sunny"
        assert sky_phrases.sky_key(10, False) == "mostly_clear"


class TestSkyPopSuppression:
    def test_sky_pop_suppression_below_threshold_54_percent_does_not_suppress(self):
        assert sky_phrases.sky_pop_suppression(54) is False

    def test_sky_pop_suppression_at_threshold_55_percent_suppresses(self):
        assert sky_phrases.sky_pop_suppression(55) is True

    def test_sky_pop_suppression_well_above_threshold_suppresses(self):
        assert sky_phrases.sky_pop_suppression(90) is True

    def test_sky_pop_suppression_matches_threshold_constant(self):
        assert sky_phrases.sky_pop_suppression(POP_SKY_LOWER_THRESHOLD) is True
        assert sky_phrases.sky_pop_suppression(POP_SKY_LOWER_THRESHOLD - 1) is False


class TestSkyTrendPhrase:
    def test_sky_trend_phrase_suppresses_adjacent_daytime_pair(self):
        # ("sunny", "mostly_sunny") is a listed SIMILAR_SKY_WORDS_DAY pair.
        assert sky_phrases.sky_trend_phrase("sunny", "mostly_sunny", True, LOCALE) is None

    def test_sky_trend_phrase_suppresses_adjacent_pair_in_reverse_order(self):
        # Suppression checks both directions of the pair.
        assert sky_phrases.sky_trend_phrase("mostly_sunny", "sunny", True, LOCALE) is None

    def test_sky_trend_phrase_suppresses_adjacent_nighttime_pair(self):
        assert sky_phrases.sky_trend_phrase("clear", "mostly_clear", False, LOCALE) is None

    def test_sky_trend_phrase_reports_non_adjacent_transition(self):
        result = sky_phrases.sky_trend_phrase("sunny", "cloudy", True, LOCALE)
        assert result == "then becoming Cloudy"

    def test_sky_trend_phrase_reports_non_adjacent_nighttime_transition(self):
        result = sky_phrases.sky_trend_phrase("clear", "cloudy", False, LOCALE)
        assert result == "then becoming Cloudy"


class TestSkyPhraseFallbackAndLocale:
    def test_sky_phrase_falls_back_to_title_case_for_unknown_locale_key(self):
        # A locale with no "forecast.sky.*" entries falls back to
        # title-cased raw key rather than surfacing a raw dotted string.
        result = sky_phrases.sky_phrase(10, True, "xx-nonexistent")
        assert result == "Sunny"

    def test_sky_trend_phrase_non_english_locale_resolves_french_label(self):
        result = sky_phrases.sky_trend_phrase("sunny", "cloudy", True, "fr")
        assert result == "puis devenant Nuageux"

    def test_sky_phrase_non_english_locale_resolves_french_sunny(self):
        assert sky_phrases.sky_phrase(5, True, "fr") == "Ensoleillé"


if __name__ == "__main__":
    pytest.main([__file__])
