"""Tests for wire_radar_settings() in endpoints/radar.py (3b-15).

Verifies:
  - wire_radar_settings() populates module-level credential vars for aeris.
  - wire_radar_settings() populates module-level credential var for openweathermap.
  - Missing credentials logs ERROR but does NOT prevent startup (degraded path —
    /radar/.../tiles returns 502 at request time).
  - Keyless providers (rainviewer, iem_nexrad, etc.) → no-op (nothing to wire).
  - radar section absent → no-op.
  - provider absent → no-op.

ADR references: ADR-027, ADR-037.
Brief: phase-2-task-3b-15-radar-keyed-A2-brief.md §Settings + dispatch wiring.
"""

from __future__ import annotations

import logging


class _FakeForecastSettings:
    """Minimal forecast settings object for wiring tests."""

    def __init__(
        self,
        aeris_client_id: str | None = None,
        aeris_client_secret: str | None = None,
        openweathermap_appid: str | None = None,
    ) -> None:
        self.aeris_client_id = aeris_client_id
        self.aeris_client_secret = aeris_client_secret
        self.openweathermap_appid = openweathermap_appid


class _FakeRadarSettings:
    """Minimal radar settings object for wiring tests."""

    def __init__(self, provider: str | None) -> None:
        self.provider = provider


class _FakeSettings:
    """Minimal top-level settings object for wiring tests."""

    def __init__(
        self,
        radar_provider: str | None = None,
        forecast: _FakeForecastSettings | None = None,
    ) -> None:
        self.radar = _FakeRadarSettings(radar_provider)
        self.forecast = forecast


def _get_radar_endpoint():
    """Import the radar endpoint module (allows late import after impl exists)."""
    from weewx_clearskies_api.endpoints import radar as _radar_endpoint  # noqa: PLC0415
    return _radar_endpoint


def _reset_radar_module_vars() -> None:
    """Reset module-level credential vars to None between tests."""
    mod = _get_radar_endpoint()
    mod._RADAR_AERIS_CLIENT_ID = None
    mod._RADAR_AERIS_CLIENT_SECRET = None
    mod._RADAR_OWM_APPID = None


# ===========================================================================
# wire_radar_settings() — Aeris keyed provider
# ===========================================================================


class TestWireRadarSettingsAeris:
    """wire_radar_settings() populates Aeris credential vars when provider='aeris'."""

    def setup_method(self) -> None:
        _reset_radar_module_vars()

    def teardown_method(self) -> None:
        _reset_radar_module_vars()

    def test_aeris_provider_wires_client_id(self) -> None:
        """wire_radar_settings() sets _RADAR_AERIS_CLIENT_ID from forecast section."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(
            radar_provider="aeris",
            forecast=_FakeForecastSettings(
                aeris_client_id="my_client_id",
                aeris_client_secret="my_client_secret",
            ),
        )
        mod.wire_radar_settings(settings)
        assert mod._RADAR_AERIS_CLIENT_ID == "my_client_id"

    def test_aeris_provider_wires_client_secret(self) -> None:
        """wire_radar_settings() sets _RADAR_AERIS_CLIENT_SECRET from forecast section."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(
            radar_provider="aeris",
            forecast=_FakeForecastSettings(
                aeris_client_id="my_client_id",
                aeris_client_secret="my_client_secret",
            ),
        )
        mod.wire_radar_settings(settings)
        assert mod._RADAR_AERIS_CLIENT_SECRET == "my_client_secret"

    def test_aeris_provider_missing_forecast_section_logs_error(
        self, caplog: "pytest.LogCaptureFixture"
    ) -> None:
        """provider=aeris but no forecast section → ERROR logged, no exception raised."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(radar_provider="aeris", forecast=None)

        with caplog.at_level(logging.ERROR):
            mod.wire_radar_settings(settings)  # must not raise

        assert any("aeris" in r.message.lower() for r in caplog.records), (
            "An error-level log about missing aeris credentials must be emitted"
        )
        # Credential vars remain None (nothing to wire)
        assert mod._RADAR_AERIS_CLIENT_ID is None
        assert mod._RADAR_AERIS_CLIENT_SECRET is None

    def test_aeris_provider_empty_credentials_logs_error(
        self, caplog: "pytest.LogCaptureFixture"
    ) -> None:
        """provider=aeris with empty credentials → ERROR logged, startup not blocked."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(
            radar_provider="aeris",
            forecast=_FakeForecastSettings(aeris_client_id=None, aeris_client_secret=None),
        )

        with caplog.at_level(logging.ERROR):
            mod.wire_radar_settings(settings)  # must not raise

        assert any("aeris" in r.message.lower() for r in caplog.records)

    def test_aeris_wiring_does_not_set_owm_appid(self) -> None:
        """wire_radar_settings() with aeris does not touch _RADAR_OWM_APPID."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(
            radar_provider="aeris",
            forecast=_FakeForecastSettings(
                aeris_client_id="cid",
                aeris_client_secret="csec",
            ),
        )
        mod.wire_radar_settings(settings)
        assert mod._RADAR_OWM_APPID is None


# ===========================================================================
# wire_radar_settings() — OWM keyed provider
# ===========================================================================


class TestWireRadarSettingsOWM:
    """wire_radar_settings() populates OWM appid when provider='openweathermap'."""

    def setup_method(self) -> None:
        _reset_radar_module_vars()

    def teardown_method(self) -> None:
        _reset_radar_module_vars()

    def test_openweathermap_provider_wires_appid(self) -> None:
        """wire_radar_settings() sets _RADAR_OWM_APPID from forecast section."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(
            radar_provider="openweathermap",
            forecast=_FakeForecastSettings(openweathermap_appid="test_owm_appid"),
        )
        mod.wire_radar_settings(settings)
        assert mod._RADAR_OWM_APPID == "test_owm_appid"

    def test_openweathermap_provider_missing_forecast_section_logs_error(
        self, caplog: "pytest.LogCaptureFixture"
    ) -> None:
        """provider=openweathermap but no forecast section → ERROR logged, no raise."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(radar_provider="openweathermap", forecast=None)

        with caplog.at_level(logging.ERROR):
            mod.wire_radar_settings(settings)  # must not raise

        assert any("openweathermap" in r.message.lower() for r in caplog.records)
        assert mod._RADAR_OWM_APPID is None

    def test_openweathermap_empty_appid_logs_error(
        self, caplog: "pytest.LogCaptureFixture"
    ) -> None:
        """provider=openweathermap with missing appid → ERROR logged, startup not blocked."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(
            radar_provider="openweathermap",
            forecast=_FakeForecastSettings(openweathermap_appid=None),
        )

        with caplog.at_level(logging.ERROR):
            mod.wire_radar_settings(settings)  # must not raise

        assert any("openweathermap" in r.message.lower() for r in caplog.records)

    def test_owm_wiring_does_not_set_aeris_credentials(self) -> None:
        """wire_radar_settings() with openweathermap does not touch Aeris vars."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(
            radar_provider="openweathermap",
            forecast=_FakeForecastSettings(openweathermap_appid="my_appid"),
        )
        mod.wire_radar_settings(settings)
        assert mod._RADAR_AERIS_CLIENT_ID is None
        assert mod._RADAR_AERIS_CLIENT_SECRET is None


# ===========================================================================
# wire_radar_settings() — keyless providers (no-op)
# ===========================================================================


class TestWireRadarSettingsKeylessProviders:
    """Keyless providers → no-op; no credentials to wire."""

    def setup_method(self) -> None:
        _reset_radar_module_vars()

    def teardown_method(self) -> None:
        _reset_radar_module_vars()

    def test_rainviewer_provider_does_not_set_credentials(self) -> None:
        """provider=rainviewer → wire_radar_settings() is a no-op for credentials."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(radar_provider="rainviewer")
        mod.wire_radar_settings(settings)
        assert mod._RADAR_AERIS_CLIENT_ID is None
        assert mod._RADAR_AERIS_CLIENT_SECRET is None
        assert mod._RADAR_OWM_APPID is None

    def test_iem_nexrad_provider_does_not_set_credentials(self) -> None:
        """provider=iem_nexrad → wire_radar_settings() is a no-op for credentials."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(radar_provider="iem_nexrad")
        mod.wire_radar_settings(settings)
        assert mod._RADAR_OWM_APPID is None
        assert mod._RADAR_AERIS_CLIENT_ID is None


# ===========================================================================
# wire_radar_settings() — radar section absent or provider absent
# ===========================================================================


class TestWireRadarSettingsNoop:
    """Missing radar section or missing provider → no-op."""

    def setup_method(self) -> None:
        _reset_radar_module_vars()

    def teardown_method(self) -> None:
        _reset_radar_module_vars()

    def test_no_radar_section_is_noop(self) -> None:
        """Settings object without a radar attribute → wire_radar_settings() is a no-op."""
        mod = _get_radar_endpoint()

        class _NoRadarSettings:
            pass

        mod.wire_radar_settings(_NoRadarSettings())
        assert mod._RADAR_AERIS_CLIENT_ID is None
        assert mod._RADAR_OWM_APPID is None

    def test_provider_none_is_noop(self) -> None:
        """provider=None (radar not configured) → wire_radar_settings() is a no-op."""
        mod = _get_radar_endpoint()
        settings = _FakeSettings(radar_provider=None)
        mod.wire_radar_settings(settings)
        assert mod._RADAR_AERIS_CLIENT_ID is None
        assert mod._RADAR_OWM_APPID is None
