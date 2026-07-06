"""Unit tests for weather/precipitation phrase generation (ADR-082, GFE SS4).

Covers weewx_clearskies_api/sse/gfe/wx_phrases.py: type + coverage +
intensity composition (weather_phrase), PoP-based suppression for
PoP-related types, special type descriptors (flurries/sprinkles), heavy
rain/snow intensity suppression, weather-phrase joining with the serial
comma (combine_weather_phrases), PoP qualification phrasing
(pop_qualification), heavy precipitation descriptors (heavy_precip_phrase),
and the French gender-inflection wiring (_type_word/_coverage_word via
i18n.t_inflected).

Module under test: weewx_clearskies_api/sse/gfe/wx_phrases.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import wx_phrases as wx

LOCALE = "en"


class TestWeatherPhraseBasicComposition:
    def test_weather_phrase_rain_with_chance_coverage_and_light_intensity(self):
        assert wx.weather_phrase("R", "Chc", "-", 50, LOCALE) == "chance of light rain"

    def test_weather_phrase_snow_with_likely_coverage(self):
        assert wx.weather_phrase("S", "Lkly", "+", 80, LOCALE) == "likely snow"

    def test_weather_phrase_thunderstorms_with_scattered_coverage(self):
        result = wx.weather_phrase("T", "Sct", "m", 60, LOCALE)
        assert result == "scattered moderate thunderstorms"


class TestWeatherPhrasePopSuppression:
    def test_weather_phrase_pop_related_type_below_threshold_is_suppressed(self):
        # "R" is in WX_POP_RELATED_TYPES; POP_WX_LOWER_THRESHOLD is 20.
        assert wx.weather_phrase("R", "Chc", "-", 10, LOCALE) == ""

    def test_weather_phrase_non_pop_related_type_ignores_low_pop(self):
        # "F" (fog) is not PoP-related, so a low pop value does not suppress it.
        result = wx.weather_phrase("F", "Patchy", "-", 10, LOCALE)
        assert result == "patchy light fog"

    def test_weather_phrase_pop_related_type_at_threshold_is_not_suppressed(self):
        result = wx.weather_phrase("R", "Chc", "-", 20, LOCALE)
        assert result != ""


class TestWeatherPhraseSpecialDescriptors:
    def test_weather_phrase_very_light_snow_showers_becomes_flurries(self):
        assert wx.weather_phrase("SW", "Chc", "--", 50, LOCALE) == "chance of flurries"

    def test_weather_phrase_very_light_rain_showers_becomes_sprinkles(self):
        assert wx.weather_phrase("RW", "Chc", "--", 50, LOCALE) == "chance of sprinkles"


class TestWeatherPhraseIntensitySuppression:
    def test_weather_phrase_heavy_rain_suppresses_inline_intensity_word(self):
        # "R" + "+" is in _HEAVY_INLINE_SUPPRESSED: no "heavy" appears inline
        # (heavy_precip_phrase carries that separately).
        result = wx.weather_phrase("R", "Def", "+", 90, LOCALE)
        assert result == "definite rain"
        assert "heavy" not in result

    def test_weather_phrase_heavy_snow_suppresses_inline_intensity_word(self):
        assert wx.weather_phrase("S", "Def", "+", 90, LOCALE) == "definite snow"

    def test_weather_phrase_shower_type_suppresses_intensity_regardless_of_level(self):
        # Shower types (RW/SW) always suppress inline intensity, not just "+".
        result = wx.weather_phrase("RW", "Def", "+", 90, LOCALE)
        assert result == "definite rain showers"


class TestCombineWeatherPhrases:
    def test_combine_weather_phrases_single_item_returned_unchanged(self):
        assert wx.combine_weather_phrases(["rain"], LOCALE) == "rain"

    def test_combine_weather_phrases_two_items_joined_with_and(self):
        assert wx.combine_weather_phrases(["rain", "snow"], LOCALE) == "rain and snow"

    def test_combine_weather_phrases_three_items_use_serial_comma(self):
        result = wx.combine_weather_phrases(["rain", "snow", "freezing rain"], LOCALE)
        assert result == "rain, snow, and freezing rain"

    def test_combine_weather_phrases_drops_empty_suppressed_items(self):
        result = wx.combine_weather_phrases(["rain", "", "snow"], LOCALE)
        assert result == "rain and snow"

    def test_combine_weather_phrases_empty_list_returns_empty_string(self):
        assert wx.combine_weather_phrases([], LOCALE) == ""


class TestPopQualification:
    def test_pop_qualification_below_threshold_returns_none(self):
        assert wx.pop_qualification(10, "R", LOCALE) is None

    def test_pop_qualification_rain_type_specific_wording(self):
        assert wx.pop_qualification(30, "R", LOCALE) == "chance of rain 30 percent"

    def test_pop_qualification_unlisted_type_falls_back_to_precipitation(self):
        assert wx.pop_qualification(30, "IP", LOCALE) == "chance of precipitation 30 percent"


class TestHeavyPrecipPhrase:
    def test_heavy_precip_phrase_rain(self):
        assert wx.heavy_precip_phrase("R", "+", LOCALE) == "rain may be heavy at times"

    def test_heavy_precip_phrase_snow(self):
        assert wx.heavy_precip_phrase("S", "+", LOCALE) == "snow may be heavy at times"

    def test_heavy_precip_phrase_non_heavy_intensity_returns_none(self):
        assert wx.heavy_precip_phrase("R", "-", LOCALE) is None

    def test_heavy_precip_phrase_type_without_descriptor_returns_none(self):
        # Fog has no heavy-precip descriptor group.
        assert wx.heavy_precip_phrase("F", "+", LOCALE) is None


class TestWeatherPhraseGenderInflectionFrench:
    def test_weather_phrase_french_rain_uses_feminine_singular_inflection(self):
        # "R" (rain) is gender-coded FS for French; "chance_of"/"light" both
        # resolve to their feminine-singular forms via t_inflected.
        result = wx.weather_phrase("R", "Chc", "-", 50, "fr")
        assert result == "risque de légère pluie"

    def test_type_word_french_resolves_plain_string_regardless_of_gender(self):
        # wx.type.* values are plain strings (not gender dicts) even for
        # gendered locales; the type word itself doesn't inflect.
        assert wx._type_word("R", "fr") == "pluie"

    def test_coverage_word_french_masculine_thunderstorm(self):
        # "T" (thunderstorms) is gender-coded MP for French.
        result = wx.weather_phrase("T", "Sct", "m", 60, "fr")
        assert "orages" in result


if __name__ == "__main__":
    pytest.main([__file__])
