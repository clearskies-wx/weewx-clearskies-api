"""Unit tests for marine wave and chop phrase resolution (ADR-082, GFE SS17).

Covers weewx_clearskies_api/sse/gfe/marine_phrases.py: wave_height_phrase's
nearest-sample-point lookup across all 10 WAVE_HEIGHT_RANGES entries
(a sparse table, not contiguous bins), and chop_phrase's 7-category
inclusive-upper-bound ladder including the open-ended (math.inf) final
category.

Module under test: weewx_clearskies_api/sse/gfe/marine_phrases.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import marine_phrases as mp

LOCALE = "en"


class TestWaveHeightPhraseAllSamplePoints:
    @pytest.mark.parametrize(
        ("avg_ft", "expected"),
        [
            (0, "less than 1 foot"),
            (1, "1 foot or less"),
            (1.5, "1 to 2 feet"),
            (2, "1 to 3 feet"),
            (3, "2 to 4 feet"),
            (5, "3 to 6 feet"),
            (8, "6 to 10 feet"),
            (12, "10 to 14 feet"),
            (20, "15 to 20 feet"),
            (100, "over 20 feet"),
        ],
    )
    def test_wave_height_phrase_at_each_documented_sample_point(self, avg_ft, expected):
        assert mp.wave_height_phrase(avg_ft, LOCALE) == expected


class TestWaveHeightPhraseNearestPointSelection:
    def test_wave_height_phrase_between_two_points_picks_nearer_lower_point(self):
        # 1.25 is nearer to the 1.0 sample point (dist 0.25) than 1.5 (dist 0.25)
        # — a tie resolves to whichever the table lists first.
        assert mp.wave_height_phrase(1.25, LOCALE) == "1 foot or less"

    def test_wave_height_phrase_far_gap_picks_nearer_of_two_distant_points(self):
        # 60 is closer to the 20 sample point than to the 100 sample point.
        assert mp.wave_height_phrase(60, LOCALE) == "15 to 20 feet"

    def test_wave_height_phrase_negative_input_still_resolves_nearest(self):
        assert mp.wave_height_phrase(-5, LOCALE) == "less than 1 foot"


class TestChopPhraseCategoryBoundaries:
    def test_chop_phrase_at_smooth_upper_boundary_7_knots(self):
        assert mp.chop_phrase(7, LOCALE) == "smooth"

    def test_chop_phrase_just_above_smooth_boundary_is_light_chop(self):
        assert mp.chop_phrase(7.1, LOCALE) == "light chop"

    def test_chop_phrase_at_light_chop_upper_boundary_12_knots(self):
        assert mp.chop_phrase(12, LOCALE) == "light chop"

    def test_chop_phrase_at_moderate_chop_upper_boundary_17_knots(self):
        assert mp.chop_phrase(17, LOCALE) == "moderate chop"

    def test_chop_phrase_at_choppy_upper_boundary_22_knots(self):
        assert mp.chop_phrase(22, LOCALE) == "choppy"

    def test_chop_phrase_at_rough_upper_boundary_27_knots(self):
        assert mp.chop_phrase(27, LOCALE) == "rough"

    def test_chop_phrase_at_very_rough_upper_boundary_32_knots(self):
        assert mp.chop_phrase(32, LOCALE) == "very rough"

    def test_chop_phrase_above_all_bounded_categories_is_extremely_rough(self):
        assert mp.chop_phrase(50, LOCALE) == "extremely rough"

    def test_chop_phrase_at_calm_zero_knots(self):
        assert mp.chop_phrase(0, LOCALE) == "smooth"


class TestMarinePhrasesLocale:
    def test_wave_height_phrase_non_english_locale_resolves_french(self):
        assert mp.wave_height_phrase(0, "fr") == "moins de 1 pied"

    def test_chop_phrase_non_english_locale_resolves_french(self):
        assert mp.chop_phrase(0, "fr") == "calme"


if __name__ == "__main__":
    pytest.main([__file__])
