"""Tests for RadarSettings validation extended for 3b-15 keyed providers.

Verifies that:
  - valid_providers set accepts the 2 new keyed entries ('aeris', 'openweathermap').
  - valid_providers set still accepts all 5 keyless entries from 3b-14.
  - Unknown provider ids are rejected.
  - None provider passes validation (operator hasn't configured radar yet).

ADR references: ADR-015, ADR-027.
Brief: phase-2-task-3b-15-radar-keyed-A2-brief.md §Settings + dispatch wiring.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.config.settings import RadarSettings


class TestRadarSettingsValidationKeyed:
    """RadarSettings.validate() accepts the 2 new keyed providers from 3b-15."""

    def test_aeris_is_valid_radar_provider(self) -> None:
        """provider='aeris' passes validation (keyed provider added in 3b-15)."""
        s = RadarSettings({"provider": "aeris"})
        s.validate()  # must not raise

    def test_openweathermap_is_valid_radar_provider(self) -> None:
        """provider='openweathermap' passes validation (keyed provider added in 3b-15)."""
        s = RadarSettings({"provider": "openweathermap"})
        s.validate()  # must not raise


class TestRadarSettingsValidationKeylessStillPass:
    """Keyless providers from 3b-14 still pass validation after 3b-15 extension."""

    @pytest.mark.parametrize("provider", [
        "rainviewer",
        "iem_nexrad",
        "noaa_mrms",
        "msc_geomet",
        "dwd_radolan",
    ])
    def test_keyless_provider_still_valid(self, provider: str) -> None:
        """3b-14 keyless providers remain valid after 3b-15 set extension."""
        s = RadarSettings({"provider": provider})
        s.validate()  # must not raise


class TestRadarSettingsValidationRejectsUnknown:
    """Unknown provider ids raise ValueError."""

    def test_mapbox_jma_rejected_per_adr015_amendment(self) -> None:
        """mapbox_jma is NOT valid — dropped per ADR-015 2026-05-11 amendment."""
        s = RadarSettings({"provider": "mapbox_jma"})
        with pytest.raises(ValueError, match="mapbox_jma"):
            s.validate()

    def test_unknown_provider_id_rejected(self) -> None:
        """Arbitrary unknown provider id raises ValueError."""
        s = RadarSettings({"provider": "totally_fake_provider"})
        with pytest.raises(ValueError):
            s.validate()

    def test_empty_provider_name_treated_as_none(self) -> None:
        """Empty string provider normalizes to None (no radar configured)."""
        s = RadarSettings({"provider": ""})
        assert s.provider is None

    def test_none_provider_passes_validation(self) -> None:
        """provider=None (not configured) passes validation — radar is optional."""
        s = RadarSettings({})
        s.validate()  # must not raise

    def test_whitespace_provider_treated_as_none(self) -> None:
        """Whitespace-only provider normalizes to None."""
        s = RadarSettings({"provider": "  "})
        assert s.provider is None


class TestRadarSettingsAllSevenValidProviders:
    """All 7 valid providers (5 keyless + 2 keyed) are accepted."""

    @pytest.mark.parametrize("provider", [
        # 5 keyless (3b-14)
        "rainviewer",
        "iem_nexrad",
        "noaa_mrms",
        "msc_geomet",
        "dwd_radolan",
        # 2 keyed (3b-15)
        "aeris",
        "openweathermap",
    ])
    def test_all_valid_providers_pass_validation(self, provider: str) -> None:
        """Each of the 7 valid provider ids passes validation without error."""
        s = RadarSettings({"provider": provider})
        s.validate()  # must not raise
