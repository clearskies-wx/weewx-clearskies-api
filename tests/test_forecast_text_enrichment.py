"""Integration tests for the forecast text enrichment adapter (ADR-082 T7.1/T7.4).

Module under test: weewx_clearskies_api/sse/forecast_text_enrichment.py
(enrich_forecast_text()).

Covers: synthetic hourly data producing GFE-composed forecastText per daily
point, the NWS pass-through no-op, graceful handling of empty hourly data,
and multi-locale text generation. All tests use timezone="UTC" so local
wall-clock time equals the UTC validTime values in the synthetic fixtures,
mirroring the convention in test_period_aggregator.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from weewx_clearskies_api.models.responses import DailyForecastPoint, HourlyForecastPoint
from weewx_clearskies_api.sse.forecast_text_enrichment import enrich_forecast_text


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


def _daily(valid_date: str, **overrides) -> DailyForecastPoint:
    defaults = {"validDate": valid_date, "source": "test"}
    defaults.update(overrides)
    return DailyForecastPoint(**defaults)


def _three_days_of_hourly_and_daily(
    **per_hour_overrides,
) -> tuple[list[HourlyForecastPoint], list[DailyForecastPoint], datetime]:
    """72 hourly points (3 day + 3 night buckets) plus 3 matching daily points.

    Starts 2026-07-05 06:00 UTC (Sunday) and runs 72 consecutive hours,
    producing exactly 3 day periods + 3 night periods with no partial
    leftover buckets — mirrors test_period_aggregator.py's fixture.
    """
    start = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
    hourly = [_hour(start + timedelta(hours=i), **per_hour_overrides) for i in range(72)]
    daily = [
        _daily("2026-07-05"),
        _daily("2026-07-06"),
        _daily("2026-07-07"),
    ]
    return hourly, daily, start


class TestSyntheticHourlyEnrichment:
    def test_daily_points_get_forecast_text(self):
        hourly, daily, start = _three_days_of_hourly_and_daily()
        current_time = start + timedelta(hours=3)

        bundle = {
            "source": "openmeteo",
            "hourly": hourly,
            "daily": daily,
            "sunrise": None,
            "sunset": None,
            "current_time": current_time,
            "timezone": "UTC",
        }

        result = enrich_forecast_text(bundle, "en")

        assert result is bundle
        assert daily[0].forecastText is not None
        assert daily[0].forecastText != ""

    def test_day_and_night_text_are_concatenated(self):
        hourly, daily, start = _three_days_of_hourly_and_daily()
        current_time = start + timedelta(hours=3)

        bundle = {
            "source": "openmeteo",
            "hourly": hourly,
            "daily": daily,
            "sunrise": None,
            "sunset": None,
            "current_time": current_time,
            "timezone": "UTC",
        }

        enrich_forecast_text(bundle, "en")

        # Full 72-hour fixture has both a day and a night bucket for every
        # daily point; forecastText should carry both halves joined by "\n".
        assert "\n" in daily[0].forecastText
        assert "Highs" in daily[0].forecastText
        assert "Lows" in daily[0].forecastText

    def test_at_least_one_period_has_text(self):
        # Minimal single-hour fixture — only "Today" is aggregated.
        current_time = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
        hourly = [
            _hour(datetime(2026, 7, 5, 9, 0, tzinfo=UTC), outTemp=72.0, cloudCover=10.0),
        ]
        daily = [_daily("2026-07-05")]

        bundle = {
            "source": "openmeteo",
            "hourly": hourly,
            "daily": daily,
            "sunrise": None,
            "sunset": None,
            "current_time": current_time,
            "timezone": "UTC",
        }

        enrich_forecast_text(bundle, "en")

        assert daily[0].forecastText
        assert "Highs" in daily[0].forecastText


class TestNwsPassthrough:
    def test_narrative_unchanged_and_forecast_text_not_set(self):
        hourly, _, start = _three_days_of_hourly_and_daily()
        current_time = start + timedelta(hours=3)
        daily = [_daily("2026-07-05", source="nws", narrative="Sunny with a high near 75.")]

        bundle = {
            "source": "nws",
            "hourly": hourly,
            "daily": daily,
            "sunrise": None,
            "sunset": None,
            "current_time": current_time,
            "timezone": "UTC",
        }

        result = enrich_forecast_text(bundle, "en")

        assert result is bundle
        assert daily[0].narrative == "Sunny with a high near 75."
        assert daily[0].forecastText is None

    def test_nws_passthrough_ignores_hourly_data_entirely(self):
        # Even with hourly data present, the GFE engine must not run for NWS.
        hourly = [_hour(datetime(2026, 7, 5, 9, 0, tzinfo=UTC))]
        daily = [_daily("2026-07-05", source="nws", narrative="NWS text")]
        bundle = {
            "source": "nws",
            "hourly": hourly,
            "daily": daily,
            "sunrise": None,
            "sunset": None,
            "current_time": datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
            "timezone": "UTC",
        }

        enrich_forecast_text(bundle, "en")

        assert daily[0].forecastText is None


class TestEmptyHourlyData:
    def test_empty_hourly_no_crash_forecast_text_none(self):
        daily = [_daily("2026-07-05")]
        bundle = {
            "source": "openmeteo",
            "hourly": [],
            "daily": daily,
            "sunrise": None,
            "sunset": None,
            "current_time": datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
            "timezone": "UTC",
        }

        result = enrich_forecast_text(bundle, "en")

        assert result is bundle
        assert daily[0].forecastText is None

    def test_empty_daily_no_crash(self):
        hourly = [_hour(datetime(2026, 7, 5, 9, 0, tzinfo=UTC))]
        bundle = {
            "source": "openmeteo",
            "hourly": hourly,
            "daily": [],
            "sunrise": None,
            "sunset": None,
            "current_time": datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
            "timezone": "UTC",
        }

        result = enrich_forecast_text(bundle, "en")

        assert result is bundle
        assert result["daily"] == []

    def test_missing_bundle_keys_no_crash(self):
        # bundle missing "hourly"/"daily" entirely (not even empty lists).
        bundle = {"source": "openmeteo"}
        result = enrich_forecast_text(bundle, "en")
        assert result is bundle


class TestMultiLocale:
    def test_en_and_de_both_produce_nonempty_text(self):
        for locale in ("en", "de"):
            hourly, daily, start = _three_days_of_hourly_and_daily()
            current_time = start + timedelta(hours=3)
            bundle = {
                "source": "openmeteo",
                "hourly": hourly,
                "daily": daily,
                "sunrise": None,
                "sunset": None,
                "current_time": current_time,
                "timezone": "UTC",
            }

            enrich_forecast_text(bundle, locale)

            assert daily[0].forecastText, f"locale {locale!r} produced no text"
            assert daily[0].forecastText != ""
