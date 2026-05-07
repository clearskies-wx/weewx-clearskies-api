"""Unit tests for the services/units.py units-block helper.

Covers:
  - Per-target_unit system defaults for every canonical field in the
    canonical-data-model.md §2.1 table (US / METRIC / METRICWX).
  - Operator override application: METRIC base with group_pressure = hPa →
    barometer resolves to hPa; outTemp still °C.
  - Startup failure paths: missing weewx.conf → WeewxConfNotFoundError raised;
    missing [StdConvert] section → US defaults returned + warn logged.
  - The Python _SYSTEM_PRESETS constant in services/units.py matches
    canonical-data-model.md §2.1 for a representative sample of every group.

ADR references: ADR-019 (units handling), canonical-data-model.md §2.1.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers: build in-memory weewx.conf fixtures via configobj
# ---------------------------------------------------------------------------


def _write_weewx_conf(tmp_path: Path, content: str) -> Path:
    """Write a minimal weewx.conf to a temp file and return the path."""
    conf_file = tmp_path / "weewx.conf"
    conf_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return conf_file


def _reset_units_cache() -> None:
    """Reset the module-level cache between tests."""
    from weewx_clearskies_api.services.units import reset_cache

    reset_cache()


# ---------------------------------------------------------------------------
# §2.1 canonical unit-system reference table
#
# Derived verbatim from docs/contracts/canonical-data-model.md §2.1.
# The implementation must match every entry below.
# ---------------------------------------------------------------------------

# Format: { canonical_field: (US_unit, METRIC_unit, METRICWX_unit) }
# Covers the full 69-field first-class Observation surface per canonical-data-model §2.1.
# Source: docs/contracts/canonical-data-model.md §2.1 group→unit table.
CANONICAL_UNIT_TABLE: dict[str, tuple[str, str, str]] = {
    # group_temperature
    "outTemp":                    ("°F",      "°C",    "°C"),
    "dewpoint":                   ("°F",      "°C",    "°C"),
    "windchill":                  ("°F",      "°C",    "°C"),
    "heatindex":                  ("°F",      "°C",    "°C"),
    "inTemp":                     ("°F",      "°C",    "°C"),
    "appTemp":                    ("°F",      "°C",    "°C"),
    "THSW":                       ("°F",      "°C",    "°C"),
    "humidex":                    ("°F",      "°C",    "°C"),
    "extraTemp1":                 ("°F",      "°C",    "°C"),
    "extraTemp2":                 ("°F",      "°C",    "°C"),
    "extraTemp3":                 ("°F",      "°C",    "°C"),
    "soilTemp1":                  ("°F",      "°C",    "°C"),
    "soilTemp2":                  ("°F",      "°C",    "°C"),
    "soilTemp3":                  ("°F",      "°C",    "°C"),
    "soilTemp4":                  ("°F",      "°C",    "°C"),
    "leafTemp1":                  ("°F",      "°C",    "°C"),
    "leafTemp2":                  ("°F",      "°C",    "°C"),
    # group_speed
    "windSpeed":                  ("mph",     "km/h",  "m/s"),
    "windGust":                   ("mph",     "km/h",  "m/s"),
    "vecavg":                     ("mph",     "km/h",  "m/s"),
    "rms":                        ("mph",     "km/h",  "m/s"),
    # group_direction
    "windDir":                    ("°",       "°",     "°"),
    "windGustDir":                ("°",       "°",     "°"),
    "vecdir":                     ("°",       "°",     "°"),
    "gustdir":                    ("°",       "°",     "°"),
    # group_pressure
    "barometer":                  ("inHg",    "mbar",  "mbar"),
    "altimeter":                  ("inHg",    "mbar",  "mbar"),
    "pressure":                   ("inHg",    "mbar",  "mbar"),
    # group_rain (depth / accumulation)
    "rain":                       ("in",      "cm",    "mm"),
    "ET":                         ("in",      "cm",    "mm"),
    "hail":                       ("in",      "cm",    "mm"),
    "snow":                       ("in",      "cm",    "mm"),
    "snowDepth":                  ("in",      "cm",    "mm"),
    # group_rainrate
    "rainRate":                   ("in/h",    "cm/h",  "mm/h"),
    "hailRate":                   ("in/h",    "cm/h",  "mm/h"),
    "snowRate":                   ("in/h",    "cm/h",  "mm/h"),
    # group_radiation
    "radiation":                  ("W/m²",    "W/m²",  "W/m²"),
    "maxSolarRad":                ("W/m²",    "W/m²",  "W/m²"),
    # group_uv
    "UV":                         ("uv_index","uv_index","uv_index"),
    # group_percent
    "outHumidity":                ("%",       "%",     "%"),
    "inHumidity":                 ("%",       "%",     "%"),
    "cloudcover":                 ("%",       "%",     "%"),
    "pop":                        ("%",       "%",     "%"),
    "rxCheckPercent":             ("%",       "%",     "%"),
    "extraHumid1":                ("%",       "%",     "%"),
    "extraHumid2":                ("%",       "%",     "%"),
    # group_moisture
    "soilMoist1":                 ("cb",      "cb",    "cb"),
    "soilMoist2":                 ("cb",      "cb",    "cb"),
    "soilMoist3":                 ("cb",      "cb",    "cb"),
    "soilMoist4":                 ("cb",      "cb",    "cb"),
    # group_count
    "leafWet1":                   ("count",   "count", "count"),
    "leafWet2":                   ("count",   "count", "count"),
    "lightning_strike_count":     ("count",   "count", "count"),
    "lightning_noise_count":      ("count",   "count", "count"),
    "lightning_disturber_count":  ("count",   "count", "count"),
    # group_distance
    "windrun":                    ("mile",    "km",    "km"),
    "lightning_distance":         ("mile",    "km",    "km"),
    # group_altitude
    "cloudbase":                  ("foot",    "meter", "meter"),
    # group_volt
    "consBatteryVoltage":         ("V",       "V",     "V"),
    "heatingVoltage":             ("V",       "V",     "V"),
    "referenceVoltage":           ("V",       "V",     "V"),
    "supplyVoltage":              ("V",       "V",     "V"),
    # group_db
    "noise":                      ("dB",      "dB",    "dB"),
    # group_deltatime
    "sunshineDur":                ("s",       "s",     "s"),
    "daySunshineDur":             ("s",       "s",     "s"),
    "rainDur":                    ("s",       "s",     "s"),
    # group_degree_day
    "heatdeg":                    ("°F·day",  "°C·day","°C·day"),
    "cooldeg":                    ("°F·day",  "°C·day","°C·day"),
    # group_illuminance
    "illuminance":                ("lx",      "lx",    "lx"),
    # group_interval (meta — ArchiveRecord only, not Observation first-class observation field,
    # but present in _GROUP_MEMBERS so it resolves in the units block)
    "interval":                   ("minute",  "minute","minute"),
}


class TestUnitSystemDefaultsUs:
    """Every canonical field in §2.1 resolves to the correct US unit."""

    def test_all_observation_fields_resolve_to_us_units(self, tmp_path: Path) -> None:
        """US target_unit → every canonical observation field maps to its US unit."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path,
            """
            [StdConvert]
                target_unit = US
            """,
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)

        for field, (us_unit, _metric, _metricwx) in CANONICAL_UNIT_TABLE.items():
            assert units.get(field) == us_unit, (
                f"US: field {field!r} expected {us_unit!r}, got {units.get(field)!r}"
            )

    def test_out_temp_us_is_fahrenheit(self, tmp_path: Path) -> None:
        """Explicit spot-check: outTemp → °F in US mode."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = US\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["outTemp"] == "°F"

    def test_rain_us_is_inches(self, tmp_path: Path) -> None:
        """Explicit spot-check: rain → in in US mode."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = US\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["rain"] == "in"

    def test_wind_speed_us_is_mph(self, tmp_path: Path) -> None:
        """Explicit spot-check: windSpeed → mph in US mode."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = US\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["windSpeed"] == "mph"

    def test_barometer_us_is_inhg(self, tmp_path: Path) -> None:
        """Explicit spot-check: barometer → inHg in US mode."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = US\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["barometer"] == "inHg"


class TestUnitSystemDefaultsMetric:
    """Every canonical field resolves to the correct METRIC unit."""

    def test_all_observation_fields_resolve_to_metric_units(self, tmp_path: Path) -> None:
        """METRIC target_unit → every canonical observation field maps to its METRIC unit."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path, "[StdConvert]\n    target_unit = METRIC\n"
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)

        for field, (_us, metric_unit, _mx) in CANONICAL_UNIT_TABLE.items():
            assert units.get(field) == metric_unit, (
                f"METRIC: field {field!r} expected {metric_unit!r}, got {units.get(field)!r}"
            )

    def test_out_temp_metric_is_celsius(self, tmp_path: Path) -> None:
        """METRIC: outTemp → °C."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = METRIC\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["outTemp"] == "°C"

    def test_rain_metric_is_cm(self, tmp_path: Path) -> None:
        """METRIC: rain → cm."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = METRIC\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["rain"] == "cm"

    def test_pressure_metric_is_mbar(self, tmp_path: Path) -> None:
        """METRIC: barometer → mbar."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = METRIC\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["barometer"] == "mbar"

    def test_wind_speed_metric_is_kmh(self, tmp_path: Path) -> None:
        """METRIC: windSpeed → km/h."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(tmp_path, "[StdConvert]\n    target_unit = METRIC\n")
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["windSpeed"] == "km/h"


class TestUnitSystemDefaultsMetricWx:
    """Every canonical field resolves to the correct METRICWX unit."""

    def test_all_observation_fields_resolve_to_metricwx_units(
        self, tmp_path: Path
    ) -> None:
        """METRICWX target_unit → all canonical fields map to METRICWX units."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path, "[StdConvert]\n    target_unit = METRICWX\n"
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)

        for field, (_us, _metric, metricwx_unit) in CANONICAL_UNIT_TABLE.items():
            assert units.get(field) == metricwx_unit, (
                f"METRICWX: field {field!r} expected {metricwx_unit!r}, "
                f"got {units.get(field)!r}"
            )

    def test_wind_speed_metricwx_is_ms(self, tmp_path: Path) -> None:
        """METRICWX: windSpeed → m/s (METRICWX differs from METRIC here)."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path, "[StdConvert]\n    target_unit = METRICWX\n"
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["windSpeed"] == "m/s"

    def test_rain_metricwx_is_mm(self, tmp_path: Path) -> None:
        """METRICWX: rain → mm (differs from METRIC cm)."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path, "[StdConvert]\n    target_unit = METRICWX\n"
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        assert units["rain"] == "mm"


class TestOperatorOverrideApplication:
    """Operator-configured unit overrides replace the system default per ADR-019."""

    def test_metric_with_group_pressure_hpa_override_applies_to_barometer(
        self, tmp_path: Path
    ) -> None:
        """METRIC + group_pressure=hPa override → barometer resolves to hPa."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path,
            """
            [StdConvert]
                target_unit = METRIC
            [StdReport]
                [[Belchertown]]
                    [[[Units]]]
                        [[[[Groups]]]]
                            group_pressure = hPa
            """,
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)

        assert units["barometer"] == "hPa", (
            "Operator group_pressure=hPa override must override the METRIC default (mbar)"
        )

    def test_metric_base_unchanged_fields_use_system_defaults(
        self, tmp_path: Path
    ) -> None:
        """METRIC + group_pressure=hPa override: outTemp still °C (unchanged group)."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path,
            """
            [StdConvert]
                target_unit = METRIC
            [StdReport]
                [[Belchertown]]
                    [[[Units]]]
                        [[[[Groups]]]]
                            group_pressure = hPa
            """,
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)

        assert units["outTemp"] == "°C", (
            "Overriding group_pressure must not affect group_temperature"
        )

    def test_override_covers_all_fields_in_the_overridden_group(
        self, tmp_path: Path
    ) -> None:
        """METRIC + group_pressure=hPa → barometer, pressure, altimeter all → hPa."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path,
            """
            [StdConvert]
                target_unit = METRIC
            [StdReport]
                [[Seasons]]
                    [[[Units]]]
                        [[[[Groups]]]]
                            group_pressure = hPa
            """,
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)

        for pressure_field in ("barometer", "pressure", "altimeter"):
            assert units.get(pressure_field) == "hPa", (
                f"group_pressure override must apply to {pressure_field!r}"
            )

    def test_us_with_group_speed_knot_override(self, tmp_path: Path) -> None:
        """US + group_speed=knot override → windSpeed resolves to knot."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path,
            """
            [StdConvert]
                target_unit = US
            [StdReport]
                [[Seasons]]
                    [[[Units]]]
                        [[[[Groups]]]]
                            group_speed = knot
            """,
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)

        assert units["windSpeed"] == "knot", (
            "US + group_speed=knot override must resolve windSpeed to knot"
        )
        assert units["windGust"] == "knot", (
            "US + group_speed=knot override must resolve windGust to knot"
        )

    def test_unrecognized_override_unit_falls_back_to_system_default_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unrecognized override unit → system default used + WARN log emitted."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path,
            """
            [StdConvert]
                target_unit = METRIC
            [StdReport]
                [[Seasons]]
                    [[[Units]]]
                        [[[[Groups]]]]
                            group_pressure = bananas
            """,
        )
        from weewx_clearskies_api.services.units import load_units_block

        with caplog.at_level(logging.WARNING):
            units, _ = load_units_block(conf_path)

        # Should fall back to METRIC default (mbar)
        assert units["barometer"] == "mbar", (
            "Unrecognized override unit must fall back to system default"
        )
        # Should emit at least one WARNING
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_messages, (
            "Unrecognized override unit must emit at least one WARNING log"
        )


class TestUnitsServiceStartupFailures:
    """Startup failure modes per the brief's cross-cutting requirements."""

    def test_missing_weewx_conf_raises_weewx_conf_not_found_error(
        self, tmp_path: Path
    ) -> None:
        """weewx.conf path missing → raises WeewxConfNotFoundError at load time."""
        _reset_units_cache()
        missing_path = tmp_path / "no_such_file.conf"

        from weewx_clearskies_api.services.units import (
            WeewxConfNotFoundError,
            load_units_block,
        )

        with pytest.raises(WeewxConfNotFoundError):
            load_units_block(missing_path)

    def test_missing_weewx_conf_raises_subclass_of_file_not_found(
        self, tmp_path: Path
    ) -> None:
        """WeewxConfNotFoundError is a subclass of FileNotFoundError."""
        _reset_units_cache()
        missing_path = tmp_path / "no_such_file.conf"

        from weewx_clearskies_api.services.units import load_units_block

        with pytest.raises(FileNotFoundError):
            load_units_block(missing_path)

    def test_missing_stdconvert_section_returns_us_defaults_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """weewx.conf without [StdConvert] → US defaults returned + WARN logged."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path,
            """
            [Station]
                station_type = Simulator
            """,
        )
        from weewx_clearskies_api.services.units import load_units_block

        with caplog.at_level(logging.WARNING):
            units, target_unit = load_units_block(conf_path)

        # US defaults must be applied
        assert units["outTemp"] == "°F", (
            "Missing [StdConvert] must default to US units (outTemp → °F)"
        )
        assert units["rain"] == "in", (
            "Missing [StdConvert] must default to US units (rain → in)"
        )
        assert target_unit == "US"

        # At least one WARNING must be emitted
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, (
            "Missing [StdConvert] section must emit at least one WARNING log"
        )


class TestSystemPresetsConstant:
    """The _SYSTEM_PRESETS constant in services/units.py matches §2.1."""

    def test_system_presets_contains_us_system(self) -> None:
        """_SYSTEM_PRESETS has a 'US' key."""
        from weewx_clearskies_api.services import units as units_module

        presets = units_module._SYSTEM_PRESETS  # type: ignore[attr-defined]
        assert "US" in presets, "_SYSTEM_PRESETS must contain a 'US' entry"

    def test_system_presets_contains_metric_system(self) -> None:
        """_SYSTEM_PRESETS has a 'METRIC' key."""
        from weewx_clearskies_api.services import units as units_module

        presets = units_module._SYSTEM_PRESETS  # type: ignore[attr-defined]
        assert "METRIC" in presets

    def test_system_presets_contains_metricwx_system(self) -> None:
        """_SYSTEM_PRESETS has a 'METRICWX' key."""
        from weewx_clearskies_api.services import units as units_module

        presets = units_module._SYSTEM_PRESETS  # type: ignore[attr-defined]
        assert "METRICWX" in presets

    def test_us_group_temperature_entry_is_fahrenheit(self) -> None:
        """_SYSTEM_PRESETS['US']['group_temperature'] == '°F' per §2.1."""
        from weewx_clearskies_api.services import units as units_module

        presets = units_module._SYSTEM_PRESETS  # type: ignore[attr-defined]
        assert presets["US"]["group_temperature"] == "°F", (
            "_SYSTEM_PRESETS must match canonical-data-model.md §2.1: "
            "US/group_temperature should be °F"
        )

    def test_metric_group_speed_entry_is_kmh(self) -> None:
        """_SYSTEM_PRESETS['METRIC']['group_speed'] == 'km/h' per §2.1."""
        from weewx_clearskies_api.services import units as units_module

        presets = units_module._SYSTEM_PRESETS  # type: ignore[attr-defined]
        assert presets["METRIC"]["group_speed"] == "km/h"

    def test_metricwx_group_speed_differs_from_metric(self) -> None:
        """METRICWX group_speed is m/s; METRIC is km/h — they must differ."""
        from weewx_clearskies_api.services import units as units_module

        presets = units_module._SYSTEM_PRESETS  # type: ignore[attr-defined]
        assert presets["METRICWX"]["group_speed"] == "m/s"
        assert presets["METRIC"]["group_speed"] == "km/h"

    def test_representative_sample_matches_canonical_data_model_table(self) -> None:
        """Spot-check 10 group entries from §2.1 against the Python constant."""
        from weewx_clearskies_api.services import units as units_module

        presets = units_module._SYSTEM_PRESETS  # type: ignore[attr-defined]

        spot_checks = [
            # (system, group, expected_unit)
            ("US",       "group_temperature", "°F"),
            ("METRIC",   "group_temperature", "°C"),
            ("METRICWX", "group_temperature", "°C"),
            ("US",       "group_rain",        "in"),
            ("METRIC",   "group_rain",        "cm"),
            ("METRICWX", "group_rain",        "mm"),
            ("US",       "group_pressure",    "inHg"),
            ("METRIC",   "group_pressure",    "mbar"),
            ("METRICWX", "group_speed",       "m/s"),
            ("US",       "group_uv",          "uv_index"),
        ]
        for system, group, expected in spot_checks:
            actual = presets[system].get(group)
            assert actual == expected, (
                f"_SYSTEM_PRESETS[{system!r}][{group!r}] = {actual!r}, "
                f"expected {expected!r} per canonical-data-model.md §2.1"
            )


class TestExpandedObservationSurfaceUnits:
    """Every newly-first-class field in the 69-field Observation surface resolves correctly.

    Tests the expanded CANONICAL_UNIT_TABLE against all three target_unit systems.
    This class pins the contract change from 2026-05-06 (user directive: all stock
    weewx columns are first-class; extras is operator-custom-only).

    Each parametrized test is independent so a single field failure is immediately
    identifiable without hiding other failures.
    """

    def _load_units(self, tmp_path: Path, target_unit: str) -> dict:
        """Helper: write a minimal weewx.conf and load units block."""
        _reset_units_cache()
        conf_path = _write_weewx_conf(
            tmp_path, f"[StdConvert]\n    target_unit = {target_unit}\n"
        )
        from weewx_clearskies_api.services.units import load_units_block

        units, _ = load_units_block(conf_path)
        return units

    def test_all_expanded_fields_resolve_to_us_unit(self, tmp_path: Path) -> None:
        """Every entry in CANONICAL_UNIT_TABLE resolves to its US unit string."""
        units = self._load_units(tmp_path, "US")
        failures = []
        for field, (us_unit, _metric, _metricwx) in CANONICAL_UNIT_TABLE.items():
            if field == "interval":
                continue  # group_interval maps via ArchiveRecord, not Observation
            actual = units.get(field)
            if actual != us_unit:
                failures.append(f"{field!r}: expected {us_unit!r}, got {actual!r}")
        assert not failures, (
            "US unit resolution failed for:\n" + "\n".join(f"  {f}" for f in failures)
        )

    def test_all_expanded_fields_resolve_to_metric_unit(self, tmp_path: Path) -> None:
        """Every entry in CANONICAL_UNIT_TABLE resolves to its METRIC unit string."""
        units = self._load_units(tmp_path, "METRIC")
        failures = []
        for field, (_us, metric_unit, _metricwx) in CANONICAL_UNIT_TABLE.items():
            if field == "interval":
                continue
            actual = units.get(field)
            if actual != metric_unit:
                failures.append(f"{field!r}: expected {metric_unit!r}, got {actual!r}")
        assert not failures, (
            "METRIC unit resolution failed for:\n" + "\n".join(f"  {f}" for f in failures)
        )

    def test_all_expanded_fields_resolve_to_metricwx_unit(self, tmp_path: Path) -> None:
        """Every entry in CANONICAL_UNIT_TABLE resolves to its METRICWX unit string."""
        units = self._load_units(tmp_path, "METRICWX")
        failures = []
        for field, (_us, _metric, metricwx_unit) in CANONICAL_UNIT_TABLE.items():
            if field == "interval":
                continue
            actual = units.get(field)
            if actual != metricwx_unit:
                failures.append(f"{field!r}: expected {metricwx_unit!r}, got {actual!r}")
        assert not failures, (
            "METRICWX unit resolution failed for:\n" + "\n".join(f"  {f}" for f in failures)
        )

    def test_newly_first_class_fields_each_resolve_to_non_empty_unit_us(
        self, tmp_path: Path
    ) -> None:
        """Fields that were formerly 'promotion candidates' now resolve to a unit in US mode.

        These were previously absent from the units block (routed through extras).
        After the 2026-05-06 contract change they must appear in the units block.
        """
        units = self._load_units(tmp_path, "US")
        newly_first_class = [
            "appTemp", "cloudbase", "cloudcover", "windrun", "maxSolarRad",
            "sunshineDur", "daySunshineDur", "rainDur", "THSW", "humidex",
            "pop", "illuminance", "noise",
            "lightning_strike_count", "lightning_distance",
            "lightning_noise_count", "lightning_disturber_count",
            "snow", "snowDepth", "snowRate",
            "vecdir", "gustdir", "vecavg", "rms",
            "heatdeg", "cooldeg",
            "extraTemp1", "extraTemp2", "extraTemp3",
            "extraHumid1", "extraHumid2",
            "soilTemp1", "soilTemp2", "soilTemp3", "soilTemp4",
            "soilMoist1", "soilMoist2", "soilMoist3", "soilMoist4",
            "leafTemp1", "leafTemp2",
            "leafWet1", "leafWet2",
            "consBatteryVoltage", "heatingVoltage", "referenceVoltage",
            "supplyVoltage", "rxCheckPercent",
        ]
        missing_from_units_block = [f for f in newly_first_class if not units.get(f)]
        assert not missing_from_units_block, (
            "These newly-first-class fields must have a non-empty unit in the US units block "
            "(they were formerly promotion candidates routed through extras, now they are "
            "first-class per the 2026-05-06 contract change):\n"
            + "\n".join(f"  {f}" for f in missing_from_units_block)
        )

    def test_degree_day_fields_us_unit_is_fahrenheit_day(
        self, tmp_path: Path
    ) -> None:
        """heatdeg + cooldeg → °F·day in US mode per §2.1 group_degree_day."""
        units = self._load_units(tmp_path, "US")
        assert units.get("heatdeg") == "°F·day", (
            f"heatdeg US unit must be '°F·day', got {units.get('heatdeg')!r}"
        )
        assert units.get("cooldeg") == "°F·day", (
            f"cooldeg US unit must be '°F·day', got {units.get('cooldeg')!r}"
        )

    def test_degree_day_fields_metric_unit_is_celsius_day(
        self, tmp_path: Path
    ) -> None:
        """heatdeg + cooldeg → °C·day in METRIC mode per §2.1."""
        units = self._load_units(tmp_path, "METRIC")
        assert units.get("heatdeg") == "°C·day"
        assert units.get("cooldeg") == "°C·day"

    def test_soil_moisture_fields_unit_is_cb_all_systems(
        self, tmp_path: Path
    ) -> None:
        """soilMoist1..4 → cb (centibar) in all unit systems (unit-invariant)."""
        for system in ("US", "METRIC", "METRICWX"):
            _reset_units_cache()
            units = self._load_units(tmp_path, system)
            for field in ("soilMoist1", "soilMoist2", "soilMoist3", "soilMoist4"):
                assert units.get(field) == "cb", (
                    f"{system}: {field} must be 'cb', got {units.get(field)!r}"
                )

    def test_lightning_count_fields_unit_is_count_all_systems(
        self, tmp_path: Path
    ) -> None:
        """lightning_*_count fields → 'count' in all unit systems (unit-invariant)."""
        for system in ("US", "METRIC", "METRICWX"):
            _reset_units_cache()
            units = self._load_units(tmp_path, system)
            for field in (
                "lightning_strike_count", "lightning_noise_count",
                "lightning_disturber_count",
            ):
                assert units.get(field) == "count", (
                    f"{system}: {field} must be 'count', got {units.get(field)!r}"
                )

    def test_windrun_and_lightning_distance_differ_by_system(
        self, tmp_path: Path
    ) -> None:
        """windrun + lightning_distance → 'mile' US, 'km' METRIC/METRICWX (group_distance)."""
        for field in ("windrun", "lightning_distance"):
            _reset_units_cache()
            us_units = self._load_units(tmp_path, "US")
            _reset_units_cache()
            metric_units = self._load_units(tmp_path, "METRIC")

            assert us_units.get(field) == "mile", (
                f"US: {field} must be 'mile', got {us_units.get(field)!r}"
            )
            assert metric_units.get(field) == "km", (
                f"METRIC: {field} must be 'km', got {metric_units.get(field)!r}"
            )
