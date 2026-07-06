"""Unit tests for GFE current-conditions text composition (ADR-082 T7.2).

Covers weewx_clearskies_api/sse/gfe/composer.py's current-conditions
surface: compose_current_text() / generate_current_text() at the standard
and verbose tiers, plus the private helpers that back them
(_wind_direction_word, _current_sky_label, _current_wind_sentence, unit
rendering via configure()).

Replaces tests/test_text_generator.py, which tested the retired
sse.text_generator module (deleted as part of this round -- ADR-082
decision: text_generator.py and conditions_text.py's generation logic move
to sse/gfe/). Assertions favor the new GFE phrasing (decade phrases,
"gusts to around N" wording, extreme-temperature descriptors) rather than
the old literal "Temperature near N" style -- per ADR-082's consequences
section, assertion updates for improved phrasing are expected and
acceptable.

The terse tier (sse.conditions_text.build_weather_text) is unaffected by
this round and is not covered here.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import composer
from weewx_clearskies_api.sse.gfe import generate_current_text as public_generate_current_text
from weewx_clearskies_api.sse.observation_model import Observation

LOCALE = "en"


@pytest.fixture(autouse=True)
def _reset_unit_system():
    """Ensure composer's module-level unit system resets between tests."""
    yield
    composer.configure("US")


# ===========================================================================
# 1. _wind_direction_word -- 8-point compass, 22.5 degree sectors
# ===========================================================================


class TestWindDirectionWord:
    @pytest.mark.parametrize(
        ("degrees", "expected"),
        [
            (0, "North"),
            (45, "Northeast"),
            (90, "East"),
            (135, "Southeast"),
            (180, "South"),
            (225, "Southwest"),
            (270, "West"),
            (315, "Northwest"),
            (359, "North"),
            (360, "North"),
            (22, "North"),
            (23, "Northeast"),
        ],
    )
    def test_eight_point_compass_word(self, degrees, expected):
        assert composer._wind_direction_word(degrees) == expected

    def test_none_degrees_returns_none(self):
        assert composer._wind_direction_word(None) is None


# ===========================================================================
# 2. _current_sky_label -- day/night mapping + GFE bucket fallback
# ===========================================================================


class TestCurrentSkyLabel:
    def test_clear_daytime_maps_to_sunny(self):
        obs = Observation(sky_label="Clear", is_daytime=True)
        assert composer._current_sky_label(obs, LOCALE) == "Sunny"

    def test_mostly_clear_daytime_maps_to_mostly_sunny(self):
        obs = Observation(sky_label="Mostly Clear", is_daytime=True)
        assert composer._current_sky_label(obs, LOCALE) == "Mostly Sunny"

    def test_clear_nighttime_stays_clear(self):
        obs = Observation(sky_label="Clear", is_daytime=False)
        assert composer._current_sky_label(obs, LOCALE) == "Clear"

    def test_overcast_passthrough(self):
        obs = Observation(sky_label="Overcast", is_daytime=True)
        assert composer._current_sky_label(obs, LOCALE) == "Overcast"

    def test_sky_label_takes_priority_over_cloud_cover(self):
        obs = Observation(sky_label="Overcast", cloud_cover_pct=3.0, is_daytime=True)
        assert composer._current_sky_label(obs, LOCALE) == "Overcast"

    def test_gfe_bucket_fallback_low_cloud_cover_daytime(self):
        """No sky_label, cloud_cover_pct=3% -> GFE bucket <5% -> 'Sunny'."""
        obs = Observation(cloud_cover_pct=3.0, is_daytime=True)
        assert composer._current_sky_label(obs, LOCALE) == "Sunny"

    def test_gfe_bucket_fallback_low_cloud_cover_nighttime(self):
        obs = Observation(cloud_cover_pct=3.0, is_daytime=False)
        assert composer._current_sky_label(obs, LOCALE) == "Clear"

    def test_no_sky_data_returns_none(self):
        obs = Observation()
        assert composer._current_sky_label(obs, LOCALE) is None


# ===========================================================================
# 3. compose_current_text -- standard tier
# ===========================================================================


class TestComposeCurrentStandard:
    def test_full_observation_produces_decade_phrase_and_wind(self):
        obs = Observation(
            sky_label="Clear",
            is_daytime=True,
            temperature=72.0,
            wind_speed=8.0,
            wind_direction=180.0,
        )
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert result == "Sunny. Temperature in the lower 70s. South winds around 8 mph."

    def test_sky_with_precipitation_label(self):
        obs = Observation(
            sky_label="Mostly Cloudy",
            precipitation_label="Light Rain",
            is_daytime=True,
            temperature=60.0,
        )
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert result.startswith("Mostly Cloudy with Light Rain.")

    def test_fog_state_produces_separate_sentence(self):
        obs = Observation(
            sky_label="Overcast",
            fog_mist_state="Foggy",
            is_daytime=True,
            temperature=65.0,
            wind_speed=3.0,
        )
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Foggy." in result

    def test_haze_detected_produces_hazy_sentence(self):
        obs = Observation(
            sky_label="Partly Cloudy",
            haze_detected=True,
            is_daytime=True,
            temperature=85.0,
        )
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Hazy." in result

    def test_fog_and_haze_fog_prioritized_no_hazy_sentence(self):
        obs = Observation(
            sky_label="Overcast",
            fog_mist_state="Foggy",
            haze_detected=True,
            is_daytime=True,
            temperature=62.0,
        )
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Foggy." in result
        assert "Hazy." not in result

    def test_calm_winds_below_null_threshold(self):
        obs = Observation(temperature=70.0, wind_speed=3.0, wind_direction=270.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Calm winds." in result

    def test_wind_at_exactly_null_threshold_is_not_calm(self):
        obs = Observation(temperature=70.0, wind_speed=5.0, wind_direction=0.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Calm winds." not in result
        assert "around 5 mph." in result

    def test_no_wind_data_omits_wind_sentence(self):
        obs = Observation(sky_label="Clear", is_daytime=True, temperature=72.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "winds" not in result
        assert "Calm" not in result

    def test_no_data_at_all_returns_empty_string(self):
        assert composer.compose_current_text(Observation(), "standard", LOCALE) == ""

    def test_extreme_heat_produces_descriptor_sentence(self):
        """temp > 99 -> 'very hot' extreme descriptor (EXTREME_TEMP_DESCRIPTORS)."""
        obs = Observation(temperature=105.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Very hot." in result

    def test_moderate_temp_omits_extreme_descriptor(self):
        obs = Observation(temperature=72.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "hot" not in result.lower()
        assert "cold" not in result.lower()

    def test_wind_gust_exceeding_threshold_uses_gfe_phrasing(self):
        """gust - speed > 10 mph -> 'with gusts to around N mph' (ADR-082 upgrade)."""
        obs = Observation(wind_speed=15.0, wind_direction=270.0, wind_gust=30.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "with gusts to around 30 mph." in result

    def test_wind_gust_within_threshold_is_not_reported(self):
        obs = Observation(wind_speed=15.0, wind_direction=270.0, wind_gust=24.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "gusts" not in result

    def test_wind_without_direction_omits_direction_word(self):
        obs = Observation(wind_speed=10.0, wind_direction=None)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Winds around 10 mph." in result

    def test_partly_cloudy_daytime_unchanged(self):
        """Matches the preserved terse-tier day-mapping (no Partly Sunny variant)."""
        obs = Observation(sky_label="Partly Cloudy", is_daytime=True)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert result == "Partly Cloudy."

    def test_temperature_at_exact_decade_uses_around_phrasing(self):
        """GFE exception table: exact round-decade value -> 'around N', not decade phrasing."""
        obs = Observation(temperature=60.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "Temperature around 60." in result


# ===========================================================================
# 4. compose_current_text -- verbose tier
# ===========================================================================


class TestComposeCurrentVerbose:
    def test_full_observation_sunny_daytime(self):
        obs = Observation(
            sky_label="Clear",
            is_daytime=True,
            temperature=72.0,
            dewpoint=55.0,
            wind_speed=8.0,
            wind_direction=180.0,
        )
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "Currently in the lower 70s" in result
        assert "sunny skies" in result
        assert "Dew point in the mid 50s." in result
        assert "South winds around 8 mph." in result

    def test_fog_narrative_overrides_sky_in_opening(self):
        obs = Observation(fog_mist_state="Foggy", is_daytime=True, temperature=65.0)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "with fog limiting visibility" in result

    def test_mist_narrative_in_opening(self):
        obs = Observation(fog_mist_state="Misty", is_daytime=False, temperature=58.0)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "with mist" in result

    def test_haze_plus_clear_daytime_produces_hazy_sunshine(self):
        obs = Observation(sky_label="Clear", haze_detected=True, is_daytime=True, temperature=85.0)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "under hazy sunshine" in result

    def test_haze_plus_clear_nighttime_produces_hazy_skies(self):
        obs = Observation(
            sky_label="Clear", haze_detected=True, is_daytime=False, temperature=70.0
        )
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "under hazy skies" in result

    def test_wind_gust_upgraded_phrasing(self):
        obs = Observation(
            temperature=70.0, wind_speed=15.0, wind_direction=270.0, wind_gust=30.0
        )
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "with gusts to around 30 mph." in result

    def test_wind_gust_not_appended_when_not_significant(self):
        obs = Observation(
            temperature=70.0, wind_speed=15.0, wind_direction=270.0, wind_gust=24.0
        )
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "gusts" not in result

    def test_minimal_data_temperature_only(self):
        obs = Observation(temperature=72.0)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert result == "Currently in the lower 70s."

    def test_no_data_returns_empty_string(self):
        assert composer.compose_current_text(Observation(), "verbose", LOCALE) == ""

    def test_no_dewpoint_omits_dew_point_sentence(self):
        obs = Observation(temperature=75.0, dewpoint=None)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "Dew point" not in result

    def test_extreme_cold_produces_descriptor_sentence(self):
        """temp < 20 (daytime) -> 'very cold' extreme descriptor."""
        obs = Observation(temperature=10.0, is_daytime=True)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "Very cold." in result

    def test_calm_winds_in_verbose(self):
        obs = Observation(temperature=70.0, wind_speed=2.0)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "Calm winds." in result

    def test_overcast_sky_in_verbose_opening(self):
        obs = Observation(sky_label="Overcast", is_daytime=True, temperature=50.0)
        result = composer.compose_current_text(obs, "verbose", LOCALE)
        assert "overcast skies" in result


# ===========================================================================
# 5. Render-time unit conversion (ADR-082 T7.2 lead decision, 2026-07-06)
# ===========================================================================


class TestUnitAwareRendering:
    def test_metric_converts_temperature_decade_phrase(self):
        """85F -> ~29.4C -> decade phrasing runs on the converted value."""
        composer.configure("METRIC")
        obs = Observation(temperature=85.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "in the upper 20s" in result

    def test_metric_converts_wind_speed_and_unit_label(self):
        composer.configure("METRIC")
        obs = Observation(wind_speed=15.0, wind_direction=180.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "South winds around 24 km/h." in result

    def test_metricwx_converts_wind_speed_and_unit_label(self):
        composer.configure("METRICWX")
        obs = Observation(wind_speed=15.0, wind_direction=180.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "m/s" in result
        assert "mph" not in result

    def test_extreme_descriptor_threshold_unaffected_by_unit_conversion(self):
        """temp_descriptor evaluates the raw F value, not the converted display value.

        85F (~29C, unremarkable) must NOT trigger a cold/hot descriptor just
        because 29 happens to look small once rendered in Celsius.
        """
        composer.configure("METRIC")
        obs = Observation(temperature=85.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "cold" not in result.lower()
        assert "hot" not in result.lower()

    def test_us_default_uses_mph(self):
        obs = Observation(wind_speed=15.0, wind_direction=180.0)
        result = composer.compose_current_text(obs, "standard", LOCALE)
        assert "mph" in result


# ===========================================================================
# 6. Public API surface (sse.gfe.generate_current_text / configure)
# ===========================================================================


class TestPublicApi:
    def test_generate_current_text_standard_via_public_api(self):
        obs = Observation(temperature=72.0)
        result = public_generate_current_text(obs, "standard", LOCALE)
        assert result == "Temperature in the lower 70s."

    def test_generate_current_text_verbose_via_public_api(self):
        obs = Observation(temperature=72.0)
        result = public_generate_current_text(obs, "verbose", LOCALE)
        assert result == "Currently in the lower 70s."

    def test_unsupported_verbosity_raises_value_error(self):
        with pytest.raises(ValueError, match="unsupported verbosity"):
            public_generate_current_text(Observation(), "terse", LOCALE)
