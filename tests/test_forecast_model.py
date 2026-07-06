"""Unit tests for the structured forecast period model (ADR-070 T2.2).

Covers:
  Group 1 — instantiation with only the required fields (period_label, is_daytime).
  Group 2 — instantiation with every field populated, verifying round-trip values.
  Group 3 — weather_codes default: empty list, and independent per instance.
  Group 4 — dataclass field shape: names and count via dataclasses.fields().

Module under test: weewx_clearskies_api/sse/forecast_model.py (ForecastPeriod).
"""

from __future__ import annotations

import dataclasses

import pytest

from weewx_clearskies_api.sse.forecast_model import ForecastPeriod

# Fields expected on ForecastPeriod, in declaration order. NOTE: the T2.3 task
# brief stated an expected count of 21, but the actual source (verified via
# dataclasses.fields()) has 22 fields. This list reflects the real, current
# dataclass shape — flagged to the lead per divergence protocol rather than
# silently trimmed to match the brief.
_EXPECTED_FIELD_NAMES = [
    "period_label",
    "is_daytime",
    "temp_high",
    "temp_low",
    "sky_label",
    "sky_percent",
    "pop",
    "precip_type",
    "precip_coverage",
    "wind_speed_min",
    "wind_speed_max",
    "wind_gust",
    "wind_direction",
    "weather_codes",
    "snow_amount",
    "ice_accumulation",
    "humidity_max",
    "humidity_min",
    "feels_like_max",
    "feels_like_min",
    "thunder_risk",
    "temp_trend",
]


class TestForecastPeriodRequiredFieldsOnly:
    """Instantiating with only the two required fields."""

    def test_minimal_instantiation_succeeds(self):
        period = ForecastPeriod(period_label="Today", is_daytime=True)
        assert period.period_label == "Today"
        assert period.is_daytime is True

    def test_optional_scalar_fields_default_to_none(self):
        period = ForecastPeriod(period_label="Tonight", is_daytime=False)
        assert period.temp_high is None
        assert period.temp_low is None
        assert period.sky_label is None
        assert period.sky_percent is None
        assert period.pop is None
        assert period.precip_type is None
        assert period.precip_coverage is None
        assert period.wind_speed_min is None
        assert period.wind_speed_max is None
        assert period.wind_gust is None
        assert period.wind_direction is None
        assert period.snow_amount is None
        assert period.ice_accumulation is None
        assert period.humidity_max is None
        assert period.humidity_min is None
        assert period.feels_like_max is None
        assert period.feels_like_min is None
        assert period.thunder_risk is None
        assert period.temp_trend is None

    def test_weather_codes_defaults_to_empty_list(self):
        period = ForecastPeriod(period_label="Today", is_daytime=True)
        assert period.weather_codes == []


class TestForecastPeriodFullyPopulated:
    """Instantiating with every field populated round-trips the values."""

    def _make_full_period(self) -> ForecastPeriod:
        return ForecastPeriod(
            period_label="Tomorrow",
            is_daytime=True,
            temp_high=78.0,
            temp_low=55.0,
            sky_label="partly_cloudy",
            sky_percent=42.5,
            pop=30.0,
            precip_type="rain",
            precip_coverage="Chc",
            wind_speed_min=5.0,
            wind_speed_max=15.0,
            wind_gust=25.0,
            wind_direction=270.0,
            weather_codes=["R", "T"],
            snow_amount=0.0,
            ice_accumulation=0.0,
            humidity_max=80.0,
            humidity_min=40.0,
            feels_like_max=80.0,
            feels_like_min=53.0,
            thunder_risk=20.0,
            temp_trend="rising",
        )

    def test_all_field_values_round_trip(self):
        period = self._make_full_period()
        assert period.period_label == "Tomorrow"
        assert period.is_daytime is True
        assert period.temp_high == 78.0
        assert period.temp_low == 55.0
        assert period.sky_label == "partly_cloudy"
        assert period.sky_percent == 42.5
        assert period.pop == 30.0
        assert period.precip_type == "rain"
        assert period.precip_coverage == "Chc"
        assert period.wind_speed_min == 5.0
        assert period.wind_speed_max == 15.0
        assert period.wind_gust == 25.0
        assert period.wind_direction == 270.0
        assert period.weather_codes == ["R", "T"]
        assert period.snow_amount == 0.0
        assert period.ice_accumulation == 0.0
        assert period.humidity_max == 80.0
        assert period.humidity_min == 40.0
        assert period.feels_like_max == 80.0
        assert period.feels_like_min == 53.0
        assert period.thunder_risk == 20.0
        assert period.temp_trend == "rising"

    def test_night_period_can_be_fully_populated(self):
        period = ForecastPeriod(
            period_label="Tonight",
            is_daytime=False,
            temp_low=48.0,
            sky_label="clear",
            temp_trend="falling",
        )
        assert period.is_daytime is False
        assert period.temp_low == 48.0
        assert period.sky_label == "clear"
        assert period.temp_trend == "falling"


class TestWeatherCodesMutableDefault:
    """weather_codes must be a distinct list per instance, not a shared default."""

    def test_default_is_a_list_not_none(self):
        period = ForecastPeriod(period_label="Today", is_daytime=True)
        assert isinstance(period.weather_codes, list)

    def test_mutating_one_instance_does_not_affect_another(self):
        period_a = ForecastPeriod(period_label="Today", is_daytime=True)
        period_b = ForecastPeriod(period_label="Tonight", is_daytime=False)

        period_a.weather_codes.append("R")

        assert period_a.weather_codes == ["R"]
        assert period_b.weather_codes == []
        assert period_a.weather_codes is not period_b.weather_codes


class TestForecastPeriodDataclassShape:
    """Verify field names, order, and count via dataclasses.fields()."""

    def test_field_names_match_expected_set(self):
        actual_names = [f.name for f in dataclasses.fields(ForecastPeriod)]
        assert actual_names == _EXPECTED_FIELD_NAMES

    def test_field_count(self):
        # NOTE: the T2.3 brief stated 21; the verified actual count is 22.
        # See divergence note above _EXPECTED_FIELD_NAMES.
        assert len(dataclasses.fields(ForecastPeriod)) == 22

    def test_required_fields_have_no_default(self):
        fields_by_name = {f.name: f for f in dataclasses.fields(ForecastPeriod)}
        missing = dataclasses.MISSING
        assert fields_by_name["period_label"].default is missing
        assert fields_by_name["is_daytime"].default is missing

    def test_optional_fields_default_to_none(self):
        fields_by_name = {f.name: f for f in dataclasses.fields(ForecastPeriod)}
        for name in _EXPECTED_FIELD_NAMES:
            if name in ("period_label", "is_daytime", "weather_codes"):
                continue
            assert fields_by_name[name].default is None, f"{name} should default to None"

    def test_weather_codes_uses_default_factory(self):
        fields_by_name = {f.name: f for f in dataclasses.fields(ForecastPeriod)}
        weather_codes_field = fields_by_name["weather_codes"]
        assert weather_codes_field.default is dataclasses.MISSING
        assert weather_codes_field.default_factory is list

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(ForecastPeriod)


if __name__ == "__main__":
    pytest.main([__file__])
