"""Tests for the config/settings module.

Verifies settings loading, validation, and the secret-leak guard.
Extended in 3b-12 to cover AQISettings IQAir provider (Q1 Option A: iqair_key
lives on AQISettings directly because IQAir is AQI-only, not a multi-domain
provider like Aeris/OWM).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from weewx_clearskies_api.config.settings import (
    ApiSettings,
    AQISettings,
    HealthSettings,
    LoggingSettings,
    RateLimitSettings,
    StationSettings,
    load_settings,
)


class TestApiSettings:
    def test_defaults(self) -> None:
        s = ApiSettings({})
        assert s.bind_host == "127.0.0.1"
        assert s.bind_port == 8765
        assert s.max_request_bytes == 1 * 1024 * 1024
        assert s.cors_origins == []

    def test_custom_values(self) -> None:
        s = ApiSettings({"bind_host": "0.0.0.0", "bind_port": "9000"})
        assert s.bind_host == "0.0.0.0"
        assert s.bind_port == 9000

    def test_cors_origins_csv(self) -> None:
        s = ApiSettings({"cors_origins": "https://a.example.com, https://b.example.com"})
        assert "https://a.example.com" in s.cors_origins
        assert "https://b.example.com" in s.cors_origins

    def test_invalid_port_raises(self) -> None:
        s = ApiSettings({"bind_port": "99999"})
        with pytest.raises(ValueError, match="bind_port"):
            s.validate()


class TestHealthSettings:
    def test_defaults(self) -> None:
        s = HealthSettings({})
        assert s.bind_host == "127.0.0.1"
        assert s.bind_port == 8081


class TestLoggingSettings:
    def test_default_level(self) -> None:
        os.environ.pop("CLEARSKIES_LOG_LEVEL", None)
        s = LoggingSettings({})
        assert s.level == "INFO"

    def test_env_override(self) -> None:
        os.environ["CLEARSKIES_LOG_LEVEL"] = "DEBUG"
        try:
            s = LoggingSettings({})
            assert s.level == "DEBUG"
        finally:
            os.environ.pop("CLEARSKIES_LOG_LEVEL", None)

    def test_invalid_level_raises(self) -> None:
        os.environ.pop("CLEARSKIES_LOG_LEVEL", None)
        with pytest.raises(ValueError):
            LoggingSettings({"level": "VERBOSE"})


class TestRateLimitSettings:
    def test_defaults(self) -> None:
        s = RateLimitSettings({})
        assert s.requests_per_minute == 60
        assert s.window_seconds == 60

    def test_invalid_rpm_raises(self) -> None:
        s = RateLimitSettings({"requests_per_minute": "0"})
        with pytest.raises(ValueError):
            s.validate()


class TestLoadSettings:
    def test_file_not_found_raises(self) -> None:
        os.environ.pop("CLEARSKIES_CONFIG", None)
        with pytest.raises(FileNotFoundError):
            load_settings(config_path=Path("/nonexistent/path/api.conf"))

    def test_valid_ini_loads(self) -> None:
        """A minimal INI file loads without error."""
        content = "[api]\nbind_host = 127.0.0.1\nbind_port = 8765\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write(content)
            tmp = Path(f.name)
        try:
            settings = load_settings(config_path=tmp)
            assert settings.api.bind_host == "127.0.0.1"
        finally:
            tmp.unlink()

    def test_secret_in_conf_raises(self) -> None:
        """A secret key in the .conf file triggers the leak guard (ADR-027)."""
        content = "[api]\nbind_host = 127.0.0.1\napi_key = supersecret\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write(content)
            tmp = Path(f.name)
        try:
            with pytest.raises(RuntimeError, match="secret"):
                load_settings(config_path=tmp)
        finally:
            tmp.unlink()


class TestAQISettingsIQAir:
    """AQISettings with provider='iqair' — 3b-12 extension.

    IQAir is AQI-only (not forecast/alerts), so credentials live on
    AQISettings.iqair_key directly (Q1 Option A user decision 2026-05-11).
    Env var: WEEWX_CLEARSKIES_IQAIR_KEY (long-form provider-scoped per LC11).
    """

    def test_aqi_settings_iqair_validates_without_error(self) -> None:
        """AQISettings({'provider': 'iqair'}).validate() does not raise."""
        settings = AQISettings({"provider": "iqair"})
        settings.validate()  # Must not raise

    def test_aqi_settings_iqair_provider_is_iqair(self) -> None:
        """AQISettings({'provider': 'iqair'}).provider = 'iqair'."""
        settings = AQISettings({"provider": "iqair"})
        assert settings.provider == "iqair"

    def test_aqi_settings_iqair_key_populated_from_env_var(self) -> None:
        """AQISettings.iqair_key populated from WEEWX_CLEARSKIES_IQAIR_KEY env var."""
        os.environ["WEEWX_CLEARSKIES_IQAIR_KEY"] = "test_iqair_key_abc123"
        try:
            settings = AQISettings({"provider": "iqair"})
            assert settings.iqair_key == "test_iqair_key_abc123", (
                f"Expected iqair_key='test_iqair_key_abc123', got {settings.iqair_key!r}"
            )
        finally:
            os.environ.pop("WEEWX_CLEARSKIES_IQAIR_KEY", None)

    def test_aqi_settings_iqair_key_is_none_when_env_var_missing(self) -> None:
        """AQISettings.iqair_key = None when WEEWX_CLEARSKIES_IQAIR_KEY not set."""
        os.environ.pop("WEEWX_CLEARSKIES_IQAIR_KEY", None)
        settings = AQISettings({"provider": "iqair"})
        assert settings.iqair_key is None, (
            f"Expected iqair_key=None when env var not set, got {settings.iqair_key!r}"
        )

    def test_aqi_settings_iqair_key_is_none_when_env_var_empty(self) -> None:
        """AQISettings.iqair_key = None when WEEWX_CLEARSKIES_IQAIR_KEY is empty string."""
        os.environ["WEEWX_CLEARSKIES_IQAIR_KEY"] = ""
        try:
            settings = AQISettings({"provider": "iqair"})
            assert settings.iqair_key is None, (
                "Empty env var must produce iqair_key=None (stripped + None if falsy)"
            )
        finally:
            os.environ.pop("WEEWX_CLEARSKIES_IQAIR_KEY", None)

    def test_aqi_settings_iqair_key_is_stripped_of_whitespace(self) -> None:
        """AQISettings.iqair_key strips leading/trailing whitespace from env var."""
        os.environ["WEEWX_CLEARSKIES_IQAIR_KEY"] = "  my_key_with_spaces  "
        try:
            settings = AQISettings({"provider": "iqair"})
            assert settings.iqair_key == "my_key_with_spaces", (
                f"Expected stripped key, got {settings.iqair_key!r}"
            )
        finally:
            os.environ.pop("WEEWX_CLEARSKIES_IQAIR_KEY", None)

    def test_aqi_settings_invalid_provider_still_raises(self) -> None:
        """AQISettings({'provider': 'bogus'}).validate() raises ValueError (regression guard)."""
        settings = AQISettings({"provider": "bogus"})
        with pytest.raises(ValueError):
            settings.validate()

    def test_aqi_settings_all_four_providers_now_valid(self) -> None:
        """All four AQI providers validate: openmeteo, aeris, openweathermap, iqair."""
        for provider in ("openmeteo", "aeris", "openweathermap", "iqair"):
            settings = AQISettings({"provider": provider})
            settings.validate()  # Must not raise for any of the four


class TestStationSettingsDefaultLocale:
    """StationSettings.default_locale — ADR-021 validation tests."""

    def setup_method(self) -> None:
        """Clear env var before each test to avoid interference."""
        os.environ.pop("CLEARSKIES_DEFAULT_LOCALE", None)

    def teardown_method(self) -> None:
        """Clean up env var after each test."""
        os.environ.pop("CLEARSKIES_DEFAULT_LOCALE", None)

    def test_default_locale_is_en_when_not_configured(self) -> None:
        """StationSettings defaults to default_locale='en' when not set in INI."""
        s = StationSettings({})
        assert s.default_locale == "en", (
            f"Expected default_locale='en', got {s.default_locale!r}"
        )

    def test_default_locale_en_validates_without_error(self) -> None:
        """StationSettings default 'en' locale passes validate()."""
        s = StationSettings({})
        s.validate()  # Must not raise

    def test_all_13_supported_locales_validate(self) -> None:
        """All 13 ADR-021 locales pass validate() without error."""
        supported = [
            "en", "de", "es", "fil", "fr", "it", "ja",
            "nl", "pt-PT", "pt-BR", "ru", "zh-CN", "zh-TW",
        ]
        for locale in supported:
            s = StationSettings({"default_locale": locale})
            s.validate()  # Must not raise for any supported locale

    def test_invalid_locale_raises_value_error(self) -> None:
        """StationSettings with an unsupported locale raises ValueError on validate()."""
        s = StationSettings({"default_locale": "xx"})
        with pytest.raises(ValueError, match="default_locale"):
            s.validate()

    def test_env_var_overrides_ini_value(self) -> None:
        """CLEARSKIES_DEFAULT_LOCALE env var wins over the INI default_locale value."""
        os.environ["CLEARSKIES_DEFAULT_LOCALE"] = "de"
        s = StationSettings({"default_locale": "fr"})
        assert s.default_locale == "de", (
            f"Env var must win over INI; expected 'de', got {s.default_locale!r}"
        )

    def test_env_var_empty_string_falls_back_to_ini(self) -> None:
        """An empty CLEARSKIES_DEFAULT_LOCALE env var falls back to the INI value."""
        os.environ["CLEARSKIES_DEFAULT_LOCALE"] = ""
        s = StationSettings({"default_locale": "fr"})
        assert s.default_locale == "fr", (
            f"Empty env var must fall back to INI; expected 'fr', got {s.default_locale!r}"
        )

    def test_env_var_strips_whitespace(self) -> None:
        """CLEARSKIES_DEFAULT_LOCALE strips surrounding whitespace."""
        os.environ["CLEARSKIES_DEFAULT_LOCALE"] = "  ja  "
        s = StationSettings({})
        assert s.default_locale == "ja", (
            f"Env var whitespace must be stripped; expected 'ja', got {s.default_locale!r}"
        )
