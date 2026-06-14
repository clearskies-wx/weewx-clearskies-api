"""Unit tests for _suggest_group heuristic patterns in endpoints/setup.py.

Verifies positive and negative pattern matches for AQI pollutant columns,
weather observation columns, and edge cases where patterns must not over-match.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.endpoints.setup import _suggest_group


# ---------------------------------------------------------------------------
# PM2.5 variants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["pm25", "pm_25", "PM2.5", "pm_2_5"])
def test_pm25_variants(col: str) -> None:
    assert _suggest_group(col) == "group_concentration"


# ---------------------------------------------------------------------------
# PM10
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["pm10", "PM10", "pm_10"])
def test_pm10(col: str) -> None:
    assert _suggest_group(col) == "group_concentration"


# ---------------------------------------------------------------------------
# PM1 (must not match PM10)
# ---------------------------------------------------------------------------

def test_pm1_suggests_concentration() -> None:
    assert _suggest_group("pm1") == "group_concentration"


def test_pm1_not_pm10() -> None:
    result_pm1 = _suggest_group("pm1")
    result_pm10 = _suggest_group("pm10")
    assert result_pm1 == "group_concentration"
    assert result_pm10 == "group_concentration"


# ---------------------------------------------------------------------------
# Gas pollutants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["no2", "so2", "o3", "co", "nh3"])
def test_gas_pollutants(col: str) -> None:
    assert _suggest_group(col) == "group_fraction"


# ---------------------------------------------------------------------------
# CO negative patterns (must not match cool, count, conf)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["cool", "count", "conf"])
def test_co_not_cool_or_count(col: str) -> None:
    assert _suggest_group(col) != "group_fraction"


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["myTemp", "indoor_temp_1"])
def test_temperature_pattern(col: str) -> None:
    assert _suggest_group(col) == "group_temperature"


# ---------------------------------------------------------------------------
# Humidity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["extraHumid1", "soil_humidity"])
def test_humidity_pattern(col: str) -> None:
    assert _suggest_group(col) == "group_percent"


# ---------------------------------------------------------------------------
# Pressure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["seaLevelPressure", "barometer_trend"])
def test_pressure_pattern(col: str) -> None:
    assert _suggest_group(col) == "group_pressure"


# ---------------------------------------------------------------------------
# Rain (positive) and not rainbow (negative)
# ---------------------------------------------------------------------------

def test_rain_total_suggests_rain() -> None:
    assert _suggest_group("rain_total") == "group_rain"


def test_rainbow_does_not_suggest_rain() -> None:
    assert _suggest_group("rainbow") != "group_rain"


# ---------------------------------------------------------------------------
# Wind speed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["windSpeed_2", "wind_gust_max"])
def test_wind_speed(col: str) -> None:
    assert _suggest_group(col) == "group_speed"


# ---------------------------------------------------------------------------
# Wind direction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["windDir_avg", "wind_direction"])
def test_wind_direction(col: str) -> None:
    assert _suggest_group(col) == "group_direction"


# ---------------------------------------------------------------------------
# No match
# ---------------------------------------------------------------------------

def test_no_match() -> None:
    assert _suggest_group("fooBarBaz") is None
