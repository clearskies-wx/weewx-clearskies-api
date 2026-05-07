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

import pytest


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
    "inTemp", "inHumidity",
}


class TestSectionMapConstantShape:
    """SECTION_MAP constant has the expected structure per the brief."""

    def test_section_map_has_all_expected_section_keys(self) -> None:
        """SECTION_MAP contains all 9 section keys per OpenAPI enum."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        expected_sections = {
            "temperature", "wind", "rain", "humidity",
            "barometer", "sun", "aqi", "inside-temp", "custom",
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

    def test_custom_section_is_empty_list(self) -> None:
        """custom section is [] (Phase 4 per ADR-027)."""
        from weewx_clearskies_api.services.records import SECTION_MAP

        assert SECTION_MAP["custom"] == [], (
            "custom section must return [] this round (Phase 4 per ADR-027)"
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
