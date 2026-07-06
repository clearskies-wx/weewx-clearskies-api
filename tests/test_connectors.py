"""Unit tests for sub-phrase connector strategies (ADR-082, GFE SS5.2).

Covers weewx_clearskies_api/sse/gfe/connectors.py: scalar_connector's
per-element overrides (Sky always "then becoming"; WaveHeight's
building/subsiding wording), vector_connector's direction/magnitude
combinations including the high-wind "becoming {dir} and
increasing/decreasing to" phrasing, and weather_connector's fixed
", then" separator.

Module under test: weewx_clearskies_api/sse/gfe/connectors.py
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.gfe import connectors as c

LOCALE = "en"


class TestScalarConnector:
    def test_scalar_connector_increasing_value_reports_increasing_to(self):
        assert c.scalar_connector(10, 20, "Wind", LOCALE) == "increasing to"

    def test_scalar_connector_decreasing_value_reports_decreasing_to(self):
        assert c.scalar_connector(20, 10, "Wind", LOCALE) == "decreasing to"

    def test_scalar_connector_equal_values_reports_then(self):
        assert c.scalar_connector(10, 10, "Wind", LOCALE) == "then"

    def test_scalar_connector_sky_element_always_then_becoming_when_increasing(self):
        assert c.scalar_connector(10, 20, "Sky", LOCALE) == "then becoming"

    def test_scalar_connector_sky_element_always_then_becoming_when_decreasing(self):
        # Sky ignores trend direction entirely.
        assert c.scalar_connector(20, 10, "Sky", LOCALE) == "then becoming"

    def test_scalar_connector_wave_height_increasing_reports_building_to(self):
        assert c.scalar_connector(3, 5, "WaveHeight", LOCALE) == "building to"

    def test_scalar_connector_wave_height_decreasing_reports_subsiding_to(self):
        assert c.scalar_connector(5, 3, "WaveHeight", LOCALE) == "subsiding to"


class TestVectorConnector:
    def test_vector_connector_same_direction_increasing_magnitude(self):
        assert c.vector_connector("N", 10, "N", 20, LOCALE) == "increasing to"

    def test_vector_connector_same_direction_decreasing_magnitude(self):
        assert c.vector_connector("N", 20, "N", 10, LOCALE) == "decreasing to"

    def test_vector_connector_same_magnitude_different_direction(self):
        assert c.vector_connector("N", 10, "NW", 10, LOCALE) == "shifting to the"

    def test_vector_connector_no_change_reports_then(self):
        assert c.vector_connector("N", 10, "N", 10, LOCALE) == "then"

    def test_vector_connector_high_wind_direction_and_magnitude_increase(self):
        # Either magnitude >= 45 mph triggers the "becoming {dir} and ..."
        # high-wind phrasing instead of a plain magnitude connector.
        result = c.vector_connector("N", 40, "NW", 50, LOCALE)
        assert result == "becoming NW and increasing to"

    def test_vector_connector_high_wind_direction_and_magnitude_decrease(self):
        result = c.vector_connector("N", 50, "NW", 40, LOCALE)
        assert result == "becoming NW and decreasing to"

    def test_vector_connector_normal_wind_direction_and_magnitude_change(self):
        # Both differ but neither speed reaches the 45 mph high-wind
        # threshold: falls back to the plain magnitude connector.
        result = c.vector_connector("N", 10, "NW", 20, LOCALE)
        assert result == "increasing to"


class TestWeatherConnector:
    def test_weather_connector_returns_comma_then(self):
        assert c.weather_connector(LOCALE) == ", then"


class TestConnectorsLocale:
    def test_scalar_connector_non_english_locale_resolves_french(self):
        assert c.scalar_connector(10, 20, "Wind", "fr") == "augmentant jusqu'à"

    def test_vector_connector_non_english_locale_resolves_french_shifting(self):
        assert c.vector_connector("N", 10, "NW", 10, "fr") == "tournant au"


if __name__ == "__main__":
    pytest.main([__file__])
