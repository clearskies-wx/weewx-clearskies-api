"""Unit tests for temperature decade phrase generation (ADR-082, GFE SS2).

Covers weewx_clearskies_api/sse/gfe/temp_phrases.py: same-decade phrasing
(temp_phrase), the exception table (zero-crossing, above-100, sub-zero,
exact-decade equality), the same-decade-priority-over-diff-threshold rule
documented in the module's docstring, extreme-temperature descriptors
(temp_descriptor), and trend phrasing (temp_trend_phrase) including the
"single digits" / "teens" / "teens below zero" special decade words.

Module under test: weewx_clearskies_api/sse/gfe/temp_phrases.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import temp_phrases as tp

LOCALE = "en"


class TestTempPhraseSameDecade:
    def test_temp_phrase_full_decade_span_omits_position_word(self):
        # min=83 (lower position), max=89 (upper position): spans the full
        # decade, per the module docstring's worked example.
        assert tp.temp_phrase(83, 89, True, LOCALE) == "in the 80s"

    def test_temp_phrase_lower_position_pair(self):
        assert tp.temp_phrase(80, 83, True, LOCALE) == "in the lower 80s"

    def test_temp_phrase_mid_position_pair(self):
        assert tp.temp_phrase(84, 86, True, LOCALE) == "in the mid 80s"

    def test_temp_phrase_upper_position_pair(self):
        assert tp.temp_phrase(87, 89, True, LOCALE) == "in the upper 80s"

    def test_temp_phrase_same_decade_wins_over_diff_threshold(self):
        # Spread here is 6 (> TEMP_DIFF_THRESHOLD=4) but min/max share a
        # decade, so decade phrasing applies instead of an exact range —
        # this is the precedence rule documented in the module docstring.
        assert tp.temp_phrase(23, 29, True, LOCALE) == "in the 20s"


class TestTempPhraseDifferentDecades:
    def test_temp_phrase_wide_spread_across_decades_reports_exact_range(self):
        # Different decades (70s/80s), spread 6 > TEMP_DIFF_THRESHOLD(4).
        assert tp.temp_phrase(76, 82, True, LOCALE) == "76 to 82"

    def test_temp_phrase_narrow_spread_across_decades_uses_max_decade(self):
        # Different decades (70s/80s), spread 3 <= TEMP_DIFF_THRESHOLD(4).
        assert tp.temp_phrase(78, 81, True, LOCALE) == "in the lower 80s"


class TestTempPhraseExceptionTable:
    def test_temp_phrase_zero_crossing_equality_is_near_zero(self):
        assert tp.temp_phrase(0, 0, True, LOCALE) == "near zero"

    def test_temp_phrase_zero_crossing_range_is_zero_to_max(self):
        assert tp.temp_phrase(0, 15, True, LOCALE) == "zero to 15"

    def test_temp_phrase_above_100_exact_equality_is_around_min(self):
        assert tp.temp_phrase(105, 105, True, LOCALE) == "around 105"

    def test_temp_phrase_above_100_range_is_min_to_max(self):
        assert tp.temp_phrase(90, 105, True, LOCALE) == "90 to 105"

    def test_temp_phrase_sub_zero_range_uses_max_to_min_below_zero(self):
        assert tp.temp_phrase(-15, -5, True, LOCALE) == "-5 to -15 below zero"

    def test_temp_phrase_exact_decade_equality_is_around_min(self):
        assert tp.temp_phrase(20, 20, True, LOCALE) == "around 20"


class TestTempDescriptor:
    def test_temp_descriptor_day_very_hot_without_heat_index(self):
        assert tp.temp_descriptor(101, None, None, True, LOCALE) == "very hot"

    def test_temp_descriptor_day_very_hot_and_humid_with_high_heat_index_diff(self):
        assert tp.temp_descriptor(101, 110, None, True, LOCALE) == "very hot and humid"

    def test_temp_descriptor_day_hot_and_humid(self):
        assert tp.temp_descriptor(96, 103, None, True, LOCALE) == "hot and humid"

    def test_temp_descriptor_day_returns_none_for_ordinary_temp(self):
        assert tp.temp_descriptor(70, None, None, True, LOCALE) is None

    def test_temp_descriptor_night_bitterly_cold_requires_both_conditions(self):
        assert tp.temp_descriptor(3, None, -5, False, LOCALE) == "bitterly cold"

    def test_temp_descriptor_night_very_cold_without_wind_chill_data(self):
        # wind_chill is None: a rule referencing it must not match, so the
        # "temp < 5" only rule applies instead of silently passing.
        assert tp.temp_descriptor(3, None, None, False, LOCALE) == "very cold"

    def test_temp_descriptor_night_bitterly_cold_from_wind_chill_alone(self):
        assert tp.temp_descriptor(10, None, -1, False, LOCALE) == "bitterly cold"

    def test_temp_descriptor_day_bitterly_cold_from_wind_chill_alone(self):
        assert tp.temp_descriptor(50, None, -1, True, LOCALE) == "bitterly cold"


class TestTempTrendPhrase:
    def test_temp_trend_phrase_none_trend_returns_none(self):
        assert tp.temp_trend_phrase(None, 75, True, LOCALE) is None

    def test_temp_trend_phrase_daytime_uses_in_the_afternoon(self):
        result = tp.temp_trend_phrase("falling", 75, True, LOCALE)
        assert result == "temperatures falling into the mid 70s in the afternoon"

    def test_temp_trend_phrase_nighttime_uses_after_midnight(self):
        result = tp.temp_trend_phrase("rising", 45, False, LOCALE)
        assert result == "temperatures rising into the mid 40s after midnight"

    def test_temp_trend_phrase_single_digits_decade_word(self):
        result = tp.temp_trend_phrase("falling", 7, True, LOCALE)
        assert result == "temperatures falling into the upper single digits in the afternoon"

    def test_temp_trend_phrase_teens_decade_word(self):
        result = tp.temp_trend_phrase("falling", 15, True, LOCALE)
        assert result == "temperatures falling into the mid teens in the afternoon"

    def test_temp_trend_phrase_teens_below_zero_decade_word(self):
        result = tp.temp_trend_phrase("rising", -5, False, LOCALE)
        assert result == "temperatures rising into the mid teens below zero after midnight"


class TestTempPhraseLocale:
    def test_temp_phrase_non_english_locale_resolves_french_decade_phrase(self):
        assert tp.temp_phrase(83, 89, True, "fr") == "autour de 80"

    def test_temp_trend_phrase_non_english_locale_resolves_french_template(self):
        result = tp.temp_trend_phrase("falling", 75, True, "fr")
        assert result == "températures en baisse vers le milieu des 70 l'après-midi"


if __name__ == "__main__":
    pytest.main([__file__])
