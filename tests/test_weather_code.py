"""Unit tests for _derive_weather_code (ADR-070 T7.3).

Tests the WMO weather code derivation function from
weewx_clearskies_api.sse.enrichment.weather_text.

Priority order under test: precipitation > fog (rime or plain) > mist > haze > sky.
All keyword arguments; no positional args per the function signature.

ADR references: ADR-069, ADR-070.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.enrichment.weather_text import _derive_weather_code


# ===========================================================================
# 1. Precipitation codes (group 6x / 7x / special)
# ===========================================================================


class TestPrecipitationCodes:
    """Snow, frozen precip, and rain labels map to correct WMO codes."""

    def test_heavy_snow_maps_to_75(self) -> None:
        """rain_label='Heavy Snow' → WMO 75 (heavy continuous snow)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Heavy Snow",
            fog_mist_state=None,
        )
        assert result == 75

    def test_moderate_snow_maps_to_73(self) -> None:
        """rain_label='Moderate Snow' → WMO 73."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Moderate Snow",
            fog_mist_state=None,
        )
        assert result == 73

    def test_light_snow_maps_to_71(self) -> None:
        """rain_label='Light Snow' → WMO 71."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Light Snow",
            fog_mist_state=None,
        )
        assert result == 71

    def test_snow_generic_maps_to_71(self) -> None:
        """rain_label='Snow' → WMO 71 (same as Light Snow)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Snow",
            fog_mist_state=None,
        )
        assert result == 71

    def test_freezing_rain_maps_to_66(self) -> None:
        """rain_label='Freezing Rain' → WMO 66 (freezing rain)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Freezing Rain",
            fog_mist_state=None,
        )
        assert result == 66

    def test_sleet_maps_to_79(self) -> None:
        """rain_label='Sleet' → WMO 79 (ice pellets)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Sleet",
            fog_mist_state=None,
        )
        assert result == 79

    def test_hail_maps_to_96(self) -> None:
        """rain_label='Hail' → WMO 96 (thunderstorm with hail)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Hail",
            fog_mist_state=None,
        )
        assert result == 96

    def test_heavy_rain_maps_to_65(self) -> None:
        """rain_label='Heavy Rain' → WMO 65."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Heavy Rain",
            fog_mist_state=None,
        )
        assert result == 65

    def test_moderate_rain_maps_to_63(self) -> None:
        """rain_label='Moderate Rain' → WMO 63."""
        result = _derive_weather_code(
            effective_sky="Mostly Cloudy",
            rain_label="Moderate Rain",
            fog_mist_state=None,
        )
        assert result == 63

    def test_light_rain_maps_to_61(self) -> None:
        """rain_label='Light Rain' → WMO 61."""
        result = _derive_weather_code(
            effective_sky="Partly Cloudy",
            rain_label="Light Rain",
            fog_mist_state=None,
        )
        assert result == 61


# ===========================================================================
# 2. Fog codes (45 plain, 48 rime)
# ===========================================================================


class TestFogCodes:
    """fog_mist_state='Foggy' maps to rime (48) or plain (45) based on temperature."""

    def test_foggy_at_freezing_temperature_is_rime_fog(self) -> None:
        """Foggy + out_temp=25°F (≤ 32°F) → WMO 48 (depositing rime fog)."""
        result = _derive_weather_code(
            effective_sky=None,
            rain_label=None,
            fog_mist_state="Foggy",
            out_temp=25.0,
        )
        assert result == 48

    def test_foggy_at_exactly_32f_is_rime_fog(self) -> None:
        """Foggy + out_temp=32.0°F (at boundary ≤ 32°F) → WMO 48."""
        result = _derive_weather_code(
            effective_sky=None,
            rain_label=None,
            fog_mist_state="Foggy",
            out_temp=32.0,
        )
        assert result == 48

    def test_foggy_above_freezing_is_plain_fog(self) -> None:
        """Foggy + out_temp=40°F (> 32°F) → WMO 45 (plain fog)."""
        result = _derive_weather_code(
            effective_sky=None,
            rain_label=None,
            fog_mist_state="Foggy",
            out_temp=40.0,
        )
        assert result == 45

    def test_foggy_with_no_temperature_is_plain_fog(self) -> None:
        """Foggy + out_temp=None → WMO 45 (plain fog; rime requires confirmed temp)."""
        result = _derive_weather_code(
            effective_sky=None,
            rain_label=None,
            fog_mist_state="Foggy",
            out_temp=None,
        )
        assert result == 45


# ===========================================================================
# 3. Mist code
# ===========================================================================


class TestMistCode:
    """fog_mist_state='Misty' → WMO 10."""

    def test_misty_maps_to_10(self) -> None:
        """fog_mist_state='Misty' → WMO 10 (mist)."""
        result = _derive_weather_code(
            effective_sky="Cloudy",
            rain_label=None,
            fog_mist_state="Misty",
        )
        assert result == 10


# ===========================================================================
# 4. Haze code
# ===========================================================================


class TestHazeCode:
    """is_hazy=True → WMO 5."""

    def test_hazy_maps_to_5(self) -> None:
        """is_hazy=True → WMO 5 (haze)."""
        result = _derive_weather_code(
            effective_sky="Partly Cloudy",
            rain_label=None,
            fog_mist_state=None,
            is_hazy=True,
        )
        assert result == 5


# ===========================================================================
# 5. Sky condition codes
# ===========================================================================


class TestSkyConditionCodes:
    """Sky labels map to WMO okta-based cloudiness codes."""

    def test_overcast_maps_to_4(self) -> None:
        """effective_sky='Overcast' → WMO 4."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 4

    def test_heavy_overcast_maps_to_4(self) -> None:
        """effective_sky='Heavy Overcast' → WMO 4."""
        result = _derive_weather_code(
            effective_sky="Heavy Overcast",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 4

    def test_cloudy_maps_to_3(self) -> None:
        """effective_sky='Cloudy' → WMO 3."""
        result = _derive_weather_code(
            effective_sky="Cloudy",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 3

    def test_mostly_cloudy_maps_to_3(self) -> None:
        """effective_sky='Mostly Cloudy' → WMO 3."""
        result = _derive_weather_code(
            effective_sky="Mostly Cloudy",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 3

    def test_partly_cloudy_maps_to_2(self) -> None:
        """effective_sky='Partly Cloudy' → WMO 2."""
        result = _derive_weather_code(
            effective_sky="Partly Cloudy",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 2

    def test_mostly_clear_maps_to_1(self) -> None:
        """effective_sky='Mostly Clear' → WMO 1."""
        result = _derive_weather_code(
            effective_sky="Mostly Clear",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 1

    def test_mostly_sunny_maps_to_1(self) -> None:
        """effective_sky='Mostly Sunny' → WMO 1 (day variant of Mostly Clear)."""
        result = _derive_weather_code(
            effective_sky="Mostly Sunny",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 1

    def test_clear_maps_to_0(self) -> None:
        """effective_sky='Clear' → WMO 0."""
        result = _derive_weather_code(
            effective_sky="Clear",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 0

    def test_sunny_maps_to_0(self) -> None:
        """effective_sky='Sunny' → WMO 0 (day variant of Clear)."""
        result = _derive_weather_code(
            effective_sky="Sunny",
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 0

    def test_none_sky_maps_to_0(self) -> None:
        """effective_sky=None → WMO 0 (no sky data = default clear/unknown)."""
        result = _derive_weather_code(
            effective_sky=None,
            rain_label=None,
            fog_mist_state=None,
        )
        assert result == 0


# ===========================================================================
# 6. Priority ordering
# ===========================================================================


class TestPriorityOrdering:
    """Higher-priority conditions override lower-priority ones."""

    def test_precipitation_wins_over_fog(self) -> None:
        """rain_label='Light Rain' + fog_mist_state='Foggy' → 61 (rain > fog)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Light Rain",
            fog_mist_state="Foggy",
            out_temp=40.0,
        )
        assert result == 61, (
            f"Expected 61 (Light Rain wins over Foggy), got {result}"
        )

    def test_precipitation_wins_over_haze(self) -> None:
        """rain_label='Heavy Rain' + is_hazy=True → 65 (rain > haze)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Heavy Rain",
            fog_mist_state=None,
            is_hazy=True,
        )
        assert result == 65

    def test_fog_wins_over_haze(self) -> None:
        """fog_mist_state='Foggy' + is_hazy=True → 45 (fog > haze)."""
        result = _derive_weather_code(
            effective_sky="Partly Cloudy",
            rain_label=None,
            fog_mist_state="Foggy",
            is_hazy=True,
            out_temp=50.0,
        )
        assert result == 45, (
            f"Expected 45 (Foggy wins over Hazy), got {result}"
        )

    def test_fog_wins_over_sky(self) -> None:
        """fog_mist_state='Foggy' + effective_sky='Clear' → 45 (fog > sky)."""
        result = _derive_weather_code(
            effective_sky="Clear",
            rain_label=None,
            fog_mist_state="Foggy",
            out_temp=60.0,
        )
        assert result == 45

    def test_haze_wins_over_sky(self) -> None:
        """is_hazy=True + effective_sky='Sunny' → 5 (haze > sky)."""
        result = _derive_weather_code(
            effective_sky="Sunny",
            rain_label=None,
            fog_mist_state=None,
            is_hazy=True,
        )
        assert result == 5

    def test_snow_wins_over_fog(self) -> None:
        """rain_label='Moderate Snow' + fog_mist_state='Foggy' → 73 (snow > fog)."""
        result = _derive_weather_code(
            effective_sky="Overcast",
            rain_label="Moderate Snow",
            fog_mist_state="Foggy",
            out_temp=28.0,
        )
        assert result == 73

    def test_mist_wins_over_haze(self) -> None:
        """fog_mist_state='Misty' + is_hazy=True → 10 (mist > haze)."""
        result = _derive_weather_code(
            effective_sky="Partly Cloudy",
            rain_label=None,
            fog_mist_state="Misty",
            is_hazy=True,
        )
        assert result == 10
