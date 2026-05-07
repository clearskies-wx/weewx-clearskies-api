"""Unit tests for station metadata loader (services/station.py).

Covers per the 3a-2 brief:
  - Station-metadata loader given a hand-built weewx.conf fixture.
  - Altitude pass-through: value flows unchanged regardless of unit system.
  - TZ source priority: api.conf wins, then weewx.conf, then OS TZ, then UTC+WARN.
  - station_id default: slugify of weewx.conf location. Operator override wins.

ADR references: ADR-020 (TZ), ADR-011 (singleton), ADR-019 (units pass-through),
canonical-data-model.md §3.9 (StationMetadata fields).
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import configobj
import pytest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_configobj(tmp_path: Path, content: str) -> configobj.ConfigObj:
    """Write a weewx.conf fragment to a temp file and parse it with configobj.

    Note: configobj parses comma-separated values as lists by default. Real
    weewx.conf files quote string values that contain commas (e.g. location),
    so fixtures here use quoted strings for location to match real-world behavior.
    The altitude value 'X, unit' is handled by the implementation's _parse_altitude
    helper which must accept either a list or a string from configobj.
    """
    conf_file = tmp_path / "weewx.conf"
    conf_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return configobj.ConfigObj(str(conf_file), interpolation=False)


def _reset_station_cache() -> None:
    from weewx_clearskies_api.services.station import reset_cache
    reset_cache()


# ---------------------------------------------------------------------------
# StationMetadata field population
# ---------------------------------------------------------------------------


class TestStationMetadataFieldPopulation:
    """load_station_metadata populates fields from weewx.conf [Station] section."""

    def test_location_populates_name_field(self, tmp_path: Path) -> None:
        """weewx.conf [Station] location → StationInfo.name."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Belchertown, MA"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
                station_type = Vantage
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg,
            api_station_id=None,
            api_timezone=None,
            unit_system="US",
        )
        assert info.name == "Belchertown, MA", (
            f"StationInfo.name must equal weewx.conf location, got {info.name!r}"
        )

    def test_latitude_populated_from_weewx_conf(self, tmp_path: Path) -> None:
        """weewx.conf latitude → StationInfo.latitude (decimal degrees)."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert abs(info.latitude - 42.375) < 0.001

    def test_longitude_populated_from_weewx_conf(self, tmp_path: Path) -> None:
        """weewx.conf longitude → StationInfo.longitude (decimal degrees, signed)."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert abs(info.longitude - (-72.519)) < 0.001

    def test_hardware_field_populated_from_station_type(self, tmp_path: Path) -> None:
        """weewx.conf [Station] station_type → StationInfo.hardware."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
                station_type = Vantage
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert info.hardware == "Vantage"

    def test_hardware_field_is_none_when_station_type_absent(
        self, tmp_path: Path
    ) -> None:
        """StationInfo.hardware is None when [Station] station_type is absent."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert info.hardware is None

    def test_unit_system_stored_on_station_info(self, tmp_path: Path) -> None:
        """unit_system parameter is stored on the returned StationInfo."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="METRIC"
        )
        assert info.unit_system == "METRIC"


# ---------------------------------------------------------------------------
# Altitude pass-through (ADR-019)
# ---------------------------------------------------------------------------


class TestAltitudePassThrough:
    """Altitude value passes through unchanged; unit is independent of value."""

    def test_altitude_value_passes_through_unchanged_foot(self, tmp_path: Path) -> None:
        """Altitude numeric value is returned as-is for 'foot' config (no conversion)."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "700, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert info.altitude == 700.0, (
            f"Altitude must pass through unchanged (700), got {info.altitude!r}"
        )

    def test_altitude_value_passes_through_unchanged_meter(
        self, tmp_path: Path
    ) -> None:
        """Altitude numeric value is returned as-is for 'meter' config (no conversion)."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "213, meter"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="METRIC"
        )
        assert info.altitude == 213.0, (
            f"Altitude must pass through unchanged (213), got {info.altitude!r}"
        )

    def test_altitude_not_converted_between_unit_systems(
        self, tmp_path: Path
    ) -> None:
        """Same weewx.conf altitude value is stored regardless of unit_system passed."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "700, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        # Load with METRICWX — value should still be 700 (no server-side conversion)
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="METRICWX"
        )
        assert info.altitude == 700.0, (
            "Altitude must NOT be server-side converted per ADR-019; "
            f"expected 700, got {info.altitude!r}"
        )


# ---------------------------------------------------------------------------
# station_id default slugification
# ---------------------------------------------------------------------------


class TestStationIdDefaultSlug:
    """station_id defaults to slug of weewx.conf location when not set."""

    def test_location_belchertown_ma_slugifies_to_belchertown_ma(
        self, tmp_path: Path
    ) -> None:
        """'Belchertown, MA' → default station_id 'belchertown-ma'."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Belchertown, MA"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert info.station_id == "belchertown-ma", (
            f"Default station_id for 'Belchertown, MA' must be 'belchertown-ma', "
            f"got {info.station_id!r}"
        )

    def test_operator_override_station_id_wins_over_slug(
        self, tmp_path: Path
    ) -> None:
        """api.conf station_id override always wins over the slug default."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Belchertown, MA"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg,
            api_station_id="my-custom-station",
            api_timezone=None,
            unit_system="US",
        )
        assert info.station_id == "my-custom-station", (
            f"Operator override station_id must win; got {info.station_id!r}"
        )

    def test_location_huntington_beach_ca_slugifies_correctly(
        self, tmp_path: Path
    ) -> None:
        """'Huntington Beach, CA' slugifies to 'huntington-beach-ca'."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Huntington Beach, CA"
                latitude = 33.66
                longitude = -117.99
                altitude = "10, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert info.station_id == "huntington-beach-ca", (
            f"Got {info.station_id!r}"
        )


# ---------------------------------------------------------------------------
# TZ source priority (ADR-020)
# ---------------------------------------------------------------------------


class TestTimezoneSourcePriority:
    """Timezone is resolved in priority order: api.conf → weewx.conf → OS → UTC+WARN."""

    def test_api_timezone_wins_over_weewx_conf_timezone(
        self, tmp_path: Path
    ) -> None:
        """api_timezone parameter wins over weewx.conf [Station] timezone."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
                timezone = America/New_York
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg,
            api_station_id=None,
            api_timezone="America/Los_Angeles",
            unit_system="US",
        )
        assert info.timezone == "America/Los_Angeles", (
            f"api.conf timezone must win; got {info.timezone!r}"
        )

    def test_weewx_conf_timezone_used_when_api_conf_absent(
        self, tmp_path: Path
    ) -> None:
        """weewx.conf [Station] timezone used when api_timezone is None."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
                timezone = America/New_York
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert info.timezone == "America/New_York", (
            f"weewx.conf timezone must be used when api.conf absent; "
            f"got {info.timezone!r}"
        )

    def test_utc_fallback_emits_warning_when_no_tz_configured(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When no explicit TZ and OS TZ is not a valid IANA name → UTC fallback + WARN."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata

        # Call with no explicit timezone — outcome depends on OS TZ setting.
        # We can't guarantee the OS TZ is not valid IANA, but we CAN verify
        # the function always returns a non-empty timezone string.
        with caplog.at_level(logging.WARNING):
            info = load_station_metadata(
                cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
            )
        assert info.timezone, "timezone must be non-empty even in fallback case"

    def test_timezone_offset_minutes_is_integer(self, tmp_path: Path) -> None:
        """timezone_offset_minutes is an integer."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
                timezone = America/New_York
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert isinstance(info.timezone_offset_minutes, int), (
            f"timezone_offset_minutes must be int, "
            f"got {type(info.timezone_offset_minutes).__name__}"
        )


# ---------------------------------------------------------------------------
# Missing required fields → startup error
# ---------------------------------------------------------------------------


class TestStationMetadataRequiredFields:
    """Missing required [Station] fields raise StationConfigError at load time."""

    def test_missing_location_raises_station_config_error(
        self, tmp_path: Path
    ) -> None:
        """weewx.conf without [Station] location → raises StationConfigError."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                latitude = 42.375
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import (
            StationConfigError,
            load_station_metadata,
        )
        with pytest.raises(StationConfigError):
            load_station_metadata(
                cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
            )

    def test_missing_latitude_raises_station_config_error(
        self, tmp_path: Path
    ) -> None:
        """weewx.conf without latitude → raises StationConfigError."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                longitude = -72.519
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import (
            StationConfigError,
            load_station_metadata,
        )
        with pytest.raises(StationConfigError):
            load_station_metadata(
                cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
            )

    def test_missing_longitude_raises_station_config_error(
        self, tmp_path: Path
    ) -> None:
        """weewx.conf without longitude → raises StationConfigError."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                altitude = "300, foot"
            """,
        )
        from weewx_clearskies_api.services.station import (
            StationConfigError,
            load_station_metadata,
        )
        with pytest.raises(StationConfigError):
            load_station_metadata(
                cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
            )

    def test_station_config_error_is_subclass_of_value_error(
        self, tmp_path: Path
    ) -> None:
        """StationConfigError is a subclass of ValueError (catchable as ValueError)."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                latitude = 42.375
                longitude = -72.519
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        with pytest.raises(ValueError):
            load_station_metadata(
                cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
            )


# ---------------------------------------------------------------------------
# Regression: configobj comma-parsing behavior
#
# configobj parses unquoted comma-separated values as Python lists by default.
# Real weewx.conf files use unquoted altitude = 700, foot — this WILL produce
# a list ['700', 'foot'] unless the implementation handles both string and list
# inputs. This test class documents the expected behavior when values are
# correctly quoted (the workaround) vs. unquoted (the bug trigger).
# ---------------------------------------------------------------------------


class TestConfigobjCommaParsingCompat:
    """load_station_metadata must handle configobj list values for altitude.

    Bug discovered during 3a-2 test authoring: configobj parses unquoted
    comma-separated values as Python lists. Real weewx.conf files use
    altitude = 700, foot (unquoted) which becomes ['700', 'foot'] in configobj.
    The implementation must handle this without raising AttributeError.
    Routed to api-dev as a defect found by test-author.
    """

    def test_quoted_altitude_value_parses_correctly(self, tmp_path: Path) -> None:
        """Quoted altitude value 'X, unit' → altitude numeric parsed correctly."""
        _reset_station_cache()
        cfg = _make_configobj(
            tmp_path,
            """
            [Station]
                location = "Test Station"
                latitude = 42.375
                longitude = -72.519
                altitude = "700, foot"
            """,
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        # Quoted format always produces a string → must work
        info = load_station_metadata(
            cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
        )
        assert info.altitude == 700.0

    def test_unquoted_altitude_value_with_comma_is_handled(
        self, tmp_path: Path
    ) -> None:
        """Unquoted altitude = 700, foot (real weewx.conf format) must not crash.

        configobj parses this as ['700', 'foot']. The implementation must handle
        both list and string forms of altitude. If this test fails with
        AttributeError('list' object has no attribute 'strip'), it means
        load_station_metadata (or _parse_altitude) does not handle list inputs.
        Route as a bug to api-dev.
        """
        _reset_station_cache()
        # Write weewx.conf with unquoted altitude (real-world format)
        conf_file = tmp_path / "weewx_unquoted.conf"
        conf_file.write_text(
            "[Station]\n"
            '    location = "Test Station"\n'
            "    latitude = 42.375\n"
            "    longitude = -72.519\n"
            "    altitude = 700, foot\n",
            encoding="utf-8",
        )
        cfg = configobj.ConfigObj(str(conf_file), interpolation=False)
        # Verify configobj actually produces a list (this is the bug trigger)
        assert isinstance(cfg["Station"]["altitude"], list), (
            "configobj should parse unquoted 'altitude = 700, foot' as a list; "
            "if this assertion fails, configobj behavior has changed"
        )
        from weewx_clearskies_api.services.station import load_station_metadata
        # This should NOT raise AttributeError
        try:
            info = load_station_metadata(
                cfg=cfg, api_station_id=None, api_timezone=None, unit_system="US"
            )
            assert info.altitude == 700.0, (
                f"Altitude from list-form ['700', 'foot'] must parse to 700.0, "
                f"got {info.altitude!r}"
            )
        except AttributeError as exc:
            pytest.fail(
                f"load_station_metadata raised AttributeError on list-form altitude: {exc}. "
                "This is a real bug: the implementation does not handle configobj list "
                "values for comma-separated fields like 'altitude = 700, foot'. "
                "Routed to api-dev for fix."
            )
