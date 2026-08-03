"""Tests for wire_imagery_settings() in endpoints/imagery.py (Phase LM, §LM-1).

Mirrors tests/test_wire_radar_settings.py's structure — closest existing
precedent. Simpler than radar's wiring (no credential extraction; imagery
is keyless at v1).

Verifies:
  - wire_imagery_settings() populates module-level provider + TTL vars.
  - imagery section absent on the settings object → no-op (defaults kept).
  - provider absent (None) → module-level provider stays None.
"""

from __future__ import annotations

from weewx_clearskies_api.endpoints import imagery as imagery_endpoint


class _FakeImagerySettings:
    def __init__(self, provider: str | None = None, tile_cache_ttl_seconds: int = 604800) -> None:
        self.provider = provider
        self.tile_cache_ttl_seconds = tile_cache_ttl_seconds


class _FakeSettings:
    def __init__(self, imagery: _FakeImagerySettings | None = None) -> None:
        self.imagery = imagery


def setup_function() -> None:
    imagery_endpoint.reset_imagery_settings_for_tests()


def teardown_function() -> None:
    imagery_endpoint.reset_imagery_settings_for_tests()


class TestWireImagerySettings:
    def test_wires_provider_and_ttl(self) -> None:
        settings = _FakeSettings(_FakeImagerySettings(provider="auto", tile_cache_ttl_seconds=3600))
        imagery_endpoint.wire_imagery_settings(settings)
        assert imagery_endpoint._imagery_provider == "auto"
        assert imagery_endpoint._imagery_tile_cache_ttl_seconds == 3600

    def test_naip_override_wired(self) -> None:
        settings = _FakeSettings(_FakeImagerySettings(provider="naip"))
        imagery_endpoint.wire_imagery_settings(settings)
        assert imagery_endpoint._imagery_provider == "naip"

    def test_esri_override_wired(self) -> None:
        settings = _FakeSettings(_FakeImagerySettings(provider="esri"))
        imagery_endpoint.wire_imagery_settings(settings)
        assert imagery_endpoint._imagery_provider == "esri"

    def test_provider_none_stays_none(self) -> None:
        settings = _FakeSettings(_FakeImagerySettings(provider=None))
        imagery_endpoint.wire_imagery_settings(settings)
        assert imagery_endpoint._imagery_provider is None

    def test_imagery_section_absent_is_noop(self) -> None:
        settings = _FakeSettings(imagery=None)
        imagery_endpoint.wire_imagery_settings(settings)
        # Defaults preserved (reset by setup_function to None/604800).
        assert imagery_endpoint._imagery_provider is None
        assert imagery_endpoint._imagery_tile_cache_ttl_seconds == 604800
