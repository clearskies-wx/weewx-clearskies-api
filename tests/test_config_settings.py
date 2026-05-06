"""Tests for the config/settings module.

Verifies settings loading, validation, and the secret-leak guard.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from weewx_clearskies_api.config.settings import (
    ApiSettings,
    HealthSettings,
    LoggingSettings,
    RateLimitSettings,
    DatabaseSettings,
    Settings,
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
