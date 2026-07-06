"""Unit tests for the GFE forecast text composition engine (ADR-082 T5.1/T5.2).

Covers weewx_clearskies_api/sse/gfe/composer.py: the single-pass sequential
phrase assembly that turns a ForecastPeriod into one NWS-style forecast
sentence, plus the NWS pass-through contract and the public API surface
re-exported from weewx_clearskies_api/sse/gfe/__init__.py.

Assertions favor substring checks over exact string equality (per the T5.3
task brief) since phrase wording may evolve as locale JSON files are
populated; the underlying threshold values (POP_SKY_LOWER_THRESHOLD=55,
POP_SNOW_LOWER_THRESHOLD=60, WIND_GUST_DIFFERENCE=10, etc.) are locked down
separately in test_gfe_thresholds.py.

Module under test: weewx_clearskies_api/sse/gfe/composer.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.forecast_model import ForecastPeriod
from weewx_clearskies_api.sse.gfe import composer
from weewx_clearskies_api.sse.gfe import generate_forecast_text as public_generate_forecast_text

LOCALE = "en"


class TestFullDayPeriod:
    def test_full_day_period_contains_all_phrase_groups(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=75,
            sky_percent=20,
            pop=30,
            precip_type="rain",
            wind_speed_min=5,
            wind_speed_max=10,
            wind_direction=180,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "Sunny" in result
        assert "Highs" in result
        assert "mph" in result
        assert "rain" in result.lower()
        assert result.endswith(".")


class TestFullNightPeriod:
    def test_full_night_period_uses_lows_not_highs(self):
        period = ForecastPeriod(
            period_label="Tonight",
            is_daytime=False,
            temp_low=45,
            sky_percent=90,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "Lows" in result
        assert "Highs" not in result
        assert "Cloudy" in result


class TestMissingElements:
    def test_only_temperature_populated_produces_only_temp_phrase(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=72,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert result == "Highs in the lower 70s."


class TestSkySuppressionByPop:
    def test_high_pop_suppresses_sky_phrase(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=75,
            sky_percent=20,
            pop=60,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "Sunny" not in result
        assert "Highs" in result

    def test_low_pop_does_not_suppress_sky_phrase(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=75,
            sky_percent=20,
            pop=30,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "Sunny" in result


class TestNwsPassthrough:
    def test_passthrough_returns_input_unchanged(self):
        narrative = "Sunny with highs near 80."
        assert composer.compose_nws_passthrough(narrative) == narrative

    def test_passthrough_returns_none_for_none(self):
        assert composer.compose_nws_passthrough(None) is None


class TestWindWithGust:
    def test_gust_exceeding_difference_threshold_is_reported(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            wind_speed_min=15,
            wind_speed_max=20,
            wind_gust=32,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "gusts to around 32 mph" in result

    def test_gust_within_difference_threshold_is_not_reported(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            wind_speed_min=15,
            wind_speed_max=20,
            wind_gust=25,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "gusts" not in result


class TestSnowAccumulation:
    def test_snow_amount_with_sufficient_pop_produces_snow_phrase(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            snow_amount=2,
            pop=70,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "inches" in result.lower()

    def test_snow_amount_with_insufficient_pop_omits_snow_phrase(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            snow_amount=2,
            pop=30,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "inches" not in result.lower()


class TestTemperatureTrend:
    def test_falling_trend_produces_trend_phrase(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=75,
            temp_trend="falling",
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "falling" in result.lower()


class TestExtremeTemperature:
    def test_temp_above_99_produces_extreme_descriptor(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=105,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "hot" in result.lower()

    def test_temp_below_99_omits_extreme_descriptor(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=75,
        )

        result = composer.compose_forecast_text(period, LOCALE)

        assert "hot" not in result.lower()


class TestDegreesToCardinal:
    @pytest.mark.parametrize(
        ("degrees", "expected"),
        [
            (0, "N"),
            (45, "NE"),
            (90, "E"),
            (135, "SE"),
            (180, "S"),
            (225, "SW"),
            (270, "W"),
            (315, "NW"),
            (360, "N"),
        ],
    )
    def test_eight_point_compass_conversion(self, degrees, expected):
        assert composer._degrees_to_cardinal(degrees) == expected

    def test_none_degrees_returns_none(self):
        assert composer._degrees_to_cardinal(None) is None


class TestPublicApi:
    def test_generate_forecast_text_via_public_api(self):
        period = ForecastPeriod(
            period_label="Today",
            is_daytime=True,
            temp_high=75,
        )

        result = public_generate_forecast_text(period, LOCALE)

        assert result == "Highs in the mid 70s."
