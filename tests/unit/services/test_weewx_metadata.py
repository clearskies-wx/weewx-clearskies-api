"""Unit tests for services/weewx_metadata.py — weewx.units metadata reader.

All tests mock the weewx import so weewx does not need to be installed.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from weewx_clearskies_api.services import weewx_metadata


# ---------------------------------------------------------------------------
# Fake weewx.units module
# ---------------------------------------------------------------------------

def _make_fake_weewx_units() -> tuple[ModuleType, ModuleType]:
    """Build fake ``weewx`` and ``weewx.units`` modules with realistic data."""
    fake_weewx = ModuleType("weewx")
    fake_weewx_units = ModuleType("weewx.units")

    fake_weewx_units.obs_group_dict = {
        "outTemp": "group_temperature",
        "windSpeed": "group_speed",
        "barometer": "group_pressure",
        "rain": "group_rain",
        "outHumidity": "group_percent",
    }

    fake_weewx_units.unit_constants = {
        "US": 1,
        "METRIC": 16,
        "METRICWX": 17,
    }

    fake_weewx_units.USUnits = {
        "group_temperature": "degree_F",
        "group_speed": "mile_per_hour",
        "group_pressure": "inHg",
        "group_rain": "inch",
        "group_percent": "percent",
    }

    fake_weewx_units.MetricUnits = {
        "group_temperature": "degree_C",
        "group_speed": "km_per_hour",
        "group_pressure": "mbar",
        "group_rain": "cm",
        "group_percent": "percent",
    }

    fake_weewx_units.MetricWXUnits = {
        "group_temperature": "degree_C",
        "group_speed": "meter_per_second",
        "group_pressure": "mbar",
        "group_rain": "mm",
        "group_percent": "percent",
    }

    fake_weewx.units = fake_weewx_units
    return fake_weewx, fake_weewx_units


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_metadata_cache():
    """Reset the module-level cache before and after each test."""
    weewx_metadata.reset_cache()
    yield
    weewx_metadata.reset_cache()


@pytest.fixture()
def _load_fake_weewx():
    """Inject fake weewx modules into sys.modules and call load_weewx_metadata."""
    fake_weewx, fake_weewx_units = _make_fake_weewx_units()
    modules = {
        "weewx": fake_weewx,
        "weewx.units": fake_weewx_units,
    }
    with patch.dict(sys.modules, modules):
        weewx_metadata.load_weewx_metadata()
        yield


# ---------------------------------------------------------------------------
# Tests: is_available
# ---------------------------------------------------------------------------

def test_is_available_true_when_loaded(_load_fake_weewx: None) -> None:
    assert weewx_metadata.is_available() is True


def test_is_available_false_when_not_loaded() -> None:
    assert weewx_metadata.is_available() is False


# ---------------------------------------------------------------------------
# Tests: get_obs_group
# ---------------------------------------------------------------------------

def test_get_obs_group_known_column(_load_fake_weewx: None) -> None:
    assert weewx_metadata.get_obs_group("outTemp") == "group_temperature"


def test_get_obs_group_unknown_column(_load_fake_weewx: None) -> None:
    assert weewx_metadata.get_obs_group("nonexistent") is None


def test_get_obs_group_returns_none_when_not_available() -> None:
    assert weewx_metadata.get_obs_group("outTemp") is None


# ---------------------------------------------------------------------------
# Tests: get_unit_for_group
# ---------------------------------------------------------------------------

def test_get_unit_for_group_us(_load_fake_weewx: None) -> None:
    assert weewx_metadata.get_unit_for_group("group_temperature", 1) == "degree_F"


def test_get_unit_for_group_metric(_load_fake_weewx: None) -> None:
    assert weewx_metadata.get_unit_for_group("group_temperature", 16) == "degree_C"


def test_get_unit_for_group_metricwx(_load_fake_weewx: None) -> None:
    assert weewx_metadata.get_unit_for_group("group_temperature", 17) == "degree_C"


def test_get_unit_for_group_unknown_group(_load_fake_weewx: None) -> None:
    assert weewx_metadata.get_unit_for_group("group_nonexistent", 1) is None


def test_get_unit_for_group_unknown_system(_load_fake_weewx: None) -> None:
    assert weewx_metadata.get_unit_for_group("group_temperature", 99) is None


def test_get_unit_for_group_returns_none_when_not_available() -> None:
    assert weewx_metadata.get_unit_for_group("group_temperature", 1) is None


# ---------------------------------------------------------------------------
# Tests: load behaviour
# ---------------------------------------------------------------------------

def test_load_with_python_path_adds_to_sys_path() -> None:
    test_path = "/fake/weewx/path/for/test"
    original_path = sys.path.copy()
    try:
        with patch.object(weewx_metadata, "_try_import", return_value=True):
            weewx_metadata.load_weewx_metadata(python_path=test_path)
        assert test_path in sys.path
    finally:
        if test_path in sys.path:
            sys.path.remove(test_path)


def test_load_graceful_when_import_fails() -> None:
    weewx_metadata.load_weewx_metadata()
    assert weewx_metadata.is_available() is False


# ---------------------------------------------------------------------------
# Tests: reset_cache
# ---------------------------------------------------------------------------

def test_reset_cache_clears_state(_load_fake_weewx: None) -> None:
    assert weewx_metadata.is_available() is True
    assert weewx_metadata.get_obs_group("outTemp") == "group_temperature"

    weewx_metadata.reset_cache()

    assert weewx_metadata.is_available() is False
    assert weewx_metadata.get_obs_group("outTemp") is None
    assert weewx_metadata.get_unit_for_group("group_temperature", 1) is None
