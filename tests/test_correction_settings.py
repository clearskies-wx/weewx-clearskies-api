"""Tests for ForecastCorrectionSettings in config/settings.py (ADR-079 Phase 1).

Validates default values, parsing from populated dicts, boolean coercion,
validation gate (min_samples, retention_years, retrain_day, retrain_schedule),
and the invalid-schedule-defaults-to-weekly behaviour documented in __init__.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.config.settings import ForecastCorrectionSettings


class TestForecastCorrectionSettingsDefaults:
    def test_empty_section_produces_documented_defaults(self) -> None:
        """ForecastCorrectionSettings({}) yields all documented default values."""
        s = ForecastCorrectionSettings({})

        assert s.enabled is False, "Default enabled must be False (collect before correcting)"
        assert s.collection_enabled is True, "Default collection_enabled must be True"
        assert s.retrain_schedule == "daily"
        assert s.retrain_day == 0, "Default retrain_day must be 0 (Monday)"
        assert s.min_samples == 500
        assert s.retention_years == 3
        assert s.db_path == "/etc/weewx-clearskies/forecast_correction.db"
        assert s.model_path == "/etc/weewx-clearskies/forecast_correction_model.pkl"

    def test_defaults_pass_validate_without_error(self) -> None:
        """Default settings are self-consistent and pass validate() cleanly."""
        s = ForecastCorrectionSettings({})
        s.validate()  # Must not raise


class TestForecastCorrectionSettingsParsing:
    def test_all_fields_parsed_from_explicit_dict(self) -> None:
        """Every setting is honoured when explicitly provided in the section dict."""
        s = ForecastCorrectionSettings({
            "enabled": "true",
            "collection_enabled": "false",
            "db_path": "/var/lib/weewx/correction.db",
            "model_path": "/var/lib/weewx/correction_model.pkl",
            "retrain_schedule": "daily",
            "retrain_day": "3",
            "min_samples": "200",
            "retention_years": "5",
        })

        assert s.enabled is True
        assert s.collection_enabled is False
        assert s.db_path == "/var/lib/weewx/correction.db"
        assert s.model_path == "/var/lib/weewx/correction_model.pkl"
        assert s.retrain_schedule == "daily"
        assert s.retrain_day == 3
        assert s.min_samples == 200
        assert s.retention_years == 5

    def test_manual_schedule_is_accepted(self) -> None:
        """retrain_schedule='manual' is a valid schedule value."""
        s = ForecastCorrectionSettings({"retrain_schedule": "manual"})
        assert s.retrain_schedule == "manual"
        s.validate()  # Must not raise


class TestForecastCorrectionSettingsBooleanParsing:
    """Boolean coercion: "true", "True", "1", "yes" → True; "false", "0", "no" → False."""

    @pytest.mark.parametrize("truthy_value", ["true", "True", "TRUE", "1", "yes", "YES"])
    def test_enabled_truthy_values_parse_as_true(self, truthy_value: str) -> None:
        """enabled recognises all documented truthy string representations."""
        s = ForecastCorrectionSettings({"enabled": truthy_value})
        assert s.enabled is True, (
            f"Expected enabled=True for input {truthy_value!r}, got {s.enabled}"
        )

    @pytest.mark.parametrize("falsy_value", ["false", "False", "FALSE", "0", "no", "NO", ""])
    def test_enabled_falsy_values_parse_as_false(self, falsy_value: str) -> None:
        """enabled treats any non-truthy string as False (safe default)."""
        s = ForecastCorrectionSettings({"enabled": falsy_value})
        assert s.enabled is False, (
            f"Expected enabled=False for input {falsy_value!r}, got {s.enabled}"
        )

    @pytest.mark.parametrize("truthy_value", ["true", "1", "yes"])
    def test_collection_enabled_truthy_values_parse_as_true(self, truthy_value: str) -> None:
        """collection_enabled truthy parsing matches the enabled field."""
        s = ForecastCorrectionSettings({"collection_enabled": truthy_value})
        assert s.collection_enabled is True

    @pytest.mark.parametrize("falsy_value", ["false", "0", "no"])
    def test_collection_enabled_falsy_values_parse_as_false(self, falsy_value: str) -> None:
        """collection_enabled falsy parsing matches the enabled field."""
        s = ForecastCorrectionSettings({"collection_enabled": falsy_value})
        assert s.collection_enabled is False


class TestForecastCorrectionSettingsValidation:
    def test_validate_rejects_min_samples_below_100(self) -> None:
        """min_samples below the documented minimum of 100 raises ValueError."""
        s = ForecastCorrectionSettings({"min_samples": "99"})
        with pytest.raises(ValueError, match="min_samples"):
            s.validate()

    def test_validate_accepts_min_samples_exactly_100(self) -> None:
        """min_samples == 100 is the boundary value and must pass."""
        s = ForecastCorrectionSettings({"min_samples": "100"})
        s.validate()  # Must not raise

    def test_validate_rejects_retention_years_below_1(self) -> None:
        """retention_years < 1 raises ValueError."""
        s = ForecastCorrectionSettings({"retention_years": "0"})
        with pytest.raises(ValueError, match="retention_years"):
            s.validate()

    def test_validate_accepts_retention_years_exactly_1(self) -> None:
        """retention_years == 1 is the minimum valid value."""
        s = ForecastCorrectionSettings({"retention_years": "1"})
        s.validate()  # Must not raise

    def test_validate_rejects_retrain_day_above_6(self) -> None:
        """retrain_day=7 is out of range (valid: 0-6) and raises ValueError."""
        s = ForecastCorrectionSettings({"retrain_day": "7"})
        with pytest.raises(ValueError, match="retrain_day"):
            s.validate()

    def test_validate_rejects_retrain_day_negative(self) -> None:
        """retrain_day=-1 is out of range and raises ValueError."""
        s = ForecastCorrectionSettings({"retrain_day": "-1"})
        with pytest.raises(ValueError, match="retrain_day"):
            s.validate()

    def test_validate_accepts_all_boundary_retrain_days(self) -> None:
        """retrain_day 0 (Monday) through 6 (Sunday) are all valid."""
        for day in range(7):
            s = ForecastCorrectionSettings({"retrain_day": str(day)})
            s.validate()  # Must not raise for any weekday

    def test_validate_accepts_all_valid_retrain_schedules(self) -> None:
        """'weekly', 'daily', and 'manual' all pass validate()."""
        for schedule in ("weekly", "daily", "manual"):
            s = ForecastCorrectionSettings({"retrain_schedule": schedule})
            s.validate()  # Must not raise


class TestForecastCorrectionSettingsInvalidScheduleDefault:
    def test_invalid_retrain_schedule_defaults_to_daily(self) -> None:
        """An unrecognised retrain_schedule string is silently normalised to 'daily'.

        This is the documented __init__ behaviour: raw_schedule is set to 'daily'
        when the provided value is not in ('weekly', 'daily', 'manual').  After
        normalisation, validate() passes because 'daily' is always valid.
        """
        s = ForecastCorrectionSettings({"retrain_schedule": "hourly"})
        assert s.retrain_schedule == "daily", (
            "Unrecognised schedule should default to 'daily', "
            f"got {s.retrain_schedule!r}"
        )
        s.validate()

    def test_mixed_case_schedule_normalised(self) -> None:
        """Schedule values are lowercased before matching; 'Weekly' should work."""
        s = ForecastCorrectionSettings({"retrain_schedule": "Weekly"})
        assert s.retrain_schedule == "weekly"

    def test_empty_schedule_defaults_to_daily(self) -> None:
        """An empty retrain_schedule string falls back to 'daily'."""
        s = ForecastCorrectionSettings({"retrain_schedule": ""})
        assert s.retrain_schedule == "daily"


class TestForecastCorrectionSettingsStandaloneUsage:
    def test_settings_object_attribute_access_pattern(self) -> None:
        """ForecastCorrectionSettings supports the attribute access pattern used in production.

        Verifies that the object behaves as a plain settings bag without requiring
        any parent Settings wrapper — the standalone use case is how the correction
        engine components access their config at runtime.
        """
        s = ForecastCorrectionSettings({
            "enabled": "false",
            "collection_enabled": "true",
            "min_samples": "500",
        })

        # Attribute access used by production code (correction/collector.py etc.)
        assert hasattr(s, "enabled")
        assert hasattr(s, "collection_enabled")
        assert hasattr(s, "db_path")
        assert hasattr(s, "model_path")
        assert hasattr(s, "retrain_schedule")
        assert hasattr(s, "retrain_day")
        assert hasattr(s, "min_samples")
        assert hasattr(s, "retention_years")

        assert s.enabled is False
        assert s.collection_enabled is True
        assert isinstance(s.db_path, str)
        assert isinstance(s.model_path, str)
        assert isinstance(s.min_samples, int)
        assert isinstance(s.retention_years, int)
