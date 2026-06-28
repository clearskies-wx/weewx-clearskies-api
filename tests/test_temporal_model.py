"""Unit tests for the ADR-075 temporal model infrastructure.

Covers:
  Group 1 — build_station_clock(): IANA timezone, date/time format, UTC offset.
  Group 2 — build_freshness() per domain: refreshInterval, generatedAt/validUntil shape.
  Group 3 — build_freshness() with provider_refresh_interval: min(config, provider) logic.
  Group 4 — FreshnessSettings: defaults, archive_interval derivation, config overrides.
  Group 5 — Representative endpoint integration: stationClock + freshness on GET /api/v1/station.

ADR references: ADR-075 §3 (stationClock), §4 (freshness envelope).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from weewx_clearskies_api.config.settings import FreshnessSettings
from weewx_clearskies_api.models.responses import FreshnessInfo, StationClock
from weewx_clearskies_api.services import freshness as freshness_mod
from weewx_clearskies_api.services import station as station_mod
from weewx_clearskies_api.services.station import StationInfo, reset_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _make_station_info(timezone: str = "America/New_York") -> StationInfo:
    """Return a StationInfo with the given timezone and realistic field values."""
    return StationInfo(
        station_id="belchertown-ma",
        name="Belchertown, MA",
        latitude=42.375,
        longitude=-72.519,
        altitude=300.0,
        timezone=timezone,
        timezone_offset_minutes=-240,
        unit_system="US",
        hardware="Davis Vantage Pro2",
        default_locale="en",
        archive_interval=300,
        week_start=6,
    )


def _wire_station(tz: str = "America/New_York") -> None:
    """Install a test StationInfo into the module-level cache."""
    reset_cache()
    station_mod._cached_station = _make_station_info(tz)


def _wire_freshness_settings(section: dict | None = None, archive_interval: int = 300) -> FreshnessSettings:
    """Build a FreshnessSettings and install it in the freshness module."""
    fs = FreshnessSettings(section or {}, archive_interval=archive_interval)
    freshness_mod.configure(fs)
    return fs


# ---------------------------------------------------------------------------
# Group 1: build_station_clock()
# ---------------------------------------------------------------------------


class TestBuildStationClock:
    """build_station_clock() returns a StationClock computed from the station timezone."""

    def setup_method(self) -> None:
        _wire_station("America/New_York")

    def test_build_station_clock_returns_station_clock_instance(self) -> None:
        """build_station_clock() returns a StationClock Pydantic model."""
        from weewx_clearskies_api.services.station import build_station_clock
        clock = build_station_clock()
        assert isinstance(clock, StationClock)

    def test_build_station_clock_date_is_yyyy_mm_dd_format(self) -> None:
        """StationClock.date is formatted as YYYY-MM-DD."""
        from weewx_clearskies_api.services.station import build_station_clock
        clock = build_station_clock()
        assert _DATE_RE.match(clock.date), (
            f"StationClock.date must be YYYY-MM-DD, got {clock.date!r}"
        )

    def test_build_station_clock_time_includes_utc_offset(self) -> None:
        """StationClock.time is ISO-8601 and includes a UTC offset (not bare Z)."""
        from weewx_clearskies_api.services.station import build_station_clock
        clock = build_station_clock()
        # station-local ISO includes +HH:MM or -HH:MM offset, not bare Z
        assert "T" in clock.time, f"StationClock.time must include 'T', got {clock.time!r}"
        # Must contain a sign character after the time portion (UTC offset)
        # isoformat() on a timezone-aware datetime always includes offset
        assert "+" in clock.time or clock.time.count("-") >= 3, (
            f"StationClock.time must include UTC offset, got {clock.time!r}"
        )

    def test_build_station_clock_timezone_field_is_iana_identifier(self) -> None:
        """StationClock.timezone is the station's IANA timezone identifier."""
        from weewx_clearskies_api.services.station import build_station_clock
        clock = build_station_clock()
        assert clock.timezone == "America/New_York", (
            f"Expected IANA tz 'America/New_York', got {clock.timezone!r}"
        )

    def test_build_station_clock_uses_station_timezone_not_utc(self) -> None:
        """StationClock.date matches the station-local date, not the UTC date."""
        from weewx_clearskies_api.services.station import build_station_clock

        # Wire a station with an extreme offset to maximise the chance of a
        # date boundary: UTC+13 (Samoa/Tonga business time) puts local date
        # one day ahead of UTC on most hours of the day.
        _wire_station("Pacific/Apia")  # UTC+13 / UTC+14
        clock = build_station_clock()

        # Compute what the station-local date should be right now
        zi = ZoneInfo("Pacific/Apia")
        expected_local_date = datetime.now(tz=zi).strftime("%Y-%m-%d")
        assert clock.date == expected_local_date, (
            f"StationClock.date must be station-local ({expected_local_date!r}), "
            f"got {clock.date!r}"
        )

    def test_build_station_clock_date_matches_station_local_time_for_new_york(self) -> None:
        """StationClock.date for America/New_York matches expected station-local date."""
        from weewx_clearskies_api.services.station import build_station_clock

        _wire_station("America/New_York")
        clock = build_station_clock()
        zi = ZoneInfo("America/New_York")
        expected_date = datetime.now(tz=zi).strftime("%Y-%m-%d")
        assert clock.date == expected_date, (
            f"Date mismatch: expected {expected_date!r}, got {clock.date!r}"
        )

    def test_build_station_clock_time_parses_as_iso_datetime(self) -> None:
        """StationClock.time is parseable as an ISO-8601 datetime with tzinfo."""
        from weewx_clearskies_api.services.station import build_station_clock

        _wire_station("America/Chicago")  # UTC-5 / UTC-6
        clock = build_station_clock()
        # datetime.fromisoformat handles "+HH:MM" offset syntax
        parsed = datetime.fromisoformat(clock.time)
        assert parsed.tzinfo is not None, (
            f"StationClock.time must include tzinfo, got {clock.time!r}"
        )

    def test_build_station_clock_utc_timezone_returns_z_offset_in_time(self) -> None:
        """StationClock for UTC station includes +00:00 (or Z) in the time field."""
        from weewx_clearskies_api.services.station import build_station_clock

        _wire_station("UTC")
        clock = build_station_clock()
        assert clock.timezone == "UTC"
        parsed = datetime.fromisoformat(clock.time)
        assert parsed.utcoffset() == timedelta(0), (
            f"UTC station must yield zero UTC offset, got {parsed.utcoffset()!r}"
        )

    def test_build_station_clock_raises_without_cache(self) -> None:
        """build_station_clock() raises RuntimeError when station cache is unset."""
        from weewx_clearskies_api.services.station import build_station_clock
        reset_cache()
        with pytest.raises(RuntimeError, match="Station metadata not loaded"):
            build_station_clock()


# ---------------------------------------------------------------------------
# Group 2: build_freshness() per domain
# ---------------------------------------------------------------------------


class TestBuildFreshnessPerDomain:
    """build_freshness() returns correct refreshInterval and UTC ISO-8601 Z timestamps."""

    def setup_method(self) -> None:
        _wire_freshness_settings(archive_interval=300)

    def _assert_freshness_shape(self, info: FreshnessInfo, expected_interval: int) -> None:
        """Assert FreshnessInfo has valid shape and the expected refreshInterval."""
        assert isinstance(info, FreshnessInfo)
        assert _UTC_ISO_Z_RE.match(info.generatedAt), (
            f"generatedAt must be UTC ISO-8601 Z, got {info.generatedAt!r}"
        )
        assert _UTC_ISO_Z_RE.match(info.validUntil), (
            f"validUntil must be UTC ISO-8601 Z, got {info.validUntil!r}"
        )
        assert info.refreshInterval == expected_interval, (
            f"Expected refreshInterval={expected_interval}, got {info.refreshInterval}"
        )

    def _parse_utc(self, s: str) -> datetime:
        """Parse a UTC ISO-8601 Z string to a timezone-aware datetime."""
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def test_build_freshness_current_observation_uses_archive_interval(self) -> None:
        """current_observation refreshInterval matches the archive_interval default (300s)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("current_observation")
        self._assert_freshness_shape(info, 300)

    def test_build_freshness_forecast_interval_is_1800(self) -> None:
        """forecast refreshInterval defaults to 1800 seconds."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("forecast")
        self._assert_freshness_shape(info, 1800)

    def test_build_freshness_alerts_interval_is_300(self) -> None:
        """alerts refreshInterval defaults to 300 seconds."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("alerts")
        self._assert_freshness_shape(info, 300)

    def test_build_freshness_aqi_interval_is_900(self) -> None:
        """aqi refreshInterval defaults to 900 seconds."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("aqi")
        self._assert_freshness_shape(info, 900)

    def test_build_freshness_almanac_daily_interval_is_86400(self) -> None:
        """almanac_daily refreshInterval defaults to 86400 seconds (24 h)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("almanac_daily")
        self._assert_freshness_shape(info, 86400)

    def test_build_freshness_almanac_positions_interval_is_60(self) -> None:
        """almanac_positions refreshInterval defaults to 60 seconds."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("almanac_positions")
        self._assert_freshness_shape(info, 60)

    def test_build_freshness_radar_interval_is_300(self) -> None:
        """radar refreshInterval defaults to 300 seconds."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("radar")
        self._assert_freshness_shape(info, 300)

    def test_build_freshness_earthquakes_interval_is_300(self) -> None:
        """earthquakes refreshInterval defaults to 300 seconds."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("earthquakes")
        self._assert_freshness_shape(info, 300)

    def test_build_freshness_records_uses_archive_interval(self) -> None:
        """records refreshInterval matches the archive_interval default (300s)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("records")
        self._assert_freshness_shape(info, 300)

    def test_build_freshness_charts_config_interval_is_86400(self) -> None:
        """charts_config refreshInterval defaults to 86400 seconds (24 h)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("charts_config")
        self._assert_freshness_shape(info, 86400)

    def test_build_freshness_station_metadata_interval_is_86400(self) -> None:
        """station_metadata refreshInterval defaults to 86400 seconds (24 h)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("station_metadata")
        self._assert_freshness_shape(info, 86400)

    def test_build_freshness_seeing_interval_is_10800(self) -> None:
        """seeing refreshInterval defaults to 10800 seconds (3 h)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("seeing")
        self._assert_freshness_shape(info, 10800)

    def test_build_freshness_valid_until_is_after_generated_at(self) -> None:
        """validUntil is strictly after generatedAt for all domains."""
        from weewx_clearskies_api.services.freshness import build_freshness
        for domain in (
            "current_observation", "forecast", "alerts", "aqi",
            "almanac_daily", "almanac_positions", "radar", "earthquakes",
            "records", "charts_config", "station_metadata", "seeing",
        ):
            info = build_freshness(domain)
            generated = self._parse_utc(info.generatedAt)
            valid = self._parse_utc(info.validUntil)
            assert valid > generated, (
                f"validUntil must be after generatedAt for domain={domain!r}: "
                f"{info.validUntil!r} vs {info.generatedAt!r}"
            )

    def test_build_freshness_valid_until_is_generated_at_plus_refresh_interval(self) -> None:
        """validUntil = generatedAt + refreshInterval (within 2 seconds of rounding)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("forecast")
        generated = self._parse_utc(info.generatedAt)
        valid = self._parse_utc(info.validUntil)
        delta_seconds = int((valid - generated).total_seconds())
        assert delta_seconds == info.refreshInterval, (
            f"validUntil - generatedAt must equal refreshInterval "
            f"({info.refreshInterval}s), got {delta_seconds}s"
        )

    def test_build_freshness_generated_at_is_near_utc_now(self) -> None:
        """generatedAt is within 5 seconds of the current UTC time."""
        from weewx_clearskies_api.services.freshness import build_freshness
        before = datetime.now(tz=UTC)
        info = build_freshness("alerts")
        after = datetime.now(tz=UTC)
        generated = self._parse_utc(info.generatedAt)
        assert before <= generated <= after or (
            (generated - before).total_seconds() < 5
        ), (
            f"generatedAt must be near current UTC time, got {info.generatedAt!r}"
        )

    def test_build_freshness_records_with_nondefault_archive_interval(self) -> None:
        """records domain uses archive_interval when non-default (e.g. 600s)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        _wire_freshness_settings(archive_interval=600)
        info = build_freshness("records")
        assert info.refreshInterval == 600, (
            f"records refreshInterval must equal archive_interval=600, got {info.refreshInterval}"
        )

    def test_build_freshness_current_observation_with_nondefault_archive_interval(self) -> None:
        """current_observation domain uses archive_interval when non-default (e.g. 600s)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        _wire_freshness_settings(archive_interval=600)
        info = build_freshness("current_observation")
        assert info.refreshInterval == 600, (
            f"current_observation refreshInterval must equal archive_interval=600, "
            f"got {info.refreshInterval}"
        )


# ---------------------------------------------------------------------------
# Group 3: build_freshness() with provider_refresh_interval
# ---------------------------------------------------------------------------


class TestBuildFreshnessProviderRefreshInterval:
    """build_freshness() applies min(config_default, provider_refresh_interval) logic."""

    def setup_method(self) -> None:
        # forecast default = 1800; use it as the config baseline
        _wire_freshness_settings(archive_interval=300)

    def test_provider_interval_less_than_config_uses_provider_interval(self) -> None:
        """When provider_refresh_interval < config default, the provider value wins."""
        from weewx_clearskies_api.services.freshness import build_freshness
        # forecast default = 1800; provider = 600 < 1800 → use 600
        info = build_freshness("forecast", provider_refresh_interval=600)
        assert info.refreshInterval == 600, (
            f"Expected min(1800, 600) = 600, got {info.refreshInterval}"
        )

    def test_provider_interval_greater_than_config_uses_config_interval(self) -> None:
        """When provider_refresh_interval > config default, the config value wins."""
        from weewx_clearskies_api.services.freshness import build_freshness
        # forecast default = 1800; provider = 3600 > 1800 → use 1800
        info = build_freshness("forecast", provider_refresh_interval=3600)
        assert info.refreshInterval == 1800, (
            f"Expected min(1800, 3600) = 1800, got {info.refreshInterval}"
        )

    def test_provider_interval_equal_to_config_uses_either_value(self) -> None:
        """When provider_refresh_interval == config default, the result equals that value."""
        from weewx_clearskies_api.services.freshness import build_freshness
        # forecast default = 1800; provider = 1800 → use 1800
        info = build_freshness("forecast", provider_refresh_interval=1800)
        assert info.refreshInterval == 1800, (
            f"Expected min(1800, 1800) = 1800, got {info.refreshInterval}"
        )

    def test_provider_interval_none_uses_config_default(self) -> None:
        """When provider_refresh_interval is None, the config default applies."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("forecast", provider_refresh_interval=None)
        assert info.refreshInterval == 1800, (
            f"Expected config default 1800 when provider=None, got {info.refreshInterval}"
        )

    def test_provider_interval_for_alerts_domain(self) -> None:
        """provider_refresh_interval min-selection applies to alerts domain."""
        from weewx_clearskies_api.services.freshness import build_freshness
        # alerts default = 300; provider = 120 < 300 → use 120
        info = build_freshness("alerts", provider_refresh_interval=120)
        assert info.refreshInterval == 120, (
            f"Expected min(300, 120) = 120, got {info.refreshInterval}"
        )

    def test_provider_interval_for_aqi_domain(self) -> None:
        """provider_refresh_interval min-selection applies to aqi domain."""
        from weewx_clearskies_api.services.freshness import build_freshness
        # aqi default = 900; provider = 1800 > 900 → use 900
        info = build_freshness("aqi", provider_refresh_interval=1800)
        assert info.refreshInterval == 900, (
            f"Expected min(900, 1800) = 900, got {info.refreshInterval}"
        )

    def test_provider_interval_for_current_observation_domain(self) -> None:
        """provider_refresh_interval applies to current_observation domain."""
        from weewx_clearskies_api.services.freshness import build_freshness
        # current_observation default = 300 (archive_interval); provider = 60 < 300 → use 60
        info = build_freshness("current_observation", provider_refresh_interval=60)
        assert info.refreshInterval == 60, (
            f"Expected min(300, 60) = 60, got {info.refreshInterval}"
        )

    def test_provider_interval_valid_until_reflects_effective_interval(self) -> None:
        """validUntil is based on the effective interval (the min-selected value)."""
        from weewx_clearskies_api.services.freshness import build_freshness
        info = build_freshness("forecast", provider_refresh_interval=600)
        # effective interval should be 600 (min(1800, 600))
        generated = datetime.strptime(info.generatedAt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        valid = datetime.strptime(info.validUntil, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        delta_seconds = int((valid - generated).total_seconds())
        assert delta_seconds == 600, (
            f"validUntil must be generatedAt + 600, but delta was {delta_seconds}s"
        )


# ---------------------------------------------------------------------------
# Group 4: FreshnessSettings
# ---------------------------------------------------------------------------


class TestFreshnessSettingsDefaults:
    """FreshnessSettings defaults match ADR-075 §4 specification."""

    def test_default_construction_succeeds_with_empty_section(self) -> None:
        """FreshnessSettings({}) constructs without error."""
        fs = FreshnessSettings({})
        assert fs is not None

    def test_default_current_observation_is_archive_interval(self) -> None:
        """current_observation defaults to archive_interval (300s when unspecified)."""
        fs = FreshnessSettings({})
        assert fs.current_observation == 300, (
            f"Expected current_observation=300 (archive_interval default), "
            f"got {fs.current_observation}"
        )

    def test_default_current_observation_reflects_custom_archive_interval(self) -> None:
        """current_observation takes on a non-default archive_interval value."""
        fs = FreshnessSettings({}, archive_interval=600)
        assert fs.current_observation == 600, (
            f"Expected current_observation=600, got {fs.current_observation}"
        )

    def test_default_records_is_archive_interval(self) -> None:
        """records defaults to archive_interval (300s when unspecified)."""
        fs = FreshnessSettings({})
        assert fs.records == 300, (
            f"Expected records=300 (archive_interval default), got {fs.records}"
        )

    def test_default_records_reflects_custom_archive_interval(self) -> None:
        """records takes on a non-default archive_interval value."""
        fs = FreshnessSettings({}, archive_interval=600)
        assert fs.records == 600, (
            f"Expected records=600, got {fs.records}"
        )

    def test_default_forecast_is_1800(self) -> None:
        """forecast defaults to 1800 seconds."""
        assert FreshnessSettings({}).forecast == 1800

    def test_default_alerts_is_300(self) -> None:
        """alerts defaults to 300 seconds."""
        assert FreshnessSettings({}).alerts == 300

    def test_default_aqi_is_900(self) -> None:
        """aqi defaults to 900 seconds."""
        assert FreshnessSettings({}).aqi == 900

    def test_default_almanac_daily_is_86400(self) -> None:
        """almanac_daily defaults to 86400 seconds."""
        assert FreshnessSettings({}).almanac_daily == 86400

    def test_default_almanac_positions_is_60(self) -> None:
        """almanac_positions defaults to 60 seconds."""
        assert FreshnessSettings({}).almanac_positions == 60

    def test_default_radar_is_300(self) -> None:
        """radar defaults to 300 seconds."""
        assert FreshnessSettings({}).radar == 300

    def test_default_earthquakes_is_300(self) -> None:
        """earthquakes defaults to 300 seconds."""
        assert FreshnessSettings({}).earthquakes == 300

    def test_default_charts_config_is_86400(self) -> None:
        """charts_config defaults to 86400 seconds."""
        assert FreshnessSettings({}).charts_config == 86400

    def test_default_station_metadata_is_86400(self) -> None:
        """station_metadata defaults to 86400 seconds."""
        assert FreshnessSettings({}).station_metadata == 86400

    def test_default_seeing_is_10800(self) -> None:
        """seeing defaults to 10800 seconds (3 h)."""
        assert FreshnessSettings({}).seeing == 10800

    def test_default_idle_timeout_is_30_minutes(self) -> None:
        """idle_timeout defaults to 30 minutes (ADR-075 T1.6)."""
        assert FreshnessSettings({}).idle_timeout == 30, (
            f"Expected idle_timeout=30, got {FreshnessSettings({}).idle_timeout}"
        )

    def test_default_idle_refresh_factor_is_10(self) -> None:
        """idle_refresh_factor defaults to 10 (ADR-075 T1.6)."""
        assert FreshnessSettings({}).idle_refresh_factor == 10, (
            f"Expected idle_refresh_factor=10, got {FreshnessSettings({}).idle_refresh_factor}"
        )

    def test_defaults_pass_validate(self) -> None:
        """Default FreshnessSettings passes validate() without error."""
        FreshnessSettings({}).validate()  # must not raise


class TestFreshnessSettingsConfigOverride:
    """FreshnessSettings reads custom values from a [freshness] config section."""

    def test_config_overrides_forecast_interval(self) -> None:
        """[freshness] forecast = 900 overrides the static default of 1800."""
        fs = FreshnessSettings({"forecast": "900"})
        assert fs.forecast == 900, (
            f"Expected forecast=900 after override, got {fs.forecast}"
        )

    def test_config_overrides_alerts_interval(self) -> None:
        """[freshness] alerts = 600 overrides the static default of 300."""
        fs = FreshnessSettings({"alerts": "600"})
        assert fs.alerts == 600

    def test_config_overrides_current_observation_ignoring_archive_interval(self) -> None:
        """[freshness] current_observation = 120 overrides the archive_interval-derived default."""
        fs = FreshnessSettings({"current_observation": "120"}, archive_interval=300)
        assert fs.current_observation == 120, (
            f"Explicit config must win over archive_interval default; got {fs.current_observation}"
        )

    def test_config_overrides_records_ignoring_archive_interval(self) -> None:
        """[freshness] records = 120 overrides the archive_interval-derived default."""
        fs = FreshnessSettings({"records": "120"}, archive_interval=300)
        assert fs.records == 120, (
            f"Explicit config must win over archive_interval default; got {fs.records}"
        )

    def test_config_overrides_idle_timeout(self) -> None:
        """[freshness] idle_timeout = 0 disables idle detection per ADR-075."""
        fs = FreshnessSettings({"idle_timeout": "0"})
        assert fs.idle_timeout == 0

    def test_config_overrides_idle_refresh_factor(self) -> None:
        """[freshness] idle_refresh_factor = 5 overrides the default of 10."""
        fs = FreshnessSettings({"idle_refresh_factor": "5"})
        assert fs.idle_refresh_factor == 5

    def test_config_overrides_seeing_interval(self) -> None:
        """[freshness] seeing = 3600 overrides the default 10800."""
        fs = FreshnessSettings({"seeing": "3600"})
        assert fs.seeing == 3600

    def test_config_overrides_pass_validate(self) -> None:
        """FreshnessSettings with all custom intervals passes validate()."""
        fs = FreshnessSettings({
            "current_observation": "120",
            "forecast": "900",
            "alerts": "150",
            "aqi": "450",
            "almanac_daily": "43200",
            "almanac_positions": "30",
            "radar": "150",
            "earthquakes": "150",
            "records": "120",
            "charts_config": "43200",
            "station_metadata": "43200",
            "seeing": "3600",
            "idle_timeout": "15",
            "idle_refresh_factor": "5",
        })
        fs.validate()  # must not raise

    def test_zero_interval_fails_validate_for_domain(self) -> None:
        """FreshnessSettings with interval=0 for a domain fails validate()."""
        fs = FreshnessSettings({"forecast": "0"})
        with pytest.raises(ValueError, match="forecast"):
            fs.validate()

    def test_negative_interval_fails_validate_for_domain(self) -> None:
        """FreshnessSettings with a negative interval for a domain fails validate()."""
        fs = FreshnessSettings({"alerts": "-1"})
        with pytest.raises(ValueError, match="alerts"):
            fs.validate()

    def test_negative_idle_refresh_factor_fails_validate(self) -> None:
        """FreshnessSettings with idle_refresh_factor < 1 fails validate()."""
        fs = FreshnessSettings({"idle_refresh_factor": "0"})
        with pytest.raises(ValueError, match="idle_refresh_factor"):
            fs.validate()

    def test_negative_idle_timeout_fails_validate(self) -> None:
        """FreshnessSettings with idle_timeout < 0 fails validate()."""
        fs = FreshnessSettings({"idle_timeout": "-1"})
        with pytest.raises(ValueError, match="idle_timeout"):
            fs.validate()


# ---------------------------------------------------------------------------
# Group 5: Representative endpoint integration
# ---------------------------------------------------------------------------


class TestStationEndpointTemporalBlocks:
    """GET /api/v1/station returns both stationClock and freshness blocks."""

    def test_station_endpoint_includes_station_clock_block(self, client) -> None:
        """GET /api/v1/station response includes a non-null stationClock block."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "stationClock" in body, "Response must include 'stationClock' key"
        assert body["stationClock"] is not None, "stationClock must not be null"

    def test_station_endpoint_station_clock_has_required_fields(self, client) -> None:
        """GET /api/v1/station stationClock block has date, time, and timezone fields."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        clock = resp.json().get("stationClock", {})
        assert "date" in clock, "stationClock must include 'date' field"
        assert "time" in clock, "stationClock must include 'time' field"
        assert "timezone" in clock, "stationClock must include 'timezone' field"

    def test_station_endpoint_station_clock_date_is_yyyy_mm_dd(self, client) -> None:
        """GET /api/v1/station stationClock.date is YYYY-MM-DD format."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        clock = resp.json().get("stationClock", {})
        date = clock.get("date", "")
        assert _DATE_RE.match(date), f"stationClock.date must be YYYY-MM-DD, got {date!r}"

    def test_station_endpoint_station_clock_timezone_matches_wired_station(self, client) -> None:
        """GET /api/v1/station stationClock.timezone matches the wired station's timezone."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        clock = resp.json().get("stationClock", {})
        # conftest._wire_test_station() sets timezone = "America/New_York"
        assert clock.get("timezone") == "America/New_York", (
            f"Expected timezone='America/New_York', got {clock.get('timezone')!r}"
        )

    def test_station_endpoint_includes_freshness_block(self, client) -> None:
        """GET /api/v1/station response includes a non-null freshness block."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        body = resp.json()
        assert "freshness" in body, "Response must include 'freshness' key"
        assert body["freshness"] is not None, "freshness must not be null"

    def test_station_endpoint_freshness_has_required_fields(self, client) -> None:
        """GET /api/v1/station freshness block has generatedAt, validUntil, refreshInterval."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        freshness = resp.json().get("freshness", {})
        assert "generatedAt" in freshness, "freshness must include 'generatedAt'"
        assert "validUntil" in freshness, "freshness must include 'validUntil'"
        assert "refreshInterval" in freshness, "freshness must include 'refreshInterval'"

    def test_station_endpoint_freshness_generated_at_is_utc_z(self, client) -> None:
        """GET /api/v1/station freshness.generatedAt is UTC ISO-8601 with Z suffix."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        freshness = resp.json().get("freshness", {})
        generated_at = freshness.get("generatedAt", "")
        assert _UTC_ISO_Z_RE.match(generated_at), (
            f"freshness.generatedAt must be UTC ISO-8601 Z, got {generated_at!r}"
        )

    def test_station_endpoint_freshness_valid_until_is_utc_z(self, client) -> None:
        """GET /api/v1/station freshness.validUntil is UTC ISO-8601 with Z suffix."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        freshness = resp.json().get("freshness", {})
        valid_until = freshness.get("validUntil", "")
        assert _UTC_ISO_Z_RE.match(valid_until), (
            f"freshness.validUntil must be UTC ISO-8601 Z, got {valid_until!r}"
        )

    def test_station_endpoint_freshness_valid_until_is_after_generated_at(self, client) -> None:
        """GET /api/v1/station freshness.validUntil is strictly after generatedAt."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        freshness = resp.json().get("freshness", {})
        generated = datetime.strptime(freshness["generatedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        valid = datetime.strptime(freshness["validUntil"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert valid > generated, (
            f"validUntil must be after generatedAt: {freshness['validUntil']!r} <= {freshness['generatedAt']!r}"
        )

    def test_station_endpoint_freshness_refresh_interval_is_positive_integer(self, client) -> None:
        """GET /api/v1/station freshness.refreshInterval is a positive integer."""
        resp = client.get("/api/v1/station")
        assert resp.status_code == 200
        freshness = resp.json().get("freshness", {})
        interval = freshness.get("refreshInterval")
        assert isinstance(interval, int), f"refreshInterval must be int, got {type(interval)}"
        assert interval > 0, f"refreshInterval must be positive, got {interval}"
