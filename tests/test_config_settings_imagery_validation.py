"""Tests for ImagerySettings validation (Phase LM, §LM-1).

Mirrors tests/test_config_settings_radar_validation.py's structure — the
closest existing precedent for a new single-section provider-selection
config class.

Verifies that:
  - Absent [imagery] section → provider is None (not configured), matching
    every other domain's "absence = not configured" convention.
  - "auto", "naip", "esri", "map" are all valid provider values.
  - Unknown provider ids are rejected.
  - Negative tile_cache_ttl_seconds is rejected.
  - api_key is stored/passed through but optional.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.config.settings import ImagerySettings


class TestImagerySettingsDefaults:
    def test_absent_section_provider_is_none(self) -> None:
        s = ImagerySettings({})
        assert s.provider is None

    def test_absent_section_passes_validation(self) -> None:
        s = ImagerySettings({})
        s.validate()  # must not raise

    def test_default_tile_cache_ttl_is_seven_days(self) -> None:
        s = ImagerySettings({})
        assert s.tile_cache_ttl_seconds == 604800

    def test_default_api_key_is_none(self) -> None:
        s = ImagerySettings({})
        assert s.api_key is None

    def test_empty_provider_name_treated_as_none(self) -> None:
        s = ImagerySettings({"provider": ""})
        assert s.provider is None


class TestImagerySettingsValidProviders:
    @pytest.mark.parametrize("provider", ["auto", "naip", "esri", "map"])
    def test_valid_provider_passes(self, provider: str) -> None:
        s = ImagerySettings({"provider": provider})
        s.validate()  # must not raise
        assert s.provider == provider

    def test_provider_is_lowercased(self) -> None:
        s = ImagerySettings({"provider": "NAIP"})
        assert s.provider == "naip"


class TestImagerySettingsRejectsUnknown:
    def test_unknown_provider_id_rejected(self) -> None:
        s = ImagerySettings({"provider": "totally_fake_provider"})
        with pytest.raises(ValueError, match="totally_fake_provider"):
            s.validate()

    def test_negative_ttl_rejected(self) -> None:
        s = ImagerySettings({"provider": "auto", "tile_cache_ttl_seconds": "-1"})
        with pytest.raises(ValueError, match="tile_cache_ttl_seconds"):
            s.validate()


class TestImagerySettingsApiKey:
    def test_api_key_stored_when_present(self) -> None:
        s = ImagerySettings({"provider": "auto", "api_key": "future-proofing-key"})
        assert s.api_key == "future-proofing-key"

    def test_empty_api_key_treated_as_none(self) -> None:
        s = ImagerySettings({"provider": "auto", "api_key": ""})
        assert s.api_key is None
