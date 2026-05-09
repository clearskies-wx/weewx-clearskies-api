"""Unit tests for the Weather Underground forecast provider (3b round 6).

Covers per the task-3b-6 brief §Test author parallel scope:

  Wire-shape Pydantic models:
  - _WUDaypart: real fixture loads cleanly; extras="ignore" doesn't break on extra fields.
  - _WU5DayResponse: real fixture produces 5-element top-level arrays + 10-element daypart.
  - _WU5DayResponse: missing validTimeLocal → ValidationError.

  Pure-compute helpers:
  - _wu_precip_type_to_canonical: "rain" → "rain"; "snow" → "snow";
    "precip" → "rain" (DEBUG logged once); "ice" → "freezing-rain" (DEBUG logged once);
    None → None; unknown string → None (DEBUG logged once).
  - _wu_precip_type_to_canonical: log-once behavior (second call doesn't re-log).
  - _wu_validdate_from_local: YYYY-MM-DD extracted correctly from "2026-04-30T07:00:00-0700".
  - _wu_validdate_from_local: date-part is already station-local (no UTC conversion needed).
  - _wu_validdate_from_local: missing "T" separator → ProviderProtocolError.

  _wu_to_daily_point (canonical translation):
  - validDate from validTimeLocal date-part.
  - tempMax from top-level temperatureMax.
  - tempMin from top-level temperatureMin.
  - precipAmount from top-level qpf.
  - precipProbabilityMax from daypart[0].precipChance[2*i] (Day slot for day i).
  - windSpeedMax from daypart[0].windSpeed[2*i].
  - windGustMax is always None (canonical §4.1.3 column = "—" for windGustMax).
  - sunrise from sunriseTimeUtc via epoch_to_utc_iso8601.
  - sunset from sunsetTimeUtc via epoch_to_utc_iso8601.
  - uvIndexMax from daypart[0].uvIndex[2*i].
  - weatherCode from str(daypart[0].iconCode[2*i]).
  - weatherText from daypart[0].wxPhraseShort[2*i].
  - narrative from top-level narrative[i].
  - precipType derivation: daypart[0].precipType[2*i] mapped via _wu_precip_type_to_canonical.
  - source is always "wunderground".
  - extras is always empty dict.

  Past-period null handling:
  - When daypart[0] slot i is null: daypart-derived fields (precipProbabilityMax,
    windSpeedMax, uvIndexMax, weatherCode, weatherText, precipType) emit as None.
  - Top-level fields (tempMax, tempMin, precipAmount, narrative, validDate) stay populated.
  - sunrise/sunset emit as None when sunriseTimeUtc/sunsetTimeUtc slot is null.

  _wu_to_canonical_bundle:
  - hourly=[] ALWAYS (PARTIAL-DOMAIN — Wunderground PWS API has no hourly forecast).
  - discussion=None ALWAYS (canonical §4.1.4 Wunderground column = all "—").
  - daily has 5 entries for a full /5day response.
  - source = "wunderground".
  - generatedAt ends with Z.

  units= mapping:
  - "US" → ?units=e; "METRIC" → ?units=m; "METRICWX" → ?units=s.

  fetch() (respx-mocked):
  - Cache miss → one outbound HTTP call → bundle cached → returned.
  - Cache hit → zero outbound HTTP calls → cached bundle returned.
  - Missing api_key (None) → KeyInvalid raised.
  - Missing pws_station_id (None) → KeyInvalid raised.
  - 401 response → KeyInvalid propagated (bare propagate, no narrow-wrap unlike OWM Q1).
  - 429 response → QuotaExhausted with retry_after_seconds attribute propagated.
  - 5xx response → TransientNetworkError propagated.
  - Malformed response (missing required field) → ProviderProtocolError.
  - Unknown target_unit → ProviderProtocolError.
  - Cached bundle round-trips correctly (hourly=[], discussion=None).

  CAPABILITY assertions:
  - provider_id = "wunderground".
  - domain = "forecast".
  - geographic_coverage = "global".
  - auth_required = ("apiKey", "pws_station_id").
  - default_poll_interval_seconds = 1800.
  - supplied_canonical_fields includes all 12 daily fields (validDate, tempMax, tempMin,
    precipAmount, precipProbabilityMax, windSpeedMax, sunrise, sunset, uvIndexMax,
    weatherCode, weatherText, narrative).
  - supplied_canonical_fields does NOT include HourlyForecastPoint fields.
  - supplied_canonical_fields does NOT include ForecastDiscussion fields.
  - supplied_canonical_fields does NOT include windGustMax.

No DB, no live network. respx mocks outbound httpx calls.
Wire-shape rule: fixtures loaded from tests/fixtures/providers/wunderground/*.json
(synthetic-from-api-docs per brief L3 rule, 3b-4 process lesson).
ADR references: ADR-006, ADR-007, ADR-010, ADR-017, ADR-018, ADR-019, ADR-020,
ADR-027, ADR-029, ADR-038.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "wunderground"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file from tests/fixtures/providers/wunderground/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helpers
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, and re-wire memory cache."""
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.forecast.wunderground import (  # noqa: PLC0415
        _reset_http_client_for_tests,
    )
    import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    # Clear rate-limiter deque so consecutive tests don't trip each other.
    _wu._rate_limiter._calls.clear()
    # Clear logged-unknown-precip sets so DEBUG-logging tests don't silently pass.
    _wu._logged_unknown_precip.clear()
    _wu._logged_mixed_precip.clear()
    # Re-wire a clean memory cache (CLEARSKIES_CACHE_URL unset in unit test env).
    wire_cache_from_env()


# Wunderground URL for respx mocking
_WU_BASE_URL = "https://api.weather.com"
_WU_FORECAST_PATH = "/v3/wx/forecast/daily/5day"
_WU_FORECAST_URL = _WU_BASE_URL + _WU_FORECAST_PATH
_LAT = 47.6062
_LON = -122.3321
_TEST_API_KEY = "TEST_WU_KEY_12345"
_TEST_PWS_ID = "KWASEATT123"


# ===========================================================================
# 1. Wire-shape Pydantic models
# ===========================================================================


class TestWuWireShapeModels:
    """Wunderground wire-shape Pydantic models load cleanly from fixtures."""

    def test_wu5day_response_loads_from_full_fixture(self) -> None:
        """_WU5DayResponse loads from forecast_daily_5day.json without errors."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day.json")
        model = _WU5DayResponse.model_validate(data)
        assert len(model.temperatureMax) == 5
        assert len(model.temperatureMin) == 5
        assert len(model.validTimeLocal) == 5
        assert len(model.narrative) == 5
        assert len(model.qpf) == 5
        assert len(model.daypart) == 1  # Single daypart container

    def test_wu5day_daypart_has_10_elements(self) -> None:
        """daypart[0] arrays have 10 elements (5 days × 2 dayparts D/N)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day.json")
        model = _WU5DayResponse.model_validate(data)
        dp = model.daypart[0]
        assert len(dp.iconCode) == 10
        assert len(dp.precipChance) == 10
        assert len(dp.windSpeed) == 10
        assert len(dp.uvIndex) == 10
        assert len(dp.wxPhraseShort) == 10

    def test_wu5day_response_ignores_extra_fields(self) -> None:
        """extras='ignore' means unknown fields in the wire response don't cause ValidationError."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day.json")
        # Inject unknown field that the Pydantic model doesn't know about
        data["unknownProviderField"] = "some_future_value"
        # Should not raise
        model = _WU5DayResponse.model_validate(data)
        assert model.temperatureMax[0] == 64

    def test_wu5day_response_validates_required_validtimelocal(self) -> None:
        """Missing validTimeLocal → ValidationError (required field)."""
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day.json")
        del data["validTimeLocal"]
        with pytest.raises(ValidationError):
            _WU5DayResponse.model_validate(data)

    def test_wu_daypart_loads_cleanly_from_fixture(self) -> None:
        """_WUDaypart loads the daypart[0] object from the fixture."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day.json")
        model = _WU5DayResponse.model_validate(data)
        dp = model.daypart[0]
        # Day/Night alternates: D/N pattern for each day
        assert dp.dayOrNight[0] == "D"
        assert dp.dayOrNight[1] == "N"

    def test_passed_today_fixture_has_null_slot_0(self) -> None:
        """forecast_daily_5day_passed_today.json has null in daypart slot 0."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day_passed_today.json")
        model = _WU5DayResponse.model_validate(data)
        dp = model.daypart[0]
        # Slot 0 (Today/Day) should be null (past-period)
        assert dp.iconCode[0] is None
        assert dp.precipChance[0] is None
        assert dp.windSpeed[0] is None
        assert dp.uvIndex[0] is None
        assert dp.wxPhraseShort[0] is None
        # Top-level fields stay populated for day 0
        assert model.temperatureMax[0] == 64
        assert model.narrative[0] is not None


# ===========================================================================
# 2. _wu_precip_type_to_canonical — all string mappings
# ===========================================================================


class TestWuPrecipTypeToCanonical:
    """_wu_precip_type_to_canonical maps Wunderground precipType values to canonical §3.3 enum."""

    def test_rain_maps_to_rain(self) -> None:
        """'rain' → 'rain' (direct match)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        assert _wu_precip_type_to_canonical("rain") == "rain"

    def test_snow_maps_to_snow(self) -> None:
        """'snow' → 'snow' (direct match)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        assert _wu_precip_type_to_canonical("snow") == "snow"

    def test_precip_maps_to_rain_with_debug_log(self, caplog: Any) -> None:
        """'precip' → 'rain' (mixed/general); DEBUG log emitted once per brief lead-call 17."""
        import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415
        _wu._logged_mixed_precip.clear()
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        with caplog.at_level(
            logging.DEBUG,
            logger="weewx_clearskies_api.providers.forecast.wunderground",
        ):
            result = _wu_precip_type_to_canonical("precip")
        assert result == "rain"
        assert "precip" in _wu._logged_mixed_precip

    def test_precip_only_logged_once(self) -> None:
        """Second call with 'precip' doesn't re-log (log-once behavior)."""
        import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415
        _wu._logged_mixed_precip.clear()
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        _wu_precip_type_to_canonical("precip")
        count_before = len(_wu._logged_mixed_precip)
        _wu_precip_type_to_canonical("precip")
        assert len(_wu._logged_mixed_precip) == count_before

    def test_ice_maps_to_freezing_rain_with_debug_log(self, caplog: Any) -> None:
        """'ice' → 'freezing-rain'; DEBUG log emitted once per brief lead-call 17."""
        import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415
        _wu._logged_mixed_precip.clear()
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        with caplog.at_level(
            logging.DEBUG,
            logger="weewx_clearskies_api.providers.forecast.wunderground",
        ):
            result = _wu_precip_type_to_canonical("ice")
        assert result == "freezing-rain"
        assert "ice" in _wu._logged_mixed_precip

    def test_ice_only_logged_once(self) -> None:
        """Second call with 'ice' doesn't re-log."""
        import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415
        _wu._logged_mixed_precip.clear()
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        _wu_precip_type_to_canonical("ice")
        count_before = len(_wu._logged_mixed_precip)
        _wu_precip_type_to_canonical("ice")
        assert len(_wu._logged_mixed_precip) == count_before

    def test_none_maps_to_none(self) -> None:
        """None → None (no precipitation)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        assert _wu_precip_type_to_canonical(None) is None

    def test_unknown_string_logs_debug_and_returns_none(self, caplog: Any) -> None:
        """Unknown string 'hail_special' → None; DEBUG log emitted once."""
        import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415
        _wu._logged_unknown_precip.clear()
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        with caplog.at_level(
            logging.DEBUG,
            logger="weewx_clearskies_api.providers.forecast.wunderground",
        ):
            result = _wu_precip_type_to_canonical("hail_special")
        assert result is None
        assert "hail_special" in _wu._logged_unknown_precip

    def test_unknown_string_only_logged_once(self) -> None:
        """Second call with unknown string doesn't double-add to logged set."""
        import weewx_clearskies_api.providers.forecast.wunderground as _wu  # noqa: PLC0415
        _wu._logged_unknown_precip.clear()
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_precip_type_to_canonical  # noqa: PLC0415

        _wu_precip_type_to_canonical("future_type")
        count_before = len(_wu._logged_unknown_precip)
        _wu_precip_type_to_canonical("future_type")
        assert len(_wu._logged_unknown_precip) == count_before


# ===========================================================================
# 3. _wu_validdate_from_local — date extraction
# ===========================================================================


class TestWuValiddateFromLocal:
    """_wu_validdate_from_local extracts station-local YYYY-MM-DD from validTimeLocal."""

    def test_extracts_date_from_datetime_with_offset(self) -> None:
        """'2026-04-30T07:00:00-0700' → '2026-04-30'."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_validdate_from_local  # noqa: PLC0415

        assert _wu_validdate_from_local("2026-04-30T07:00:00-0700") == "2026-04-30"

    def test_extracts_date_from_positive_offset(self) -> None:
        """'2026-05-01T07:00:00+0530' → '2026-05-01' (positive UTC offset)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_validdate_from_local  # noqa: PLC0415

        assert _wu_validdate_from_local("2026-05-01T07:00:00+0530") == "2026-05-01"

    def test_missing_t_separator_raises_provider_protocol_error(self) -> None:
        """String without 'T' separator raises ProviderProtocolError (schema change)."""
        from weewx_clearskies_api.providers._common.exceptions import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_validdate_from_local  # noqa: PLC0415

        with pytest.raises(ProviderProtocolError):
            _wu_validdate_from_local("2026-04-30 07:00:00")

    def test_date_is_station_local_not_utc(self) -> None:
        """Date portion reflects station-local time, not UTC conversion.

        At -0700, '2026-04-30T07:00:00-0700' is 2026-04-30 in local time.
        (UTC equivalent is 2026-04-30T14:00:00Z — same calendar date in this case,
        but the rule is: use the Local field's date portion directly, per brief lead-call 28.)
        """
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_validdate_from_local  # noqa: PLC0415

        # If the time were, say, 23:00 local vs 06:00 UTC next day, the local date wins.
        # This test confirms we split on 'T' and take the date part only.
        assert _wu_validdate_from_local("2026-04-30T23:00:00-0700") == "2026-04-30"


# ===========================================================================
# 4. _wu_to_daily_point — canonical translation
# ===========================================================================


class TestWuToDailyPoint:
    """_wu_to_daily_point maps a day-i entry to a DailyForecastPoint."""

    def setup_method(self) -> None:
        """Load the full fixture and parse into a _WU5DayResponse model."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day.json")
        self._model = _WU5DayResponse.model_validate(data)

    def test_validdate_from_validtimelocal_date_part(self) -> None:
        """validDate is the date portion of validTimeLocal[0]."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.validDate == "2026-04-30"

    def test_validdate_day_1(self) -> None:
        """validDate for day 1 is '2026-05-01'."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=1)
        assert point.validDate == "2026-05-01"

    def test_tempmax_from_temperature_max_array(self) -> None:
        """tempMax = temperatureMax[i] (top-level array, already in target_unit)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.tempMax == 64

    def test_tempmin_from_temperature_min_array(self) -> None:
        """tempMin = temperatureMin[i]."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.tempMin == 48

    def test_precipamount_from_qpf_array(self) -> None:
        """precipAmount = qpf[i] (already in target_unit's precip unit)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 0: qpf=0.0, Day 1: qpf=0.12
        point_0 = _wu_to_daily_point(self._model, day_index=0)
        assert point_0.precipAmount == 0.0
        point_1 = _wu_to_daily_point(self._model, day_index=1)
        assert point_1.precipAmount == 0.12

    def test_precipprobabilitymax_from_daypart_precipchance_day_slot(self) -> None:
        """precipProbabilityMax = daypart[0].precipChance[2*i] (Day slot, already %)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 0 → slot 0 (D), precipChance[0] = 20
        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.precipProbabilityMax == 20

    def test_precipprobabilitymax_day_2(self) -> None:
        """precipProbabilityMax for day 2 = daypart[0].precipChance[4] = 5."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 2 → slot 4 (D), precipChance[4] = 5
        point = _wu_to_daily_point(self._model, day_index=2)
        assert point.precipProbabilityMax == 5

    def test_windspeedmax_from_daypart_windspeed_day_slot(self) -> None:
        """windSpeedMax = daypart[0].windSpeed[2*i] (Day slot)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 0 → slot 0, windSpeed[0] = 7
        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.windSpeedMax == 7

    def test_windgustmax_is_always_none(self) -> None:
        """windGustMax is always None — canonical §4.1.3 Wunderground column = '—'."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.windGustMax is None

    def test_sunrise_from_sunrisetimeutc_epoch(self) -> None:
        """sunrise = epoch_to_utc_iso8601(sunriseTimeUtc[i]) — UTC ISO-8601 Z string."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.sunrise is not None
        assert point.sunrise.endswith("Z")
        # sunriseTimeUtc[0] = 1746017700 → should produce a valid ISO-8601 UTC string
        assert "T" in point.sunrise

    def test_sunset_from_sunsettimeutc_epoch(self) -> None:
        """sunset = epoch_to_utc_iso8601(sunsetTimeUtc[i]) — UTC ISO-8601 Z string."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.sunset is not None
        assert point.sunset.endswith("Z")

    def test_uvindexmax_from_daypart_uvindex_day_slot(self) -> None:
        """uvIndexMax = daypart[0].uvIndex[2*i] (Day slot)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 0 → slot 0, uvIndex[0] = 4
        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.uvIndexMax == 4

    def test_weathercode_is_str_of_daypart_iconcode_day_slot(self) -> None:
        """weatherCode = str(daypart[0].iconCode[2*i]) — opaque provider pass-through."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 0 → slot 0, iconCode[0] = 28 → "28"
        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.weatherCode == "28"

    def test_weathertext_from_daypart_wxphraseshort_day_slot(self) -> None:
        """weatherText = daypart[0].wxPhraseShort[2*i] (Day slot short phrase)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 0 → slot 0, wxPhraseShort[0] = "M Cloudy"
        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.weatherText == "M Cloudy"

    def test_narrative_from_top_level_narrative_array(self) -> None:
        """narrative = top-level narrative[i] (NOT daypart narrative)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.narrative == "Mostly cloudy. High 64F. Winds SW at 5 to 10 mph."

    def test_preciptype_maps_rain_string_to_canonical_rain(self) -> None:
        """precipType 'rain' from daypart[0].precipType[2*i] → canonical 'rain'."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 0 → slot 0, precipType[0] = "rain"
        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.precipType == "rain"

    def test_preciptype_maps_null_to_none(self) -> None:
        """precipType None from daypart slot → canonical None."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        # Day 2 → slot 4, precipType[4] = null
        point = _wu_to_daily_point(self._model, day_index=2)
        assert point.precipType is None

    def test_source_is_wunderground(self) -> None:
        """source is always 'wunderground'."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.source == "wunderground"

    def test_extras_is_empty_dict(self) -> None:
        """extras is always empty dict (no extras extraction in v0.1 per brief lead-call 32)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.extras == {}


# ===========================================================================
# 5. Past-period null handling
# ===========================================================================


class TestPastPeriodNullHandling:
    """Past-period slot 0 null propagates correctly to canonical None for daypart fields."""

    def setup_method(self) -> None:
        """Load the passed-today fixture."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day_passed_today.json")
        self._model = _WU5DayResponse.model_validate(data)

    def test_daypart_precipchance_null_slot_emits_none(self) -> None:
        """When daypart[0].precipChance[0] is null, precipProbabilityMax=None for day 0."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.precipProbabilityMax is None

    def test_daypart_windspeed_null_slot_emits_none(self) -> None:
        """When daypart[0].windSpeed[0] is null, windSpeedMax=None for day 0."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.windSpeedMax is None

    def test_daypart_uvindex_null_slot_emits_none(self) -> None:
        """When daypart[0].uvIndex[0] is null, uvIndexMax=None for day 0."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.uvIndexMax is None

    def test_daypart_iconcode_null_slot_emits_none_weathercode(self) -> None:
        """When daypart[0].iconCode[0] is null, weatherCode=None for day 0."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.weatherCode is None

    def test_daypart_wxphraseshort_null_slot_emits_none_weathertext(self) -> None:
        """When daypart[0].wxPhraseShort[0] is null, weatherText=None for day 0."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.weatherText is None

    def test_daypart_preciptype_null_slot_emits_none(self) -> None:
        """When daypart[0].precipType[0] is null, precipType=None for day 0."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.precipType is None

    def test_top_level_tempmax_stays_populated(self) -> None:
        """Top-level temperatureMax[0] stays populated even when daypart slot 0 is null."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        # temperatureMax[0] = 64 in the passed-today fixture
        assert point.tempMax == 64

    def test_top_level_narrative_stays_populated(self) -> None:
        """Top-level narrative[0] stays populated even when daypart slot 0 is null."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        assert point.narrative is not None

    def test_top_level_qpf_stays_populated(self) -> None:
        """Top-level qpf[0] stays populated even when daypart slot 0 is null."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=0)
        # qpf[0] = 0.0 in the passed-today fixture
        assert point.precipAmount == 0.0

    def test_subsequent_days_unaffected_by_null_slot_0(self) -> None:
        """Day 1 (slot 2 = D) has all daypart fields populated normally."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_daily_point  # noqa: PLC0415

        point = _wu_to_daily_point(self._model, day_index=1)
        # Day 1 → slot 2 (D), all populated in passed-today fixture
        assert point.precipProbabilityMax is not None
        assert point.windSpeedMax is not None
        assert point.weatherCode is not None


# ===========================================================================
# 6. _wu_to_canonical_bundle — full bundle assembly
# ===========================================================================


class TestWuToCanonicalBundle:
    """_wu_to_canonical_bundle assembles a ForecastBundle from the wire model."""

    def setup_method(self) -> None:
        """Load the full fixture."""
        from weewx_clearskies_api.providers.forecast.wunderground import _WU5DayResponse  # noqa: PLC0415

        data = _load_fixture("forecast_daily_5day.json")
        self._model = _WU5DayResponse.model_validate(data)

    def test_hourly_is_always_empty_list(self) -> None:
        """hourly=[] ALWAYS — Wunderground PWS API has no hourly forecast (PARTIAL-DOMAIN)."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_canonical_bundle  # noqa: PLC0415

        bundle = _wu_to_canonical_bundle(self._model)
        assert bundle.hourly == []

    def test_discussion_is_always_none(self) -> None:
        """discussion=None ALWAYS — canonical §4.1.4 Wunderground column = all '—'."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_canonical_bundle  # noqa: PLC0415

        bundle = _wu_to_canonical_bundle(self._model)
        assert bundle.discussion is None

    def test_daily_has_5_entries(self) -> None:
        """daily has 5 entries for a full /5day response."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_canonical_bundle  # noqa: PLC0415

        bundle = _wu_to_canonical_bundle(self._model)
        assert len(bundle.daily) == 5

    def test_source_is_wunderground(self) -> None:
        """source = 'wunderground' on the bundle."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_canonical_bundle  # noqa: PLC0415

        bundle = _wu_to_canonical_bundle(self._model)
        assert bundle.source == "wunderground"

    def test_generatedat_ends_with_z(self) -> None:
        """generatedAt is a UTC ISO-8601 string ending in Z."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_canonical_bundle  # noqa: PLC0415

        bundle = _wu_to_canonical_bundle(self._model)
        assert bundle.generatedAt.endswith("Z")

    def test_daily_points_have_correct_validdates(self) -> None:
        """Daily entries have ascending station-local dates."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_canonical_bundle  # noqa: PLC0415

        bundle = _wu_to_canonical_bundle(self._model)
        assert bundle.daily[0].validDate == "2026-04-30"
        assert bundle.daily[1].validDate == "2026-05-01"
        assert bundle.daily[4].validDate == "2026-05-04"

    def test_bundle_source_on_each_daily_point_is_wunderground(self) -> None:
        """Each DailyForecastPoint.source = 'wunderground'."""
        from weewx_clearskies_api.providers.forecast.wunderground import _wu_to_canonical_bundle  # noqa: PLC0415

        bundle = _wu_to_canonical_bundle(self._model)
        for day in bundle.daily:
            assert day.source == "wunderground"


# ===========================================================================
# 7. units= mapping — US, METRIC, METRICWX
# ===========================================================================


class TestUnitsMapping:
    """fetch() passes the correct Wunderground units= query param per target_unit."""

    def setup_method(self) -> None:
        _reset_provider_state()

    def teardown_method(self) -> None:
        _reset_provider_state()

    def test_us_target_unit_uses_units_e(self) -> None:
        """target_unit='US' → units=e (English/imperial) in the outbound URL."""
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture = _load_fixture("forecast_daily_5day.json")
        captured_params: dict[str, str] = {}

        with respx.mock(assert_all_called=False) as mock:
            def capture_and_respond(request: httpx.Request) -> httpx.Response:
                for key, val in request.url.params.items():
                    captured_params[key] = val
                return httpx.Response(200, json=fixture)

            mock.get(_WU_FORECAST_URL).mock(side_effect=capture_and_respond)
            fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="US",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )

        assert captured_params.get("units") == "e"

    def test_metric_target_unit_uses_units_m(self) -> None:
        """target_unit='METRIC' → units=m (Metric SI variant)."""
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture = _load_fixture("forecast_daily_5day.json")
        captured_params: dict[str, str] = {}

        with respx.mock(assert_all_called=False) as mock:
            def capture_and_respond(request: httpx.Request) -> httpx.Response:
                for key, val in request.url.params.items():
                    captured_params[key] = val
                return httpx.Response(200, json=fixture)

            mock.get(_WU_FORECAST_URL).mock(side_effect=capture_and_respond)
            fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="METRIC",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )

        assert captured_params.get("units") == "m"

    def test_metricwx_target_unit_uses_units_s(self) -> None:
        """target_unit='METRICWX' → units=s (Pure SI, m/s wind)."""
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture = _load_fixture("forecast_daily_5day.json")
        captured_params: dict[str, str] = {}

        with respx.mock(assert_all_called=False) as mock:
            def capture_and_respond(request: httpx.Request) -> httpx.Response:
                for key, val in request.url.params.items():
                    captured_params[key] = val
                return httpx.Response(200, json=fixture)

            mock.get(_WU_FORECAST_URL).mock(side_effect=capture_and_respond)
            fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="METRICWX",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )

        assert captured_params.get("units") == "s"


# ===========================================================================
# 8. fetch() — respx-mocked HTTP interactions
# ===========================================================================


class TestFetchHttpInteractions:
    """fetch() HTTP interaction: cache miss/hit, error paths, credential validation."""

    def setup_method(self) -> None:
        _reset_provider_state()

    def teardown_method(self) -> None:
        _reset_provider_state()

    def test_cache_miss_makes_one_outbound_call_and_returns_bundle(self) -> None:
        """Cache miss → one HTTP call to Wunderground → bundle returned."""
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture = _load_fixture("forecast_daily_5day.json")
        call_count = 0

        with respx.mock(assert_all_called=False) as mock:
            def count_and_respond(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(200, json=fixture)

            mock.get(_WU_FORECAST_URL).mock(side_effect=count_and_respond)
            bundle = fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="US",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )

        assert call_count == 1
        assert len(bundle.daily) == 5
        assert bundle.hourly == []
        assert bundle.discussion is None
        assert bundle.source == "wunderground"

    def test_cache_hit_makes_zero_outbound_calls(self) -> None:
        """Second fetch (cache hit) makes zero outbound calls."""
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture = _load_fixture("forecast_daily_5day.json")
        call_count = 0

        with respx.mock(assert_all_called=False) as mock:
            def count_and_respond(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(200, json=fixture)

            mock.get(_WU_FORECAST_URL).mock(side_effect=count_and_respond)
            # First call populates cache
            fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="US",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )
            # Second call should hit cache
            bundle = fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="US",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )

        assert call_count == 1
        assert bundle.hourly == []
        assert bundle.discussion is None

    def test_cached_bundle_round_trips_correctly(self) -> None:
        """Cached bundle round-trips: discussion=None, hourly=[], source='wunderground'."""
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture = _load_fixture("forecast_daily_5day.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=fixture)
            )
            # First call: populate cache
            fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="US",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )
            # Second call: from cache
            bundle = fetch(
                lat=_LAT,
                lon=_LON,
                target_unit="US",
                api_key=_TEST_API_KEY,
                pws_station_id=_TEST_PWS_ID,
            )

        assert bundle.discussion is None
        assert bundle.hourly == []
        assert bundle.source == "wunderground"
        assert len(bundle.daily) == 5

    def test_missing_api_key_raises_key_invalid(self) -> None:
        """api_key=None → KeyInvalid raised (loud failure per brief lead-call 14)."""
        from weewx_clearskies_api.providers._common.exceptions import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        with respx.mock(assert_all_called=False):
            with pytest.raises(KeyInvalid):
                fetch(
                    lat=_LAT,
                    lon=_LON,
                    target_unit="US",
                    api_key=None,
                    pws_station_id=_TEST_PWS_ID,
                )

    def test_missing_pws_station_id_raises_key_invalid(self) -> None:
        """pws_station_id=None → KeyInvalid raised (brief lead-call 14 defense-in-depth)."""
        from weewx_clearskies_api.providers._common.exceptions import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        with respx.mock(assert_all_called=False):
            with pytest.raises(KeyInvalid):
                fetch(
                    lat=_LAT,
                    lon=_LON,
                    target_unit="US",
                    api_key=_TEST_API_KEY,
                    pws_station_id=None,
                )

    def test_401_response_propagates_key_invalid(self) -> None:
        """401 from Wunderground → KeyInvalid propagated (bare propagate, no Q1 wrap)."""
        from weewx_clearskies_api.providers._common.exceptions import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture_401 = _load_fixture("error_401_invalid_key.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(401, json=fixture_401)
            )
            with pytest.raises(KeyInvalid):
                fetch(
                    lat=_LAT,
                    lon=_LON,
                    target_unit="US",
                    api_key=_TEST_API_KEY,
                    pws_station_id=_TEST_PWS_ID,
                )

    def test_429_response_propagates_quota_exhausted(self) -> None:
        """429 from Wunderground → QuotaExhausted with retry_after_seconds attribute."""
        from weewx_clearskies_api.providers._common.exceptions import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        fixture_429 = _load_fixture("error_429_quota.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(
                    429,
                    json=fixture_429,
                    headers={"Retry-After": "60"},
                )
            )
            with pytest.raises(QuotaExhausted) as exc_info:
                fetch(
                    lat=_LAT,
                    lon=_LON,
                    target_unit="US",
                    api_key=_TEST_API_KEY,
                    pws_station_id=_TEST_PWS_ID,
                )
        # retry_after_seconds attribute must be present (per 3b-4 F1 remediation)
        assert hasattr(exc_info.value, "retry_after_seconds")

    def test_5xx_response_propagates_transient_network_error(self) -> None:
        """5xx from Wunderground → TransientNetworkError propagated."""
        from weewx_clearskies_api.providers._common.exceptions import TransientNetworkError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(503, json={"error": "Service Unavailable"})
            )
            with pytest.raises(TransientNetworkError):
                fetch(
                    lat=_LAT,
                    lon=_LON,
                    target_unit="US",
                    api_key=_TEST_API_KEY,
                    pws_station_id=_TEST_PWS_ID,
                )

    def test_malformed_response_raises_provider_protocol_error(self) -> None:
        """Response missing required field → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.exceptions import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        # Missing validTimeLocal makes the Pydantic model fail
        malformed = {"temperatureMax": [64, 66, 70, 68, 65]}
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_WU_FORECAST_URL).mock(
                return_value=httpx.Response(200, json=malformed)
            )
            with pytest.raises(ProviderProtocolError):
                fetch(
                    lat=_LAT,
                    lon=_LON,
                    target_unit="US",
                    api_key=_TEST_API_KEY,
                    pws_station_id=_TEST_PWS_ID,
                )

    def test_unknown_target_unit_raises_provider_protocol_error(self) -> None:
        """Unknown target_unit → ProviderProtocolError (schema/config error)."""
        from weewx_clearskies_api.providers._common.exceptions import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.wunderground import fetch  # noqa: PLC0415

        with respx.mock(assert_all_called=False):
            with pytest.raises(ProviderProtocolError):
                fetch(
                    lat=_LAT,
                    lon=_LON,
                    target_unit="IMPERIAL",  # invalid — not US/METRIC/METRICWX
                    api_key=_TEST_API_KEY,
                    pws_station_id=_TEST_PWS_ID,
                )


# ===========================================================================
# 9. CAPABILITY assertions
# ===========================================================================


class TestWundergroundCapability:
    """CAPABILITY declaration matches the brief lead-call 18 specification."""

    def test_capability_provider_id_is_wunderground(self) -> None:
        """CAPABILITY.provider_id = 'wunderground'."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "wunderground"

    def test_capability_domain_is_forecast(self) -> None:
        """CAPABILITY.domain = 'forecast'."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "forecast"

    def test_capability_geographic_coverage_is_global(self) -> None:
        """CAPABILITY.geographic_coverage = 'global' (TWC place network per lead-call 29)."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "global"

    def test_capability_auth_required_includes_api_key(self) -> None:
        """CAPABILITY.auth_required includes 'apiKey'."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "apiKey" in CAPABILITY.auth_required

    def test_capability_auth_required_includes_pws_station_id(self) -> None:
        """CAPABILITY.auth_required includes 'pws_station_id' (config-time gate)."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "pws_station_id" in CAPABILITY.auth_required

    def test_capability_default_poll_interval_is_1800(self) -> None:
        """CAPABILITY.default_poll_interval_seconds = 1800 (30 min per ADR-017)."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.default_poll_interval_seconds == 1800

    def test_capability_supplied_fields_includes_validdate(self) -> None:
        """'validDate' is in supplied_canonical_fields."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "validDate" in CAPABILITY.supplied_canonical_fields

    def test_capability_supplied_fields_includes_all_12_daily_fields(self) -> None:
        """All 12 daily fields from brief lead-call 18 are in supplied_canonical_fields."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        required_daily_fields = {
            "validDate", "tempMax", "tempMin", "precipAmount",
            "precipProbabilityMax", "windSpeedMax",
            "sunrise", "sunset", "uvIndexMax", "weatherCode", "weatherText",
            "narrative",
        }
        for field in required_daily_fields:
            assert field in CAPABILITY.supplied_canonical_fields, (
                f"'{field}' missing from supplied_canonical_fields"
            )

    def test_capability_supplied_fields_excludes_windgustmax(self) -> None:
        """'windGustMax' is NOT in supplied_canonical_fields — canonical §4.1.3 column = '—'."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "windGustMax" not in CAPABILITY.supplied_canonical_fields

    def test_capability_supplied_fields_excludes_hourly_validtime(self) -> None:
        """'validTime' (HourlyForecastPoint) is NOT supplied — PARTIAL-DOMAIN."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "validTime" not in CAPABILITY.supplied_canonical_fields

    def test_capability_supplied_fields_excludes_hourly_outtemp(self) -> None:
        """'outTemp' (HourlyForecastPoint) is NOT supplied — no hourly on any tier."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "outTemp" not in CAPABILITY.supplied_canonical_fields

    def test_capability_supplied_fields_excludes_hourly_precipprobability(self) -> None:
        """'precipProbability' (HourlyForecastPoint) is NOT supplied — no hourly."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "precipProbability" not in CAPABILITY.supplied_canonical_fields

    def test_capability_supplied_fields_excludes_discussion_headline(self) -> None:
        """'headline' (ForecastDiscussion) is NOT supplied — no discussion product."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "headline" not in CAPABILITY.supplied_canonical_fields

    def test_capability_supplied_fields_excludes_discussion_body(self) -> None:
        """'body' (ForecastDiscussion) is NOT supplied — no discussion product."""
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        assert "body" not in CAPABILITY.supplied_canonical_fields

    def test_capability_wire_providers_registers_wunderground(self) -> None:
        """wire_providers([wunderground.CAPABILITY]) → registry has wunderground entry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast.wunderground import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        wu_entries = [p for p in registry if p.provider_id == "wunderground"]
        assert len(wu_entries) == 1
        assert wu_entries[0].domain == "forecast"
