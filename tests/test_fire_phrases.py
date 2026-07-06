"""Unit tests for fire weather phrase resolution (ADR-082, GFE SS18).

Covers weewx_clearskies_api/sse/gfe/fire_phrases.py: humidity_recovery's
MaxRH shortcut and unknown/poor/moderate/good/excellent 24h-diff tiers,
lal_from_weather's thunderstorm-code and coverage-based Lightning
Activity Level heuristic (including the dry-thunderstorm override and
no-thunderstorm floor), smoke_dispersal's VentRate tiers, and
haines_index_phrase's 4 fire-growth-potential levels.

Module under test: weewx_clearskies_api/sse/gfe/fire_phrases.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import fire_phrases as fp

LOCALE = "en"


class TestHumidityRecovery:
    def test_humidity_recovery_maxrh_shortcut_is_excellent(self):
        # MaxRH > 50 is immediately "Excellent" regardless of the 24h-prior
        # reading (even when that reading is unavailable).
        assert fp.humidity_recovery(60, None, LOCALE) == "Excellent"

    def test_humidity_recovery_maxrh_shortcut_ignores_prev_24h_value(self):
        assert fp.humidity_recovery(60, 20, LOCALE) == "Excellent"

    def test_humidity_recovery_unknown_when_prev_24h_missing(self):
        # Below the MaxRH shortcut, no prior reading means the category
        # cannot be computed — "Unknown", not a guess.
        assert fp.humidity_recovery(40, None, LOCALE) == "Unknown"

    def test_humidity_recovery_poor_tier(self):
        assert fp.humidity_recovery(40, 20, LOCALE) == "Poor"

    def test_humidity_recovery_moderate_tier(self):
        assert fp.humidity_recovery(40, 5, LOCALE) == "Moderate"

    def test_humidity_recovery_good_tier(self):
        assert fp.humidity_recovery(40, -20, LOCALE) == "Good"

    def test_humidity_recovery_excellent_tier_from_large_diff(self):
        assert fp.humidity_recovery(40, -50, LOCALE) == "Excellent"


class TestLalFromWeather:
    def test_lal_from_weather_no_thunderstorm_codes_returns_one(self):
        assert fp.lal_from_weather(["R"], None, LOCALE) == 1

    def test_lal_from_weather_empty_codes_returns_one(self):
        assert fp.lal_from_weather([], None, LOCALE) == 1

    def test_lal_from_weather_dry_thunderstorm_overrides_to_six(self):
        assert fp.lal_from_weather(["T:Dry"], None, LOCALE) == 6

    def test_lal_from_weather_isolated_coverage_uses_upper_of_range(self):
        assert fp.lal_from_weather(["T"], "Iso", LOCALE) == 3

    def test_lal_from_weather_patchy_coverage(self):
        assert fp.lal_from_weather(["T"], "Patchy", LOCALE) == 2

    def test_lal_from_weather_scattered_coverage(self):
        assert fp.lal_from_weather(["T"], "Sct", LOCALE) == 4

    def test_lal_from_weather_widespread_coverage(self):
        assert fp.lal_from_weather(["T"], "Wide", LOCALE) == 5

    def test_lal_from_weather_unmapped_coverage_falls_back_to_default(self):
        assert fp.lal_from_weather(["T"], "Unknown", LOCALE) == 2

    def test_lal_from_weather_missing_coverage_falls_back_to_default(self):
        assert fp.lal_from_weather(["T"], None, LOCALE) == 2


class TestSmokeDispersal:
    def test_smoke_dispersal_poor_tier(self):
        assert fp.smoke_dispersal(10_000, LOCALE) == "Poor"

    def test_smoke_dispersal_at_poor_upper_boundary_rolls_to_fair(self):
        assert fp.smoke_dispersal(40_000, LOCALE) == "Fair"

    def test_smoke_dispersal_at_fair_upper_boundary_rolls_to_good(self):
        assert fp.smoke_dispersal(60_000, LOCALE) == "Good"

    def test_smoke_dispersal_at_good_upper_boundary_rolls_to_very_good(self):
        assert fp.smoke_dispersal(100_000, LOCALE) == "Very Good"

    def test_smoke_dispersal_at_very_good_upper_boundary_rolls_to_excellent(self):
        assert fp.smoke_dispersal(150_000, LOCALE) == "Excellent"

    def test_smoke_dispersal_far_above_all_bounded_tiers_is_excellent(self):
        assert fp.smoke_dispersal(500_000, LOCALE) == "Excellent"


class TestHainesIndexPhrase:
    def test_haines_index_phrase_at_very_low_upper_boundary(self):
        assert fp.haines_index_phrase(3, LOCALE) == "Very Low potential for large fire growth"

    def test_haines_index_phrase_at_low_upper_boundary(self):
        assert fp.haines_index_phrase(4, LOCALE) == "Low potential for large fire growth"

    def test_haines_index_phrase_at_moderate_upper_boundary(self):
        assert fp.haines_index_phrase(5, LOCALE) == "Moderate potential for large fire growth"

    def test_haines_index_phrase_at_high_upper_boundary(self):
        assert fp.haines_index_phrase(10, LOCALE) == "High potential for large fire growth"


class TestFirePhrasesLocale:
    def test_humidity_recovery_non_english_locale_resolves_french(self):
        assert fp.humidity_recovery(60, None, "fr") == "Excellent"

    def test_haines_index_phrase_non_english_locale_resolves_french(self):
        result = fp.haines_index_phrase(3, "fr")
        assert result == "Potentiel très faible de propagation rapide du feu"


if __name__ == "__main__":
    pytest.main([__file__])
