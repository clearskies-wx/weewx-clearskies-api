"""Unit tests for /records section assignment and self-hide logic.

Tests assert the lead-confirmed section mapping from the brief and the
self-hide rule: if a section's canonical fields are not in ColumnRegistry.mapped,
the section must be absent from the response (key omitted entirely, not empty []).

Uses SECTION_MAP from services/records.py plus a ColumnRegistry mock to
exercise get_records() directly (no DB for these pure logic tests).

The actual SQL queries are exercised by the integration tests in
test_endpoints_integration.py.

ADR references: ADR-035 (column registry), brief §3 per-endpoint spec.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Build a ColumnRegistry mock with a configurable set of mapped fields.
# ---------------------------------------------------------------------------


def _make_registry(mapped_fields: set[str]) -> MagicMock:
    """Build a ColumnRegistry mock where registry.stock.keys() == mapped_fields.

    The records service uses `set(registry.stock.keys())` to build the
    mapped_fields set internally.
    """
    registry = MagicMock()
    registry.stock = {f: MagicMock(canonical_name=f, is_stock=True) for f in mapped_fields}
    registry.unmapped = {}
    return registry


# ---------------------------------------------------------------------------
# Full-schema registry: all core observation fields present
# ---------------------------------------------------------------------------

FULL_OBSERVATION_FIELDS: set[str] = {
    "outTemp", "dewpoint", "windchill", "heatindex",
    "windSpeed", "windGust",
    "rain", "rainRate",
    "outHumidity",
    "barometer",
    "radiation", "UV",
}


class TestSectionMapConstantShape:
    """SECTION_MAP constant has the expected structure per the brief."""

    def test_section_map_has_all_expected_section_keys(self) -> None:
        """SECTION_MAP contains all 7 section keys per OpenAPI enum."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        expected_sections = {
            "temperature", "wind", "rain", "humidity",
            "barometer", "sun", "aqi",
        }
        assert set(SECTION_MAP.keys()) == expected_sections, (
            f"SECTION_MAP keys {set(SECTION_MAP.keys())} != expected {expected_sections}"
        )

    def test_temperature_section_has_high_and_low_out_temp_entries(self) -> None:
        """temperature section includes High temperature and Low temperature entries."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["temperature"]]
        assert "High temperature" in labels, (
            "temperature section must include 'High temperature' entry"
        )
        assert "Low temperature" in labels, (
            "temperature section must include 'Low temperature' entry"
        )

    def test_temperature_section_has_high_dewpoint_entry(self) -> None:
        """temperature section includes 'High dewpoint' entry."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["temperature"]]
        assert "High dewpoint" in labels

    def test_temperature_section_high_out_temp_uses_max_aggregator(self) -> None:
        """High temperature spec uses 'max' aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        high_specs = [s for s in SECTION_MAP["temperature"] if s.label == "High temperature"]
        assert high_specs, "High temperature spec must exist"
        assert high_specs[0].aggregator == "max"

    def test_wind_section_has_high_wind_speed_and_gust(self) -> None:
        """wind section includes 'High wind speed' and 'High wind gust'."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["wind"]]
        assert "High wind speed" in labels
        assert "High wind gust" in labels

    def test_rain_section_has_highest_rain_rate_entry(self) -> None:
        """rain section includes 'Highest rain rate' entry."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["rain"]]
        assert any("rain rate" in l.lower() for l in labels), (
            "rain section must include a rain rate entry"
        )

    def test_rain_section_includes_high_daily_rainfall(self) -> None:
        """rain section includes 'High daily rainfall' with sum-by-day aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["rain"]}
        assert "High daily rainfall" in specs
        assert "sum-by-day" in specs["High daily rainfall"].aggregator, (
            "High daily rainfall must use sum-by-day aggregator"
        )

    def test_humidity_section_uses_out_humidity_canonical_field(self) -> None:
        """humidity section entries reference outHumidity canonical field."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        fields = {s.canonicalField for s in SECTION_MAP["humidity"]}
        assert "outHumidity" in fields

    def test_sun_section_references_radiation_and_uv(self) -> None:
        """sun section entries reference radiation and UV canonical fields."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        fields = {s.canonicalField for s in SECTION_MAP["sun"]}
        assert "radiation" in fields
        assert "UV" in fields

    def test_aqi_section_is_empty_list(self) -> None:
        """aqi section is empty [] (self-hides this round, Phase 4)."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        assert SECTION_MAP["aqi"] == [], (
            "aqi section must be [] this round (Phase 4 per ADR-013)"
        )


class TestGetRecordsSectionInclusion:
    """get_records() includes sections when backing fields are present."""

    def _make_db_session(self, records: dict[str, tuple]) -> MagicMock:
        """Build a DB session mock that returns the given MAX/MIN values."""
        session = MagicMock()

        def mock_execute(sql, params=None):
            # Return (value, ts) for any query
            mock_result = MagicMock()
            mock_result.fetchone.return_value = (72.3, 1778099700)
            return mock_result

        session.execute.side_effect = mock_execute
        # Provide dialect info (needed by some aggregators)
        session.bind = MagicMock()
        session.bind.dialect = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def test_temperature_section_included_when_out_temp_in_registry(self) -> None:
        """get_records() returns 'temperature' section when outTemp is mapped."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session({})
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "temperature" in bundle.sections, (
            "temperature section must be included when outTemp is in mapped fields"
        )

    def test_wind_section_included_when_wind_speed_in_registry(self) -> None:
        """get_records() returns 'wind' section when windSpeed is mapped."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session({})
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "wind" in bundle.sections

    def test_rain_section_included_when_rain_in_registry(self) -> None:
        """get_records() returns 'rain' section when rain is mapped."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session({})
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "rain" in bundle.sections

    def test_sun_section_included_when_radiation_or_uv_in_registry(self) -> None:
        """get_records() returns 'sun' section when radiation OR UV is mapped."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session({})
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "sun" in bundle.sections


class TestGetRecordsSectionSelfHide:
    """get_records() omits sections when their backing fields are absent."""

    def _make_db_session(self) -> MagicMock:
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (72.3, 1778099700)
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def test_temperature_section_absent_when_no_temperature_fields_mapped(
        self,
    ) -> None:
        """temperature section omitted when all temperature fields are absent."""
        from weewx_clearskies_api.services.records import get_records

        # Remove all temperature-related fields
        fields = FULL_OBSERVATION_FIELDS - {
            "outTemp", "dewpoint", "windchill", "heatindex"
        }
        registry = _make_registry(fields)
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "temperature" not in bundle.sections, (
            "temperature section must be absent when no temperature fields are mapped"
        )

    def test_wind_section_absent_when_wind_fields_not_mapped(self) -> None:
        """wind section omitted when windSpeed and windGust both absent."""
        from weewx_clearskies_api.services.records import get_records

        fields = FULL_OBSERVATION_FIELDS - {"windSpeed", "windGust"}
        registry = _make_registry(fields)
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "wind" not in bundle.sections

    def test_sun_section_absent_when_radiation_and_uv_both_missing(self) -> None:
        """sun section omitted when BOTH radiation AND UV are absent."""
        from weewx_clearskies_api.services.records import get_records

        fields = FULL_OBSERVATION_FIELDS - {"radiation", "UV"}
        registry = _make_registry(fields)
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "sun" not in bundle.sections

    def test_sun_section_present_when_only_uv_mapped(self) -> None:
        """sun section present when UV is mapped (radiation not needed)."""
        from weewx_clearskies_api.services.records import get_records

        fields = (FULL_OBSERVATION_FIELDS - {"radiation"}) | {"UV"}
        registry = _make_registry(fields)
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "sun" in bundle.sections, (
            "sun section must appear when UV is mapped even if radiation is absent"
        )

    def test_aqi_section_always_absent(self) -> None:
        """aqi section is always absent this round (Phase 4 / ADR-013)."""
        from weewx_clearskies_api.services.records import get_records

        # Even if 'aqi' were a mapped field, the aqi section self-hides
        registry = _make_registry(FULL_OBSERVATION_FIELDS | {"aqi"})
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        assert "aqi" not in bundle.sections, (
            "aqi section must self-hide this round — it flows through Phase 4 UI"
        )

    def test_temperature_section_omits_dewpoint_entry_when_dewpoint_missing(
        self,
    ) -> None:
        """dewpoint entry absent from temperature section when dewpoint not mapped."""
        from weewx_clearskies_api.services.records import get_records

        # Keep outTemp but drop dewpoint
        fields = (FULL_OBSERVATION_FIELDS - {"dewpoint"}) | {"outTemp"}
        registry = _make_registry(fields)
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "temperature" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["temperature"]]
            assert "dewpoint" not in canonical_fields, (
                "dewpoint entry must be omitted from temperature section "
                "when dewpoint is not in ColumnRegistry.stock"
            )

    def test_section_filter_returns_only_requested_section(self) -> None:
        """section_filter='temperature' returns only the temperature section."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session()
        bundle = get_records(
            db, registry, period="all-time", section_filter="temperature"
        )

        assert "wind" not in bundle.sections, (
            "When section_filter='temperature', wind section must be absent"
        )
        assert "temperature" in bundle.sections

    def test_bundle_period_matches_requested_period(self) -> None:
        """Bundle period field reflects the requested period."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session()

        for period in ("ytd", "all-time", "2025"):
            bundle = get_records(db, registry, period=period, section_filter=None)
            assert bundle.period == period, (
                f"Bundle period must match requested period: expected {period!r}, "
                f"got {bundle.period!r}"
            )


class TestRecordEntryShape:
    """RecordEntry objects have the expected fields per OpenAPI schema."""

    def test_record_entry_has_required_fields(self) -> None:
        """RecordEntry has label, canonicalField, value, observedAt, brokenInLast30Days."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (72.3, 1778099700)
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"

        bundle = get_records(session, registry, period="all-time", section_filter="temperature")

        if "temperature" in bundle.sections and bundle.sections["temperature"]:
            entry = bundle.sections["temperature"][0]
            assert hasattr(entry, "label"), "RecordEntry must have 'label'"
            assert hasattr(entry, "canonicalField"), "RecordEntry must have 'canonicalField'"
            assert hasattr(entry, "brokenInLast30Days"), "RecordEntry must have 'brokenInLast30Days'"


class TestExpandedObservationSurfaceRecordsSectionIsolation:
    """The expanded 69-field Observation surface introduces fields not in any records section.

    Records sections are hand-curated in SECTION_MAP — they are NOT auto-derived
    from _FIRST_CLASS_FIELDS. This class verifies that newly-first-class fields
    that are NOT in any section mapping do NOT appear in section output.

    Catching a regression where someone accidentally auto-populates records sections
    from _FIRST_CLASS_FIELDS instead of from the lead-confirmed SECTION_MAP constant.
    """

    def _make_db_session_returning(self, value: float, epoch: int) -> MagicMock:
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (value, epoch)
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def test_extra_temp1_does_not_appear_in_temperature_section(self) -> None:
        """extraTemp1 is a first-class Observation field but NOT in temperature section records.

        SECTION_MAP.temperature hard-codes: outTemp, dewpoint, windchill, heatindex.
        extraTemp1 is a sensor-expansion slot that is NOT in the section mapping.
        A regression that auto-populates from _FIRST_CLASS_FIELDS would incorrectly
        add extraTemp1 to the temperature section.
        """
        from weewx_clearskies_api.services.records import get_records

        # Give the registry the expanded first-class surface including extraTemp1
        registry = _make_registry(FULL_OBSERVATION_FIELDS | {"extraTemp1"})
        db = self._make_db_session_returning(72.3, 1778099700)
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "temperature" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["temperature"]]
            assert "extraTemp1" not in canonical_fields, (
                "extraTemp1 must NOT appear in the temperature section records. "
                "Records sections are hand-curated (SECTION_MAP), not auto-populated "
                "from _FIRST_CLASS_FIELDS. Regression: auto-population from the "
                "expanded Observation surface would incorrectly add sensor-expansion "
                "fields to records sections."
            )

    def test_extra_temp2_not_in_temperature_section(self) -> None:
        """extraTemp2 is first-class Observation but NOT in temperature records section."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS | {"extraTemp2", "extraTemp3"})
        db = self._make_db_session_returning(72.3, 1778099700)
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "temperature" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["temperature"]]
            for field in ("extraTemp2", "extraTemp3"):
                assert field not in canonical_fields, (
                    f"{field} must NOT appear in temperature section records (sensor "
                    "expansion slot, not in SECTION_MAP)"
                )

    def test_soil_temp_fields_not_in_temperature_section(self) -> None:
        """soilTemp1..4 are first-class Observation fields but NOT in temperature records."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(
            FULL_OBSERVATION_FIELDS | {"soilTemp1", "soilTemp2", "soilTemp3", "soilTemp4"}
        )
        db = self._make_db_session_returning(72.3, 1778099700)
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "temperature" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["temperature"]]
            for field in ("soilTemp1", "soilTemp2", "soilTemp3", "soilTemp4"):
                assert field not in canonical_fields, (
                    f"{field} must NOT appear in temperature section records (soil sensor, "
                    "not in SECTION_MAP)"
                )

    def test_lightning_fields_not_in_wind_section(self) -> None:
        """lightning_strike_count is first-class but NOT in wind section records."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(
            FULL_OBSERVATION_FIELDS | {"lightning_strike_count", "lightning_distance"}
        )
        db = self._make_db_session_returning(72.3, 1778099700)
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "wind" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["wind"]]
            for field in ("lightning_strike_count", "lightning_distance"):
                assert field not in canonical_fields, (
                    f"{field} must NOT appear in wind section records"
                )

    def test_voltage_fields_not_in_any_standard_section(self) -> None:
        """Electrical telemetry fields (voltage, rxCheckPercent) are first-class but in no section."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(
            FULL_OBSERVATION_FIELDS | {
                "consBatteryVoltage", "heatingVoltage", "referenceVoltage",
                "supplyVoltage", "rxCheckPercent",
            }
        )
        db = self._make_db_session_returning(12.5, 1778099700)
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        telemetry_fields = {
            "consBatteryVoltage", "heatingVoltage", "referenceVoltage",
            "supplyVoltage", "rxCheckPercent",
        }
        # Check no standard section contains any of these fields
        standard_sections = {"temperature", "wind", "rain", "humidity", "barometer", "sun"}
        for section_name in standard_sections:
            if section_name in bundle.sections:
                canonical_fields = {e.canonicalField for e in bundle.sections[section_name]}
                overlap = canonical_fields & telemetry_fields
                assert not overlap, (
                    f"Electrical telemetry fields {overlap} must NOT appear in the "
                    f"{section_name!r} section — they are not in SECTION_MAP for any "
                    "standard section."
                )

# ---------------------------------------------------------------------------
# Tests for the 8 new record specs
# ---------------------------------------------------------------------------


FULL_WITH_OPTIONAL: set[str] = FULL_OBSERVATION_FIELDS | {"appTemp", "windrun"}


class TestNewSectionMapEntries:
    """SECTION_MAP has all 8 new record specs in the correct sections."""

    def test_temperature_section_has_high_apparent_temperature(self) -> None:
        """temperature section includes High apparent temperature."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["temperature"]]
        assert "High apparent temperature" in labels

    def test_temperature_section_has_low_apparent_temperature(self) -> None:
        """temperature section includes Low apparent temperature."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["temperature"]]
        assert "Low apparent temperature" in labels

    def test_apparent_temperature_specs_use_appTemp_canonical_field(self) -> None:
        """High/Low apparent temperature specs reference appTemp canonical field."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        app_specs = [s for s in SECTION_MAP["temperature"] if "apparent" in s.label.lower()]
        assert len(app_specs) == 2, "Expected exactly 2 apparent temperature specs"
        for spec in app_specs:
            assert spec.canonicalField == "appTemp", (
                f"Apparent temperature spec {spec.label!r} must use appTemp canonical field"
            )

    def test_high_apparent_temperature_uses_max_aggregator(self) -> None:
        """High apparent temperature spec uses max aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["temperature"]}
        assert specs["High apparent temperature"].aggregator == "max"

    def test_low_apparent_temperature_uses_min_aggregator(self) -> None:
        """Low apparent temperature spec uses min aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["temperature"]}
        assert specs["Low apparent temperature"].aggregator == "min"

    def test_temperature_section_has_largest_daily_range(self) -> None:
        """temperature section includes Largest daily temperature range."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["temperature"]]
        assert "Largest daily temperature range" in labels

    def test_temperature_section_has_smallest_daily_range(self) -> None:
        """temperature section includes Smallest daily temperature range."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["temperature"]]
        assert "Smallest daily temperature range" in labels

    def test_largest_daily_range_uses_max_daily_range_aggregator(self) -> None:
        """Largest daily temperature range spec uses max-daily-range aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["temperature"]}
        assert specs["Largest daily temperature range"].aggregator == "max-daily-range"

    def test_smallest_daily_range_uses_min_daily_range_aggregator(self) -> None:
        """Smallest daily temperature range spec uses min-daily-range aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["temperature"]}
        assert specs["Smallest daily temperature range"].aggregator == "min-daily-range"

    def test_daily_range_specs_reference_out_temp_canonical_field(self) -> None:
        """Daily temperature range specs reference outTemp canonical field."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        range_specs = [
            s for s in SECTION_MAP["temperature"]
            if "daily temperature range" in s.label.lower()
        ]
        assert len(range_specs) == 2, "Expected 2 daily temperature range specs"
        for spec in range_specs:
            assert spec.canonicalField == "outTemp", (
                f"Daily range spec {spec.label!r} must reference outTemp"
            )

    def test_wind_section_has_highest_daily_wind_run(self) -> None:
        """wind section includes Highest daily wind run."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["wind"]]
        assert "Highest daily wind run" in labels

    def test_highest_daily_wind_run_uses_sum_by_day_aggregator(self) -> None:
        """Highest daily wind run uses sum-by-day-then-max aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["wind"]}
        assert specs["Highest daily wind run"].aggregator == "sum-by-day-then-max"

    def test_highest_daily_wind_run_references_windrun_canonical_field(self) -> None:
        """Highest daily wind run references windrun canonical field."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["wind"]}
        assert specs["Highest daily wind run"].canonicalField == "windrun"

    def test_rain_section_has_highest_annual_rainfall(self) -> None:
        """rain section includes Highest annual rainfall."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["rain"]]
        assert "Highest annual rainfall" in labels

    def test_highest_annual_rainfall_uses_sum_by_year_aggregator(self) -> None:
        """Highest annual rainfall uses sum-by-year-then-max aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["rain"]}
        assert specs["Highest annual rainfall"].aggregator == "sum-by-year-then-max"

    def test_rain_section_has_consecutive_days_with_rain(self) -> None:
        """rain section includes Consecutive days with rain."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["rain"]]
        assert "Consecutive days with rain" in labels

    def test_rain_section_has_consecutive_days_without_rain(self) -> None:
        """rain section includes Consecutive days without rain."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        labels = [s.label for s in SECTION_MAP["rain"]]
        assert "Consecutive days without rain" in labels

    def test_consecutive_rain_days_uses_correct_aggregator(self) -> None:
        """Consecutive days with rain uses max-consecutive-rain-days aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["rain"]}
        assert specs["Consecutive days with rain"].aggregator == "max-consecutive-rain-days"

    def test_consecutive_dry_days_uses_correct_aggregator(self) -> None:
        """Consecutive days without rain uses max-consecutive-dry-days aggregator."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        specs = {s.label: s for s in SECTION_MAP["rain"]}
        assert specs["Consecutive days without rain"].aggregator == "max-consecutive-dry-days"


class TestCanonicalToDBEntries:
    """_CANONICAL_TO_DB has entries for the new fields."""

    def test_app_temp_in_canonical_to_db(self) -> None:
        """appTemp is present in _CANONICAL_TO_DB."""
        from weewx_clearskies_api.services.records import _CANONICAL_TO_DB  # noqa: PLC2701

        assert "appTemp" in _CANONICAL_TO_DB
        assert _CANONICAL_TO_DB["appTemp"] == "appTemp"

    def test_windrun_in_canonical_to_db(self) -> None:
        """windrun is present in _CANONICAL_TO_DB."""
        from weewx_clearskies_api.services.records import _CANONICAL_TO_DB  # noqa: PLC2701

        assert "windrun" in _CANONICAL_TO_DB
        assert _CANONICAL_TO_DB["windrun"] == "windrun"


class TestSelfHideNewFields:
    """New optional fields (appTemp, windrun) self-hide when absent from registry."""

    def _make_db_session(self) -> MagicMock:
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (72.3, 1778099700)
        mock_result.fetchall.return_value = [(5.0, 1778099700, "2025-07-01")]
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def test_apparent_temperature_entries_absent_when_apptemp_not_mapped(self) -> None:
        """appTemp entries omitted from temperature section when appTemp not in registry."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "temperature" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["temperature"]]
            app_temp_entries = [f for f in canonical_fields if f == "appTemp"]
            assert not app_temp_entries, (
                "appTemp entries must be absent from temperature section "
                "when appTemp is not in ColumnRegistry.stock"
            )

    def test_apparent_temperature_entries_present_when_apptemp_mapped(self) -> None:
        """appTemp entries appear in temperature section when appTemp is in registry."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS | {"appTemp"})
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "temperature" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["temperature"]]
            assert "appTemp" in canonical_fields, (
                "appTemp entries must appear in temperature section when appTemp is mapped"
            )

    def test_wind_run_entry_absent_when_windrun_not_mapped(self) -> None:
        """windrun entry omitted from wind section when windrun not in registry."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "wind" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["wind"]]
            assert "windrun" not in canonical_fields, (
                "windrun entry must be absent from wind section when windrun not in registry"
            )

    def test_wind_run_entry_present_when_windrun_mapped(self) -> None:
        """windrun entry appears in wind section when windrun is in registry."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS | {"windrun"})
        db = self._make_db_session()
        bundle = get_records(db, registry, period="all-time", section_filter=None)

        if "wind" in bundle.sections:
            canonical_fields = [e.canonicalField for e in bundle.sections["wind"]]
            assert "windrun" in canonical_fields, (
                "windrun entry must appear in wind section when windrun is mapped"
            )


class TestYearBucketSql:
    """_year_bucket_sql returns dialect-appropriate SQL expressions."""

    def test_sqlite_year_bucket_uses_strftime(self) -> None:
        """SQLite year bucket uses strftime with localtime modifier."""
        from weewx_clearskies_api.services.records import _year_bucket_sql  # noqa: PLC2701

        sql = _year_bucket_sql("sqlite")
        assert "strftime" in sql
        assert "%Y" in sql
        assert "localtime" in sql

    def test_mysql_year_bucket_uses_year_function(self) -> None:
        """MySQL year bucket uses YEAR() function."""
        from weewx_clearskies_api.services.records import _year_bucket_sql  # noqa: PLC2701

        sql = _year_bucket_sql("mysql")
        assert "YEAR" in sql
        assert "FROM_UNIXTIME" in sql


class TestDailyRangeExtreme:
    """_daily_range_extreme returns correct (range, epoch) for max and min direction."""

    def _make_session_with_row(self, range_val: float, ts: int) -> MagicMock:
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (range_val, ts)
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def _make_session_returning_none(self) -> MagicMock:
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def test_max_direction_returns_range_and_epoch(self) -> None:
        """_daily_range_extreme with direction=max returns range value and epoch."""
        from weewx_clearskies_api.services.records import _daily_range_extreme  # noqa: PLC2701

        session = self._make_session_with_row(12.5, 1778099700)
        val, ts = _daily_range_extreme(session, "outTemp", "max", "", {}, "sqlite")
        assert val == 12.5
        assert ts == 1778099700

    def test_min_direction_returns_range_and_epoch(self) -> None:
        """_daily_range_extreme with direction=min returns range value and epoch."""
        from weewx_clearskies_api.services.records import _daily_range_extreme  # noqa: PLC2701

        session = self._make_session_with_row(1.2, 1778099700)
        val, ts = _daily_range_extreme(session, "outTemp", "min", "", {}, "sqlite")
        assert val == 1.2
        assert ts == 1778099700

    def test_returns_none_none_when_no_data(self) -> None:
        """_daily_range_extreme returns (None, None) when table is empty."""
        from weewx_clearskies_api.services.records import _daily_range_extreme  # noqa: PLC2701

        session = self._make_session_returning_none()
        val, ts = _daily_range_extreme(session, "outTemp", "max", "", {}, "sqlite")
        assert val is None
        assert ts is None

    def test_min_direction_sql_has_having_clause(self) -> None:
        """_daily_range_extreme with direction=min issues a query with HAVING COUNT."""
        from weewx_clearskies_api.services.records import _daily_range_extreme  # noqa: PLC2701

        session = self._make_session_with_row(1.0, 1778099700)
        _daily_range_extreme(session, "outTemp", "min", "", {}, "sqlite")
        call_args = session.execute.call_args
        sql_text = str(call_args[0][0])
        assert "HAVING" in sql_text, (
            "min-daily-range query must include HAVING clause to exclude single-reading days"
        )

    def test_max_direction_sql_has_desc_order(self) -> None:
        """_daily_range_extreme with direction=max orders DESC."""
        from weewx_clearskies_api.services.records import _daily_range_extreme  # noqa: PLC2701

        session = self._make_session_with_row(12.5, 1778099700)
        _daily_range_extreme(session, "outTemp", "max", "", {}, "sqlite")
        call_args = session.execute.call_args
        sql_text = str(call_args[0][0])
        assert "DESC" in sql_text

    def test_min_direction_sql_has_asc_order(self) -> None:
        """_daily_range_extreme with direction=min orders ASC."""
        from weewx_clearskies_api.services.records import _daily_range_extreme  # noqa: PLC2701

        session = self._make_session_with_row(1.0, 1778099700)
        _daily_range_extreme(session, "outTemp", "min", "", {}, "sqlite")
        call_args = session.execute.call_args
        sql_text = str(call_args[0][0])
        assert "ASC" in sql_text


class TestConsecutiveRainDays:
    """_consecutive_rain_days calculates streak length and epoch correctly."""

    def _make_session_with_rows(self, rows: list) -> MagicMock:
        """rows: list of (day_rain, first_ts, bucket) tuples."""
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = rows
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def test_simple_rain_streak(self) -> None:
        """Three consecutive rainy days yields streak of 3."""
        from weewx_clearskies_api.services.records import _consecutive_rain_days  # noqa: PLC2701

        rows = [
            (0.0, 1700000000, "2023-11-14"),
            (2.5, 1700086400, "2023-11-15"),
            (1.0, 1700172800, "2023-11-16"),
            (3.2, 1700259200, "2023-11-17"),
            (0.0, 1700345600, "2023-11-18"),
        ]
        session = self._make_session_with_rows(rows)
        val, ts = _consecutive_rain_days(session, "rain", "", {}, "sqlite", with_rain=True)
        assert val == 3.0
        assert ts == 1700259200

    def test_simple_dry_streak(self) -> None:
        """Three consecutive dry days yields a dry streak of 3."""
        from weewx_clearskies_api.services.records import _consecutive_rain_days  # noqa: PLC2701

        rows = [
            (2.5, 1700000000, "2023-11-14"),
            (0.0, 1700086400, "2023-11-15"),
            (0.0, 1700172800, "2023-11-16"),
            (0.0, 1700259200, "2023-11-17"),
            (1.0, 1700345600, "2023-11-18"),
        ]
        session = self._make_session_with_rows(rows)
        val, ts = _consecutive_rain_days(session, "rain", "", {}, "sqlite", with_rain=False)
        assert val == 3.0
        assert ts == 1700259200

    def test_longest_streak_wins_when_multiple_streaks(self) -> None:
        """When there are two rain streaks, the longer one is returned."""
        from weewx_clearskies_api.services.records import _consecutive_rain_days  # noqa: PLC2701

        rows = [
            (1.0, 1700000000, "2023-11-14"),
            (1.0, 1700086400, "2023-11-15"),
            (0.0, 1700172800, "2023-11-16"),
            (1.0, 1700259200, "2023-11-17"),
            (1.0, 1700345600, "2023-11-18"),
            (1.0, 1700432000, "2023-11-19"),
            (1.0, 1700518400, "2023-11-20"),
        ]
        session = self._make_session_with_rows(rows)
        val, ts = _consecutive_rain_days(session, "rain", "", {}, "sqlite", with_rain=True)
        assert val == 4.0
        assert ts == 1700518400

    def test_returns_none_none_when_no_rows(self) -> None:
        """_consecutive_rain_days returns (None, None) when query returns no rows."""
        from weewx_clearskies_api.services.records import _consecutive_rain_days  # noqa: PLC2701

        session = self._make_session_with_rows([])
        val, ts = _consecutive_rain_days(session, "rain", "", {}, "sqlite", with_rain=True)
        assert val is None
        assert ts is None

    def test_returns_none_none_when_no_qualifying_days(self) -> None:
        """Returns (None, None) when no days qualify for the streak type."""
        from weewx_clearskies_api.services.records import _consecutive_rain_days  # noqa: PLC2701

        rows = [
            (0.0, 1700000000, "2023-11-14"),
            (0.0, 1700086400, "2023-11-15"),
        ]
        session = self._make_session_with_rows(rows)
        val, ts = _consecutive_rain_days(session, "rain", "", {}, "sqlite", with_rain=True)
        assert val is None
        assert ts is None

    def test_null_rain_counts_as_dry_day(self) -> None:
        """A day with NULL rain sum is treated as a dry day."""
        from weewx_clearskies_api.services.records import _consecutive_rain_days  # noqa: PLC2701

        rows = [
            (None, 1700000000, "2023-11-14"),
            (None, 1700086400, "2023-11-15"),
        ]
        session = self._make_session_with_rows(rows)
        val, ts = _consecutive_rain_days(session, "rain", "", {}, "sqlite", with_rain=False)
        assert val == 2.0

    def test_streak_value_is_float(self) -> None:
        """The streak count is returned as float (for RecordEntry compatibility)."""
        from weewx_clearskies_api.services.records import _consecutive_rain_days  # noqa: PLC2701

        rows = [(1.0, 1700000000, "2023-11-14")]
        session = self._make_session_with_rows(rows)
        val, _ = _consecutive_rain_days(session, "rain", "", {}, "sqlite", with_rain=True)
        assert val == 1.0
        assert isinstance(val, float)


class TestGetRecordsWithNewAggregators:
    """get_records() dispatches to new aggregators without error."""

    def _make_db_session_simple(self) -> MagicMock:
        """Session that returns a fetchone row and fetchall rows for all query types."""
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (10.0, 1778099700)
        mock_result.fetchall.return_value = [(2.5, 1778099700, "2025-07-01")]
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"
        return session

    def test_temperature_section_includes_apparent_temp_when_apptemp_mapped(self) -> None:
        """get_records() includes appTemp entries when appTemp is in registry."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS | {"appTemp"})
        db = self._make_db_session_simple()
        bundle = get_records(db, registry, period="all-time", section_filter="temperature")

        labels = [e.label for e in bundle.sections.get("temperature", [])]
        assert "High apparent temperature" in labels
        assert "Low apparent temperature" in labels

    def test_temperature_section_includes_daily_range_entries(self) -> None:
        """get_records() includes daily range entries for temperature."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session_simple()
        bundle = get_records(db, registry, period="all-time", section_filter="temperature")

        labels = [e.label for e in bundle.sections.get("temperature", [])]
        assert "Largest daily temperature range" in labels
        assert "Smallest daily temperature range" in labels

    def test_wind_section_includes_wind_run_when_windrun_mapped(self) -> None:
        """get_records() includes windrun entry when windrun is in registry."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS | {"windrun"})
        db = self._make_db_session_simple()
        bundle = get_records(db, registry, period="all-time", section_filter="wind")

        labels = [e.label for e in bundle.sections.get("wind", [])]
        assert "Highest daily wind run" in labels

    def test_rain_section_includes_annual_rainfall(self) -> None:
        """get_records() includes Highest annual rainfall in rain section."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session_simple()
        bundle = get_records(db, registry, period="all-time", section_filter="rain")

        labels = [e.label for e in bundle.sections.get("rain", [])]
        assert "Highest annual rainfall" in labels

    def test_rain_section_includes_consecutive_rain_days(self) -> None:
        """get_records() includes consecutive rain/dry day entries in rain section."""
        from weewx_clearskies_api.services.records import get_records

        registry = _make_registry(FULL_OBSERVATION_FIELDS)
        db = self._make_db_session_simple()
        bundle = get_records(db, registry, period="all-time", section_filter="rain")

        labels = [e.label for e in bundle.sections.get("rain", [])]
        assert "Consecutive days with rain" in labels
        assert "Consecutive days without rain" in labels

    def test_unknown_aggregator_produces_none_entry(self) -> None:
        """An unknown aggregator logs an error and produces a RecordEntry with None value."""
        from unittest.mock import patch

        from weewx_clearskies_api.services.records import (  # noqa: PLC2701
            SECTION_MAP,
            _RecordSpec,
            get_records,
        )

        bad_spec = _RecordSpec("Test bad aggregator", "outTemp", "high", "unknown-agg")
        patched = dict(SECTION_MAP)
        patched["temperature"] = [bad_spec]

        registry = _make_registry({"outTemp"})
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (72.3, 1778099700)
        mock_result.fetchall.return_value = []
        session.execute.return_value = mock_result
        session.bind = MagicMock()
        session.bind.dialect.name = "sqlite"

        with patch("weewx_clearskies_api.services.records.SECTION_MAP", patched):
            bundle = get_records(session, registry, period="all-time", section_filter="temperature")

        entries = bundle.sections.get("temperature", [])
        assert len(entries) == 1
        assert entries[0].value is None, (
            "Unknown aggregator must produce a RecordEntry with value=None"
        )
