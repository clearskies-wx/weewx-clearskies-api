"""Unit tests for GFE threshold tables (ADR-082).

Covers every threshold table/constant in weewx_clearskies_api/sse/gfe/thresholds.py.
These tables are ported faithfully from the NWS Graphical Forecast Editor (GFE)
text formatter source; the tests here lock in exact values so an accidental
edit (rounding, "improving" a threshold) is caught immediately.

Module under test: weewx_clearskies_api/sse/gfe/thresholds.py
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from weewx_clearskies_api.sse.gfe import thresholds as t


class TestSkyThresholds:
    def test_sky_value_list_has_six_entries(self):
        assert len(t.SKY_VALUE_LIST) == 6

    def test_sky_value_list_boundaries(self):
        boundaries = [bucket.upper_pct for bucket in t.SKY_VALUE_LIST]
        assert boundaries == [5, 25, 50, 69, 87, 100]

    def test_sky_value_list_first_entry(self):
        first = t.SKY_VALUE_LIST[0]
        assert first.day_key == "sunny"
        assert first.night_key == "clear"

    def test_sky_value_list_last_entry(self):
        last = t.SKY_VALUE_LIST[-1]
        assert last.day_key == "cloudy"
        assert last.night_key == "cloudy"

    def test_similar_sky_words_day_has_four_pairs(self):
        assert len(t.SIMILAR_SKY_WORDS_DAY) == 4

    def test_similar_sky_words_night_has_three_pairs(self):
        assert len(t.SIMILAR_SKY_WORDS_NIGHT) == 3

    def test_pop_sky_lower_threshold(self):
        assert t.POP_SKY_LOWER_THRESHOLD == 55


class TestTemperatureThresholds:
    def test_temp_diff_threshold(self):
        assert t.TEMP_DIFF_THRESHOLD == 4

    def test_temp_boundary_dict_has_all_ten_digits(self):
        assert set(t.TEMP_BOUNDARY_DICT.keys()) == set(range(10))

    def test_temp_boundary_dict_lower_digits(self):
        for digit in (0, 1, 2, 3):
            assert t.TEMP_BOUNDARY_DICT[digit] == "lower"

    def test_temp_boundary_dict_mid_digits(self):
        for digit in (4, 5, 6):
            assert t.TEMP_BOUNDARY_DICT[digit] == "mid"

    def test_temp_boundary_dict_upper_digits(self):
        for digit in (7, 8, 9):
            assert t.TEMP_BOUNDARY_DICT[digit] == "upper"

    def test_temp_exception_table_total_row_count(self):
        # 7 base rows + 8 decade rows (20-90) = 15 total.
        assert len(t.TEMP_EXCEPTION_TABLE) == 15

    def test_temp_exception_table_at_least_eight_rows(self):
        assert len(t.TEMP_EXCEPTION_TABLE) >= 8

    def test_temp_exception_table_zero_crossing_row_exists(self):
        zero_crossing_rows = [
            row
            for row in t.TEMP_EXCEPTION_TABLE
            if row.min_bounds == (0, 0) and row.max_bounds == (0, 29)
        ]
        assert len(zero_crossing_rows) == 1
        assert zero_crossing_rows[0].equality_key == "near_zero"
        assert zero_crossing_rows[0].range_key == "zero_to_max"

    def test_temp_exception_table_decade_rows_present(self):
        decades = (20, 30, 40, 50, 60, 70, 80, 90)
        decade_rows = [
            row
            for row in t.TEMP_EXCEPTION_TABLE
            if row.min_bounds == row.max_bounds and row.min_bounds[0] in decades
        ]
        assert len(decade_rows) == 8

    def test_temp_trend_threshold(self):
        assert t.TEMP_TREND_THRESHOLD == 20

    def test_steady_temp_threshold(self):
        assert t.STEADY_TEMP_THRESHOLD == 4

    def test_extreme_temp_descriptors_has_day_and_night_keys(self):
        assert set(t.EXTREME_TEMP_DESCRIPTORS.keys()) == {"day", "night"}

    def test_extreme_temp_descriptors_day_rule_count(self):
        assert len(t.EXTREME_TEMP_DESCRIPTORS["day"]) == 8

    def test_extreme_temp_descriptors_night_rule_count(self):
        assert len(t.EXTREME_TEMP_DESCRIPTORS["night"]) == 3


class TestWindThresholds:
    def test_wind_null_threshold(self):
        assert t.WIND_NULL_THRESHOLD == 5

    def test_wind_gust_difference(self):
        assert t.WIND_GUST_DIFFERENCE == 10

    def test_wind_summary_descriptors_entry_count(self):
        assert len(t.WIND_SUMMARY_DESCRIPTORS) == 6

    def test_wind_summary_descriptors_first_entry(self):
        threshold, descriptor = t.WIND_SUMMARY_DESCRIPTORS[0]
        assert threshold == 25
        assert descriptor == ""

    def test_wind_summary_descriptors_last_entry(self):
        _, descriptor = t.WIND_SUMMARY_DESCRIPTORS[-1]
        assert descriptor == "hurricane_force_winds"

    def test_marine_wind_descriptors_entry_count(self):
        assert len(t.MARINE_WIND_DESCRIPTORS) == 3

    def test_marine_wind_descriptors_thresholds(self):
        thresholds = [threshold for threshold, _ in t.MARINE_WIND_DESCRIPTORS]
        assert thresholds == [34, 45, 64]


class TestWeatherTypeThresholds:
    def test_wx_type_hierarchy_entry_count(self):
        assert len(t.WX_TYPE_HIERARCHY) == 22

    def test_wx_type_hierarchy_first_entry(self):
        assert t.WX_TYPE_HIERARCHY[0] == "WP"

    def test_wx_type_hierarchy_thunderstorms_after_rain(self):
        index = t.WX_TYPE_HIERARCHY.index("T")
        assert index == 3, "T must follow WP, R, RW per GFE SS4.1 row-major order"

    def test_wx_coverage_hierarchy_entry_count(self):
        assert len(t.WX_COVERAGE_HIERARCHY) == 16

    def test_wx_coverage_hierarchy_first_entry(self):
        assert t.WX_COVERAGE_HIERARCHY[0] == "Def"

    def test_wx_coverage_hierarchy_last_entry(self):
        assert t.WX_COVERAGE_HIERARCHY[-1] == "Patchy"

    def test_wx_intensity_codes_entry_count(self):
        assert len(t.WX_INTENSITY_CODES) == 4

    def test_wx_intensity_codes_heavy(self):
        assert t.WX_INTENSITY_CODES["+"] == "heavy"

    def test_wx_intensity_codes_very_light(self):
        assert t.WX_INTENSITY_CODES["--"] == "very_light"

    def test_wx_pop_related_types_entry_count(self):
        assert len(t.WX_POP_RELATED_TYPES) == 7

    def test_wx_pop_related_types_includes_expected(self):
        assert "R" in t.WX_POP_RELATED_TYPES
        assert "S" in t.WX_POP_RELATED_TYPES
        assert "T" in t.WX_POP_RELATED_TYPES


class TestPopThresholds:
    def test_pop_lower_threshold(self):
        assert t.POP_LOWER_THRESHOLD == 15

    def test_pop_lower_threshold_extended(self):
        assert t.POP_LOWER_THRESHOLD_EXTENDED == 25

    def test_pop_wx_lower_threshold(self):
        assert t.POP_WX_LOWER_THRESHOLD == 20

    def test_pop_to_coverage_table_covers_0_to_100(self):
        assert t.POP_TO_COVERAGE_TABLE[0].lower_pct == 0
        assert t.POP_TO_COVERAGE_TABLE[-1].upper_pct == 100
        # Bands must be contiguous with no gaps.
        for previous, current in pairwise(t.POP_TO_COVERAGE_TABLE):
            assert current.lower_pct == previous.upper_pct + 1

    def test_pop_25_maps_to_chc_and_sct(self):
        band = next(
            row for row in t.POP_TO_COVERAGE_TABLE if row.lower_pct <= 25 <= row.upper_pct
        )
        assert band.pop_related_coverage == "Chc"
        assert band.areal_coverage == "Sct"

    def test_pop_snow_lower_threshold(self):
        assert t.POP_SNOW_LOWER_THRESHOLD == 60


class TestSnowAndIceThresholds:
    def test_snow_accumulation_tiers_count(self):
        assert len(t.SNOW_ACCUMULATION_TIERS) == 5

    def test_snow_accumulation_tiers_first_phrase(self):
        assert t.SNOW_ACCUMULATION_TIERS[0].phrase_key == "no_accumulation"

    def test_ice_accumulation_tiers_count(self):
        assert len(t.ICE_ACCUMULATION_TIERS) == 7

    def test_ice_accumulation_tiers_first_phrase(self):
        assert t.ICE_ACCUMULATION_TIERS[0].phrase_key == "less_than_one_quarter"


class TestMarineThresholds:
    def test_wave_height_ranges_count(self):
        assert len(t.WAVE_HEIGHT_RANGES) == 10

    def test_wave_height_ranges_first_entry(self):
        assert t.WAVE_HEIGHT_RANGES[0].phrase_key == "less_than_1_foot"

    def test_chop_categories_count(self):
        assert len(t.CHOP_CATEGORIES) == 7

    def test_chop_categories_first_entry(self):
        assert t.CHOP_CATEGORIES[0].phrase_key == "smooth"

    def test_chop_categories_last_entry(self):
        last = t.CHOP_CATEGORIES[-1]
        assert last.phrase_key == "extremely_rough"
        assert last.upper_kt == math.inf

    def test_marine_seas_threshold(self):
        assert t.MARINE_SEAS_THRESHOLD == 34


class TestFireWeatherThresholds:
    def test_smoke_dispersal_categories_count(self):
        assert len(t.SMOKE_DISPERSAL_CATEGORIES) == 5

    def test_smoke_dispersal_categories_first_entry(self):
        assert t.SMOKE_DISPERSAL_CATEGORIES[0].phrase_key == "poor"

    def test_haines_index_categories_count(self):
        assert len(t.HAINES_INDEX_CATEGORIES) == 4

    def test_humidity_recovery_categories_count(self):
        assert len(t.HUMIDITY_RECOVERY_CATEGORIES) == 4

    def test_humidity_recovery_maxrh_shortcut(self):
        assert t.HUMIDITY_RECOVERY_MAXRH_SHORTCUT == 50

    def test_lal_levels_count(self):
        assert len(t.LAL_LEVELS) == 6


class TestConfigurationThresholds:
    def test_coverage_weights_count(self):
        assert len(t.COVERAGE_WEIGHTS) == 16

    def test_coverage_weights_def(self):
        assert t.COVERAGE_WEIGHTS["Def"] == 6

    def test_coverage_weights_patchy(self):
        assert t.COVERAGE_WEIGHTS["Patchy"] == 1

    def test_null_values_wind(self):
        assert t.NULL_VALUES["wind"] == 5

    def test_null_values_wind_gust(self):
        assert t.NULL_VALUES["wind_gust"] == 20

    def test_rounding_increments_wind(self):
        assert t.ROUNDING_INCREMENTS["wind"] == 5

    def test_rounding_increments_pop(self):
        assert t.ROUNDING_INCREMENTS["pop"] == 10


if __name__ == "__main__":
    pytest.main([__file__])
