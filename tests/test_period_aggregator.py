"""Unit tests for hourly-to-period forecast aggregation (ADR-070 T3.2).

Module under test: weewx_clearskies_api/sse/period_aggregator.py
(aggregate_periods()).

All tests use timezone="UTC" so local wall-clock time equals the UTC
validTime values in the synthetic fixtures below, keeping test data
construction simple and unambiguous. One test (test_timezone_conversion_
applied) exercises a non-UTC timezone to confirm local-time conversion is
actually applied rather than assumed away by the UTC-only fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from weewx_clearskies_api.models.responses import HourlyForecastPoint
from weewx_clearskies_api.sse.period_aggregator import aggregate_periods


def _hour(dt: datetime, **overrides) -> HourlyForecastPoint:
    """Build a synthetic HourlyForecastPoint at the given UTC datetime."""
    defaults = {
        "validTime": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outTemp": 70.0,
        "outHumidity": 50.0,
        "windSpeed": 10.0,
        "windDir": 180.0,
        "windGust": 15.0,
        "precipProbability": 10.0,
        "precipAmount": 0.0,
        "precipType": None,
        "cloudCover": 30.0,
        "weatherCode": None,
        "weatherText": None,
        "feelsLike": 70.0,
        "source": "test",
    }
    defaults.update(overrides)
    return HourlyForecastPoint(**defaults)


def _six_periods_of_hourly(**per_hour_overrides) -> tuple[list[HourlyForecastPoint], datetime]:
    """72 hourly points, exactly aligned to 6 consecutive 12h day/night buckets.

    Starts 2026-07-05 06:00 UTC (Sunday) and runs 72 consecutive hours,
    producing exactly 3 day periods + 3 night periods with no partial
    leftover buckets at either end.
    """
    start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
    points = [_hour(start + timedelta(hours=i), **per_hour_overrides) for i in range(72)]
    return points, start


class TestEmptyAndShape:
    def test_empty_hourly_returns_empty_list(self):
        current_time = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
        result = aggregate_periods([], None, None, current_time, "UTC")
        assert result == []

    def test_72_hourly_points_produce_6_periods(self):
        points, start = _six_periods_of_hourly()
        current_time = start + timedelta(hours=3)  # within the first day period
        result = aggregate_periods(points, None, None, current_time, "UTC")
        assert len(result) == 6
        day_count = sum(1 for p in result if p.is_daytime)
        night_count = sum(1 for p in result if not p.is_daytime)
        assert day_count == 3
        assert night_count == 3

    def test_single_hour_period_produces_valid_period(self):
        current_time = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
        points = [_hour(datetime(2026, 7, 5, 9, 0, tzinfo=UTC), outTemp=72.0)]
        result = aggregate_periods(points, None, None, current_time, "UTC")
        assert len(result) == 1
        assert result[0].temp_high == 72.0
        assert result[0].period_label == "Today"


class TestPeriodBoundaries:
    def test_period_splitting_at_boundaries(self):
        points, start = _six_periods_of_hourly()
        current_time = start + timedelta(hours=3)
        result = aggregate_periods(points, None, None, current_time, "UTC")
        # Chronological order: day, night, day, night, day, night.
        assert [p.is_daytime for p in result] == [True, False, True, False, True, False]

    def test_hours_before_6am_join_previous_nights_bucket(self):
        # An hour at 05:00 local on day2 should be grouped into day1's night
        # bucket (18:00 day1 - 06:00 day2), not day2's day bucket.
        start = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)  # day1 18:00 (start of night)
        points = [
            _hour(start, outTemp=60.0),  # day1 night start
            _hour(start + timedelta(hours=11), outTemp=55.0),  # day2 05:00 -> still night
        ]
        current_time = start
        result = aggregate_periods(points, None, None, current_time, "UTC")
        assert len(result) == 1
        assert result[0].is_daytime is False
        assert result[0].temp_low == 55.0


class TestLabels:
    def test_today_label_for_current_day_daytime(self):
        points, start = _six_periods_of_hourly()
        current_time = start + timedelta(hours=1)
        result = aggregate_periods(points, None, None, current_time, "UTC")
        assert result[0].period_label == "Today"
        assert result[0].is_daytime is True

    def test_tonight_label_for_current_day_nighttime(self):
        points, start = _six_periods_of_hourly()
        current_time = start + timedelta(hours=1)
        result = aggregate_periods(points, None, None, current_time, "UTC")
        assert result[1].period_label == "Tonight"
        assert result[1].is_daytime is False

    def test_tomorrow_labels(self):
        points, start = _six_periods_of_hourly()
        current_time = start + timedelta(hours=1)
        result = aggregate_periods(points, None, None, current_time, "UTC")
        assert result[2].period_label == "Tomorrow"
        assert result[3].period_label == "Tomorrow Night"

    def test_future_day_label_uses_weekday_name(self):
        points, start = _six_periods_of_hourly()
        current_time = start + timedelta(hours=1)
        result = aggregate_periods(points, None, None, current_time, "UTC")
        # start is 2026-07-05 (Sunday); the 3rd day bucket is 2026-07-07 (Tuesday).
        assert result[4].period_label == "Tuesday"
        assert result[5].period_label == "Tuesday Night"


class TestTemperatureAggregation:
    def test_temp_high_is_max_for_day_period(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, outTemp=70.0),
            _hour(start + timedelta(hours=1), outTemp=85.0),
            _hour(start + timedelta(hours=2), outTemp=60.0),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].temp_high == 85.0
        assert result[0].temp_low is None

    def test_temp_low_is_min_for_night_period(self):
        start = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)
        points = [
            _hour(start, outTemp=55.0),
            _hour(start + timedelta(hours=1), outTemp=40.0),
            _hour(start + timedelta(hours=2), outTemp=48.0),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].temp_low == 40.0
        assert result[0].temp_high is None

    def test_temp_trend_falling_for_day_period(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        first_half = [90.0, 92.0, 95.0, 93.0, 90.0, 88.0]
        second_half = [70.0, 65.0, 60.0, 55.0, 50.0, 45.0]
        points = [
            _hour(start + timedelta(hours=i), outTemp=t)
            for i, t in enumerate(first_half + second_half)
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].temp_high == 95.0
        assert result[0].temp_trend == "falling"

    def test_temp_trend_none_when_stable(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [_hour(start + timedelta(hours=i), outTemp=70.0 + i) for i in range(6)]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].temp_trend is None


class TestSkyAggregation:
    def test_sky_percent_is_mean_of_cloud_cover(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, cloudCover=10.0),
            _hour(start + timedelta(hours=1), cloudCover=30.0),
            _hour(start + timedelta(hours=2), cloudCover=50.0),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].sky_percent == pytest.approx(30.0)

    def test_sky_label_lookup_mostly_sunny_for_day(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [_hour(start, cloudCover=30.0)]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].sky_percent == 30.0
        assert result[0].sky_label == "mostly_sunny"

    def test_sky_label_lookup_uses_night_key(self):
        start = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)
        points = [_hour(start, cloudCover=30.0)]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].is_daytime is False
        assert result[0].sky_label == "partly_cloudy"


class TestPrecipAggregation:
    def test_pop_is_max_of_precip_probability(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, precipProbability=10.0),
            _hour(start + timedelta(hours=1), precipProbability=45.0),
            _hour(start + timedelta(hours=2), precipProbability=20.0),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].pop == 45.0

    def test_precip_type_mode(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, precipType="rain"),
            _hour(start + timedelta(hours=1), precipType="rain"),
            _hour(start + timedelta(hours=2), precipType="snow"),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].precip_type == "rain"

    def test_precip_coverage_derived_from_pop(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [_hour(start, precipProbability=40.0)]
        result = aggregate_periods(points, None, None, start, "UTC")
        # 40% falls in the 25-54 band -> pop_related_coverage "Chc".
        assert result[0].precip_coverage == "Chc"

    def test_snow_amount_sums_only_snow_hours(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, precipType="snow", precipAmount=0.1),
            _hour(start + timedelta(hours=1), precipType="snow", precipAmount=0.2),
            _hour(start + timedelta(hours=2), precipType="rain", precipAmount=5.0),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].snow_amount == pytest.approx(0.3)

    def test_snow_amount_none_when_no_snow_hours(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [_hour(start, precipType="rain", precipAmount=5.0)]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].snow_amount is None


class TestWindAggregation:
    def test_wind_direction_mode_rounds_to_8_point_compass(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, windDir=10.0),      # rounds to 0
            _hour(start + timedelta(hours=1), windDir=15.0),   # rounds to 0
            _hour(start + timedelta(hours=2), windDir=20.0),   # rounds to 0
            _hour(start + timedelta(hours=3), windDir=100.0),  # rounds to 90
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].wind_direction == 0.0

    def test_wind_speed_min_max_and_gust(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, windSpeed=5.0, windGust=10.0),
            _hour(start + timedelta(hours=1), windSpeed=20.0, windGust=35.0),
            _hour(start + timedelta(hours=2), windSpeed=12.0, windGust=18.0),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].wind_speed_min == 5.0
        assert result[0].wind_speed_max == 20.0
        assert result[0].wind_gust == 35.0


class TestNoneHandling:
    def test_all_none_field_stays_none(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, outHumidity=None),
            _hour(start + timedelta(hours=1), outHumidity=None),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].humidity_max is None
        assert result[0].humidity_min is None

    def test_single_none_does_not_corrupt_aggregation(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [
            _hour(start, outTemp=70.0),
            _hour(start + timedelta(hours=1), outTemp=None),
            _hour(start + timedelta(hours=2), outTemp=80.0),
        ]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].temp_high == 80.0

    def test_thunder_risk_and_ice_accumulation_always_none(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [_hour(start, outTemp=70.0)]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].thunder_risk is None
        assert result[0].ice_accumulation is None


class TestSunriseSunset:
    def test_is_daytime_defaults_to_bucket_type_when_sun_times_none(self):
        start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
        points = [_hour(start)]
        result = aggregate_periods(points, None, None, start, "UTC")
        assert result[0].is_daytime is True

    def test_night_period_stays_night_with_normal_sun_times(self):
        start = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)
        points = [_hour(start)]
        # Typical mid-latitude summer sun times: well within the day bucket,
        # so the night period should remain is_daytime=False.
        sunrise = "2026-07-05T10:00:00Z"
        sunset = "2026-07-05T23:30:00Z"
        result = aggregate_periods(points, sunrise, sunset, start, "UTC")
        assert result[0].is_daytime is False


class TestTimezoneConversion:
    def test_timezone_conversion_applied(self):
        # 2026-07-05 10:00 UTC = 06:00 local in America/New_York (EDT, UTC-4)
        # -> exactly the day-bucket boundary, so this point should land in
        # the "Today" day period, not the previous night's bucket.
        dt_utc = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
        points = [_hour(dt_utc, outTemp=75.0)]
        result = aggregate_periods(points, None, None, dt_utc, "America/New_York")
        assert len(result) == 1
        assert result[0].is_daytime is True
        assert result[0].period_label == "Today"
