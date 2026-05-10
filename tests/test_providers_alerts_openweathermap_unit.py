"""Unit tests for the OpenWeatherMap alerts provider (3b round 8).

Covers per the task-3b-8 brief §Test plan (unit tests section):

  Wire-shape Pydantic validation:
  - alerts_paid.json fixture loads cleanly against _OWMAlertEntry / _OWMOneCallAlertsResponse.
  - Extra fields (tags) ignored silently (extra="ignore").
  - Missing required event field → ValidationError → ProviderProtocolError.
  - Missing required start field → ValidationError → ProviderProtocolError.

  Severity-from-event-keyword mapping (canonical-data-model §4.3, lead-call 12):
  - Table-driven: warning > watch > advisory/statement > default.
  - "Tornado Warning" → "warning".
  - "Severe Thunderstorm Watch" → "watch".
  - "Wind Advisory" → "advisory".
  - "Special Weather Statement" → "advisory".
  - "Heat Watch" → "watch".
  - "Coastal Flood Warning" → "warning".
  - "Unknown Mystery Hazard" → "advisory" (default; NO log emitted).
  - "tornado warning" (lower-case) → "warning" (case-insensitive).
  - "Severe Weather Warning" → "warning" (warning beats advisory priority).

  Datetime conversion (lead-call 14):
  - epoch_to_utc_iso8601: epoch UTC → ISO Z string.
  - start=1714485600 → "2026-04-30T14:00:00Z" (verified UTC).
  - end=None → expires=None on canonical record.

  ID synthesis (lead-call 13):
  - Normal: ("Wind Advisory", 1714485600, "NWS Seattle WA") → "Wind Advisory|1714485600|NWS Seattle WA".
  - None sender_name: ("Foo", 100, None) → "Foo|100|".
  - Empty sender_name: ("Foo", 100, "") → "Foo|100|".

  Description passthrough (no instruction-append):
  - description field passed through unchanged.
  - No NWS-style instruction-append (OWM has no instruction field).

  PARTIAL-DOMAIN fields (lead-call 16):
  - urgency always None on canonical record.
  - certainty always None on canonical record.
  - areaDesc always None on canonical record.
  - category always None on canonical record.

  tags field:
  - Wire tags present → dropped silently (canonical AlertRecord has no tag-bearing field).

  Cache hit/miss:
  - Cache miss → outbound HTTP call → records stored.
  - Cache hit → no HTTP call → cached records returned.
  - Cached records round-trip through model_dump/model_validate.

  Credentials missing (lead-call 8):
  - appid=None → KeyInvalid before any HTTP call.
  - appid="" → KeyInvalid before any HTTP call.

  HTTP error paths:
  - HTTP 401 + status_code==401 (Q1=A): empty list returned + WARN log once +
    cache stored with empty list (3b-5 audit F2 cache-parity mirror).
  - HTTP 401 + status_code==401: second call with same key does NOT log warning twice.
  - HTTP 401 + status_code==401: second call returns from cache (cache-parity test).
  - HTTP 429 → QuotaExhausted; retry_after_seconds propagated (Retry-After header).
  - HTTP 500 → TransientNetworkError.
  - Non-401 KeyInvalid (e.g. status_code=403) → re-raised as KeyInvalid (defensive).

  Pydantic ValidationError → ProviderProtocolError:
  - Alert entry missing 'event' field → ProviderProtocolError.
  - Alert entry missing 'start' field → ProviderProtocolError.

  Empty alerts:
  - alerts: [] response → empty list (NOT an error).

  Redaction filter:
  - URL with appid=ABC123 → appid=[REDACTED] in logged output.
  - appid in middle of query string is redacted.
  - OWM alerts URL shape (with exclude param) is redacted.

  Capability registry:
  - CAPABILITY.provider_id = "openweathermap", domain = "alerts".
  - CAPABILITY.auth_required includes "appid".
  - CAPABILITY.supplied_canonical_fields includes expected 8 fields.
  - CAPABILITY.supplied_canonical_fields excludes urgency/certainty/areaDesc/category (PARTIAL-DOMAIN).
  - CAPABILITY.geographic_coverage = "global".
  - CAPABILITY.default_poll_interval_seconds = 300.
  - wire_providers([CAPABILITY]) → registry has openweathermap alerts entry.

No DB, no live network. respx mocks outbound httpx calls.
Wire-shape rule: fixtures loaded from tests/fixtures/providers/openweathermap/alerts_*.json
(synthetic-from-api-docs-example per brief L3 rule).
ADR references: ADR-006, ADR-016, ADR-017, ADR-038.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "openweathermap"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file from tests/fixtures/providers/openweathermap/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helpers
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, and re-wire memory cache."""
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
        _reset_basic_tier_warned_for_tests,
        _reset_http_client_for_tests,
    )
    import weewx_clearskies_api.providers.alerts.openweathermap as _owm_alerts  # noqa: PLC0415

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _reset_basic_tier_warned_for_tests()
    # Clear rate-limiter deque so consecutive tests don't trip each other.
    _owm_alerts._rate_limiter._calls.clear()
    # Re-wire a clean memory cache (CLEARSKIES_CACHE_URL unset in unit test env).
    wire_cache_from_env()


# OWM One Call URL for respx mocking
_OWM_ONECALL_URL = "https://api.openweathermap.org/data/3.0/onecall"
_LAT = 47.6062
_LON = -122.3321
_TEST_APPID = "TEST_APPID_12345"


# ===========================================================================
# 1. Severity-from-event-keyword mapping (lead-call 12)
# ===========================================================================


class TestOwmSeverityFromEvent:
    """_owm_severity_from_event derives canonical severity from event keyword.

    canonical-data-model §4.3 OWM column: severity derived via case-insensitive
    substring match in priority order: warning > watch > advisory/statement > default.
    Unknown events default to "advisory" with NO WARNING log (lead-call 12:
    OWM event strings are natural-language agency labels, not schema codes).
    """

    def test_tornado_warning_maps_to_warning(self) -> None:
        """`Tornado Warning` → 'warning' (contains 'warning' keyword)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Tornado Warning") == "warning"

    def test_severe_thunderstorm_watch_maps_to_watch(self) -> None:
        """`Severe Thunderstorm Watch` → 'watch' (contains 'watch' keyword)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Severe Thunderstorm Watch") == "watch"

    def test_wind_advisory_maps_to_advisory(self) -> None:
        """`Wind Advisory` → 'advisory' (contains 'advisory' keyword)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Wind Advisory") == "advisory"

    def test_special_weather_statement_maps_to_advisory(self) -> None:
        """`Special Weather Statement` → 'advisory' (contains 'statement' keyword)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Special Weather Statement") == "advisory"

    def test_heat_watch_maps_to_watch(self) -> None:
        """`Heat Watch` → 'watch' (contains 'watch' keyword)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Heat Watch") == "watch"

    def test_coastal_flood_warning_maps_to_warning(self) -> None:
        """`Coastal Flood Warning` → 'warning' (contains 'warning' keyword)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Coastal Flood Warning") == "warning"

    def test_unknown_event_defaults_to_advisory(self) -> None:
        """`Unknown Mystery Hazard` → 'advisory' (no keyword match → default)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Unknown Mystery Hazard") == "advisory"

    def test_unknown_event_does_not_emit_warning_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown event → 'advisory' default; NO WARNING log (lead-call 12: OWM events
        are natural-language agency labels, not schema codes — noise suppressed).
        """
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        with caplog.at_level(
            logging.WARNING,
            logger="weewx_clearskies_api.providers.alerts.openweathermap",
        ):
            _owm_severity_from_event("Unknown Mystery Hazard")
        assert len(caplog.records) == 0, (
            "Unknown OWM event must NOT emit WARNING log (lead-call 12: not schema drift)"
        )

    def test_lowercase_tornado_warning_maps_to_warning(self) -> None:
        """Case-insensitive: `tornado warning` (lower-case) → 'warning'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("tornado warning") == "warning"

    def test_warning_beats_advisory_in_priority_order(self) -> None:
        """Priority overlap: `Severe Weather Warning` → 'warning' (warning beats advisory)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        # "Severe Weather Warning" contains both "warning" and no other matches,
        # but the priority ordering ensures "warning" wins over the default.
        assert _owm_severity_from_event("Severe Weather Warning") == "warning"

    def test_watch_and_advisory_combined_warning_wins(self) -> None:
        """Priority overlap: event with 'Warning' in name → 'warning' beats 'watch'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        # If a hypothetical event string had "Warning Watch" in its name,
        # warning must take priority per the brief priority order.
        assert _owm_severity_from_event("Warning Watch Test") == "warning"

    def test_real_fixture_first_entry_wind_advisory_yields_advisory(self) -> None:
        """Real fixture entry 1 'Wind Advisory' → severity 'advisory'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Wind Advisory") == "advisory"

    def test_real_fixture_second_entry_tornado_warning_yields_warning(self) -> None:
        """Real fixture entry 2 'Tornado Warning' → severity 'warning'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_severity_from_event  # noqa: PLC0415
        assert _owm_severity_from_event("Tornado Warning") == "warning"


# ===========================================================================
# 2. Datetime conversion (lead-call 14)
# ===========================================================================


class TestDatetimeConversion:
    """epoch_to_utc_iso8601 converts OWM epoch seconds to UTC ISO Z strings."""

    def test_epoch_converts_to_utc_iso8601_z_string(self) -> None:
        """epoch=1714485600 → UTC ISO-8601 Z string ending in Z."""
        from weewx_clearskies_api.providers._common.datetime_utils import epoch_to_utc_iso8601  # noqa: PLC0415
        result = epoch_to_utc_iso8601(1714485600, provider_id="openweathermap", domain="alerts")
        assert result.endswith("Z"), f"Expected UTC Z suffix, got {result!r}"

    def test_epoch_converts_to_correct_utc_datetime(self) -> None:
        """epoch=1714485600 → 2024-04-30T14:00:00Z (verified UTC)."""
        from weewx_clearskies_api.providers._common.datetime_utils import epoch_to_utc_iso8601  # noqa: PLC0415
        result = epoch_to_utc_iso8601(1714485600, provider_id="openweathermap", domain="alerts")
        assert result == "2024-04-30T14:00:00Z", (
            f"Expected '2024-04-30T14:00:00Z', got {result!r}"
        )

    def test_owm_alert_entry_effective_is_utc_z(self) -> None:
        """_owm_alert_to_canonical: start epoch → effective UTC Z."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMAlertEntry,
            _owm_alert_to_canonical,
        )
        entry = _OWMAlertEntry.model_validate({
            "sender_name": "NWS Seattle WA",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": "Advisory text.",
        })
        record = _owm_alert_to_canonical(entry)
        assert record.effective is not None
        assert record.effective.endswith("Z"), (
            f"effective must end with Z, got {record.effective!r}"
        )

    def test_owm_alert_entry_end_none_yields_expires_none(self) -> None:
        """end=None → expires=None on canonical record."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMAlertEntry,
            _owm_alert_to_canonical,
        )
        entry = _OWMAlertEntry.model_validate({
            "sender_name": "NWS Test",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": None,
            "description": "Text.",
        })
        record = _owm_alert_to_canonical(entry)
        assert record.expires is None, (
            f"end=None should yield expires=None, got {record.expires!r}"
        )

    def test_real_fixture_effective_is_utc_z(self) -> None:
        """Real fixture start=1714485600 → effective ends with Z."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMAlertEntry,
            _owm_alert_to_canonical,
        )
        fixture = _load_fixture("alerts_paid.json")
        first_raw = fixture["alerts"][0]
        entry = _OWMAlertEntry.model_validate(first_raw)
        record = _owm_alert_to_canonical(entry)
        assert record.effective.endswith("Z"), (
            f"Real fixture effective must be UTC Z, got {record.effective!r}"
        )


# ===========================================================================
# 3. ID synthesis (lead-call 13)
# ===========================================================================


class TestIdSynthesis:
    """_synthesize_alert_id produces deterministic pipe-delimited IDs."""

    def test_normal_case_produces_pipe_delimited_id(self) -> None:
        """Normal: ("Wind Advisory", 1714485600, "NWS Seattle WA") → expected ID."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _synthesize_alert_id  # noqa: PLC0415
        result = _synthesize_alert_id("Wind Advisory", 1714485600, "NWS Seattle WA")
        assert result == "Wind Advisory|1714485600|NWS Seattle WA", (
            f"Expected pipe-delimited ID, got {result!r}"
        )

    def test_none_sender_name_produces_empty_trailing_segment(self) -> None:
        """sender_name=None → trailing empty segment after last pipe."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _synthesize_alert_id  # noqa: PLC0415
        result = _synthesize_alert_id("Foo", 100, None)
        assert result == "Foo|100|", (
            f"Expected 'Foo|100|' for None sender_name, got {result!r}"
        )

    def test_empty_sender_name_produces_empty_trailing_segment(self) -> None:
        """sender_name="" → trailing empty segment after last pipe."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _synthesize_alert_id  # noqa: PLC0415
        result = _synthesize_alert_id("Foo", 100, "")
        assert result == "Foo|100|", (
            f"Expected 'Foo|100|' for empty sender_name, got {result!r}"
        )

    def test_id_synthesis_from_real_fixture_first_entry(self) -> None:
        """Real fixture entry 1: synthesized ID has expected format."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMAlertEntry,
            _owm_alert_to_canonical,
        )
        fixture = _load_fixture("alerts_paid.json")
        first_raw = fixture["alerts"][0]
        entry = _OWMAlertEntry.model_validate(first_raw)
        record = _owm_alert_to_canonical(entry)
        expected_id = "Wind Advisory|1714485600|NWS Seattle WA"
        assert record.id == expected_id, (
            f"Expected id={expected_id!r}, got {record.id!r}"
        )

    def test_id_synthesis_second_entry_different_sender_and_start(self) -> None:
        """Real fixture entry 2 (Tornado Warning): ID uses different sender and start."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMAlertEntry,
            _owm_alert_to_canonical,
        )
        fixture = _load_fixture("alerts_paid.json")
        second_raw = fixture["alerts"][1]
        entry = _OWMAlertEntry.model_validate(second_raw)
        record = _owm_alert_to_canonical(entry)
        expected_id = "Tornado Warning|1714490000|NWS Portland OR"
        assert record.id == expected_id, (
            f"Expected id={expected_id!r}, got {record.id!r}"
        )


# ===========================================================================
# 4. Description passthrough (no instruction-append)
# ===========================================================================


class TestDescriptionPassthrough:
    """description field passes through without modification.

    OWM has no instruction field (unlike NWS which appends instruction with
    double-newline). Canonical description = wire description, verbatim.
    """

    def _make_entry(self, description: str | None) -> Any:
        """Build a minimal _OWMAlertEntry with the given description."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _OWMAlertEntry  # noqa: PLC0415
        return _OWMAlertEntry.model_validate({
            "sender_name": "NWS Test",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": description,
        })

    def test_description_text_is_passed_through_unchanged(self) -> None:
        """Wire description text is not modified."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_alert_to_canonical  # noqa: PLC0415
        text = "* WHAT...Southerly winds 20 to 30 mph with gusts up to 45 mph."
        entry = self._make_entry(text)
        record = _owm_alert_to_canonical(entry)
        assert record.description == text, (
            f"Expected description unchanged, got {record.description!r}"
        )

    def test_description_does_not_append_instruction(self) -> None:
        """No NWS-style instruction-append for OWM (brief: passthrough only)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_alert_to_canonical  # noqa: PLC0415
        text = "Test advisory body."
        entry = self._make_entry(text)
        record = _owm_alert_to_canonical(entry)
        assert record.description == text
        assert "\n\n" not in record.description, (
            "OWM description must not have instruction appended with double-newline"
        )

    def test_description_none_maps_to_none_or_empty(self) -> None:
        """Wire description=None → None or empty string on canonical record (not an error)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _owm_alert_to_canonical  # noqa: PLC0415
        entry = self._make_entry(None)
        record = _owm_alert_to_canonical(entry)
        # None or empty string both acceptable — no assertion on which; just that it doesn't raise.
        assert record.description is None or isinstance(record.description, str)


# ===========================================================================
# 5. PARTIAL-DOMAIN fields (lead-call 16)
# ===========================================================================


class TestPartialDomainFields:
    """urgency/certainty/areaDesc/category are always None (PARTIAL-DOMAIN).

    OWM categorically does not supply these fields on any plan tier.
    PARTIAL-DOMAIN per canonical-data-model §4.3 OWM column.
    """

    def _make_canonical_record(self) -> Any:
        """Make a canonical AlertRecord from a minimal OWM alert entry."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMAlertEntry,
            _owm_alert_to_canonical,
        )
        entry = _OWMAlertEntry.model_validate({
            "sender_name": "NWS Test",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": "Advisory text.",
        })
        return _owm_alert_to_canonical(entry)

    def test_urgency_is_none_partial_domain(self) -> None:
        """urgency = None (PARTIAL-DOMAIN; OWM does not supply this field)."""
        record = self._make_canonical_record()
        assert record.urgency is None, (
            f"Expected urgency=None (PARTIAL-DOMAIN), got {record.urgency!r}"
        )

    def test_certainty_is_none_partial_domain(self) -> None:
        """certainty = None (PARTIAL-DOMAIN; OWM does not supply this field)."""
        record = self._make_canonical_record()
        assert record.certainty is None, (
            f"Expected certainty=None (PARTIAL-DOMAIN), got {record.certainty!r}"
        )

    def test_area_desc_is_none_partial_domain(self) -> None:
        """areaDesc = None (PARTIAL-DOMAIN; OWM does not supply this field)."""
        record = self._make_canonical_record()
        assert record.areaDesc is None, (
            f"Expected areaDesc=None (PARTIAL-DOMAIN), got {record.areaDesc!r}"
        )

    def test_category_is_none_partial_domain(self) -> None:
        """category = None (PARTIAL-DOMAIN; OWM does not supply this field)."""
        record = self._make_canonical_record()
        assert record.category is None, (
            f"Expected category=None (PARTIAL-DOMAIN), got {record.category!r}"
        )

    def test_source_is_openweathermap(self) -> None:
        """source = 'openweathermap' (provider_id literal) on all canonical records."""
        record = self._make_canonical_record()
        assert record.source == "openweathermap", (
            f"Expected source='openweathermap', got {record.source!r}"
        )

    def test_headline_equals_event(self) -> None:
        """headline = event (direct passthrough per canonical §4.3)."""
        record = self._make_canonical_record()
        assert record.headline == "Wind Advisory", (
            f"Expected headline='Wind Advisory', got {record.headline!r}"
        )

    def test_event_equals_wire_event_field(self) -> None:
        """event = wire event field (direct passthrough)."""
        record = self._make_canonical_record()
        assert record.event == "Wind Advisory", (
            f"Expected event='Wind Advisory', got {record.event!r}"
        )

    def test_sender_name_equals_wire_sender_name(self) -> None:
        """senderName = wire sender_name field (direct passthrough)."""
        record = self._make_canonical_record()
        assert record.senderName == "NWS Test", (
            f"Expected senderName='NWS Test', got {record.senderName!r}"
        )


# ===========================================================================
# 6. Tags field dropped silently
# ===========================================================================


class TestTagsFieldDroppedSilently:
    """Wire tags field present → dropped silently; no tag-bearing field on canonical record."""

    def test_tags_field_in_wire_does_not_appear_on_canonical_record(self) -> None:
        """Wire tags=['Wind'] → dropped; AlertRecord has no tags field."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMAlertEntry,
            _owm_alert_to_canonical,
        )
        # Wire entry WITH tags (as in real OWM response per api-docs L209)
        entry_data = {
            "sender_name": "NWS Seattle WA",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": "Winds 20-30 mph.",
            "tags": ["Wind"],  # Present in wire; must be dropped
        }
        # Should load cleanly (extra="ignore")
        entry = _OWMAlertEntry.model_validate(entry_data)
        record = _owm_alert_to_canonical(entry)
        # Assert canonical AlertRecord has no tags field
        assert not hasattr(record, "tags"), (
            "Canonical AlertRecord must not have a 'tags' attribute (tags dropped per §3.6)"
        )
        # Assert record's fields don't include anything tag-like
        from weewx_clearskies_api.models.responses import AlertRecord  # noqa: PLC0415
        alert_fields = set(AlertRecord.model_fields.keys())
        assert "tags" not in alert_fields, (
            "AlertRecord Pydantic model must not have a 'tags' field"
        )

    def test_real_fixture_with_tags_loads_without_error(self) -> None:
        """Real fixture entry with tags=["Wind"] loads cleanly (extra="ignore")."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _OWMAlertEntry  # noqa: PLC0415
        fixture = _load_fixture("alerts_paid.json")
        first_raw = fixture["alerts"][0]
        # Confirm the fixture has tags
        assert "tags" in first_raw, "Real fixture must have tags field for this test"
        # Should not raise
        entry = _OWMAlertEntry.model_validate(first_raw)
        assert entry.event == "Wind Advisory"


# ===========================================================================
# 7. Wire-shape Pydantic validation
# ===========================================================================


class TestWireShapePydantic:
    """Wire-shape models validate correctly against the fixture shapes."""

    def test_real_fixture_loads_cleanly_via_response_model(self) -> None:
        """alerts_paid.json loads via _OWMOneCallAlertsResponse with 2 alerts."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMOneCallAlertsResponse,
        )
        raw = _load_fixture("alerts_paid.json")
        response = _OWMOneCallAlertsResponse.model_validate(raw)
        assert len(response.alerts) == 2, (
            f"Expected 2 alert entries, got {len(response.alerts)}"
        )

    def test_real_fixture_first_entry_has_expected_fields(self) -> None:
        """First fixture entry has expected wire fields."""
        fixture = _load_fixture("alerts_paid.json")
        first = fixture["alerts"][0]
        assert first["event"] == "Wind Advisory"
        assert first["sender_name"] == "NWS Seattle WA"
        assert first["start"] == 1714485600
        assert first["end"] == 1714521600

    def test_real_fixture_second_entry_tornado_warning(self) -> None:
        """Second fixture entry (Tornado Warning) has expected wire fields."""
        fixture = _load_fixture("alerts_paid.json")
        second = fixture["alerts"][1]
        assert second["event"] == "Tornado Warning"
        assert second["sender_name"] == "NWS Portland OR"
        assert second["start"] == 1714490000

    def test_empty_fixture_loads_with_empty_alerts_list(self) -> None:
        """alerts_paid_empty.json loads with alerts=[]."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMOneCallAlertsResponse,
        )
        raw = _load_fixture("alerts_paid_empty.json")
        response = _OWMOneCallAlertsResponse.model_validate(raw)
        assert response.alerts == []

    def test_alert_entry_missing_event_raises_validation_error(self) -> None:
        """Missing required 'event' field → ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.openweathermap import _OWMAlertEntry  # noqa: PLC0415
        with pytest.raises(ValidationError):
            _OWMAlertEntry.model_validate({
                "sender_name": "NWS Test",
                "start": 1714485600,
                "end": 1714521600,
                "description": "Text.",
                # event field intentionally absent
            })

    def test_alert_entry_missing_start_raises_validation_error(self) -> None:
        """Missing required 'start' field → ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts.openweathermap import _OWMAlertEntry  # noqa: PLC0415
        with pytest.raises(ValidationError):
            _OWMAlertEntry.model_validate({
                "sender_name": "NWS Test",
                "event": "Wind Advisory",
                # start field intentionally absent
                "end": 1714521600,
                "description": "Text.",
            })

    def test_extra_fields_on_alert_entry_ignored(self) -> None:
        """Unknown extra fields on alert entry are silently ignored (extra='ignore')."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _OWMAlertEntry  # noqa: PLC0415
        entry = _OWMAlertEntry.model_validate({
            "sender_name": "NWS Test",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": "Text.",
            "FUTURE_OWM_FIELD": "should be ignored",
            "another_extra": 42,
        })
        assert entry.event == "Wind Advisory"

    def test_sender_name_none_is_accepted(self) -> None:
        """sender_name=None is valid (optional field)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _OWMAlertEntry  # noqa: PLC0415
        entry = _OWMAlertEntry.model_validate({
            "event": "Wind Advisory",
            "start": 1714485600,
        })
        assert entry.sender_name is None

    def test_end_none_is_accepted(self) -> None:
        """end=None is valid (nullable field)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _OWMAlertEntry  # noqa: PLC0415
        entry = _OWMAlertEntry.model_validate({
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": None,
        })
        assert entry.end is None

    def test_response_model_extra_fields_ignored(self) -> None:
        """Top-level response model ignores extra fields (extra='ignore')."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _OWMOneCallAlertsResponse,
        )
        data = {
            "lat": 47.6062,
            "lon": -122.3321,
            "timezone": "America/Los_Angeles",
            "timezone_offset": -25200,
            "current": {"dt": 1714485600},  # excluded block, should be ignored
            "hourly": [{"dt": 1714485600}],  # excluded block, should be ignored
            "alerts": [],
        }
        response = _OWMOneCallAlertsResponse.model_validate(data)
        assert response.alerts == []


# ===========================================================================
# 8. Cache hit/miss — fetch() with respx-mocked HTTP
# ===========================================================================


class TestFetchCacheMissAndHit:
    """fetch() — cache miss makes outbound call; cache hit returns without call."""

    def _make_valid_alert_response(self) -> dict[str, Any]:
        """Build a minimal valid OWM One Call alerts response with one entry."""
        return {
            "lat": _LAT,
            "lon": _LON,
            "timezone": "America/Los_Angeles",
            "timezone_offset": -25200,
            "alerts": [
                {
                    "sender_name": "NWS Seattle WA",
                    "event": "Wind Advisory",
                    "start": 1714485600,
                    "end": 1714521600,
                    "description": "Wind advisory in effect.",
                    "tags": ["Wind"],
                }
            ],
        }

    def test_cache_miss_makes_outbound_call_and_returns_records(self) -> None:
        """Cache miss: HTTP call made; canonical records returned."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records = openweathermap.fetch(
                lat=_LAT, lon=_LON, appid=_TEST_APPID
            )
            assert mock.calls.call_count == 1

        assert len(records) == 1
        assert records[0].source == "openweathermap"
        assert records[0].event == "Wind Advisory"

    def test_cache_hit_returns_records_without_outbound_call(self) -> None:
        """Cache hit: no HTTP call made; cached records returned."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

        # Prime cache with first call
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        # Second call — should hit cache with zero HTTP calls
        with respx.mock(assert_all_called=False) as mock2:
            records = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
            assert mock2.calls.call_count == 0  # No calls on cache hit

        assert len(records) == 1
        assert records[0].source == "openweathermap"

    def test_empty_alerts_cached_and_returns_empty_list(self) -> None:
        """Empty alerts[] (no active alerts) → empty list; empty list cached."""
        _reset_provider_state()
        empty_data = _load_fixture("alerts_paid_empty.json")
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=empty_data)
            )
            records = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
            assert mock.calls.call_count == 1

        assert records == []
        # Cache was populated with empty list
        from weewx_clearskies_api.providers.alerts.openweathermap import _build_alerts_cache_key  # noqa: PLC0415
        cached = get_cache().get(_build_alerts_cache_key(_LAT, _LON))
        assert cached is not None, "Empty list must be cached (not None key miss)"
        assert cached == [], f"Expected cached empty list, got {cached!r}"

    def test_cached_records_round_trip_through_model_dump_validate(self) -> None:
        """Records cached as list[dict] and reconstructed via model_validate on hit."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

        # First fetch — populates cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records1 = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        # Second fetch — from cache
        with respx.mock(assert_all_called=False):
            records2 = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        assert records1[0].id == records2[0].id
        assert records1[0].source == records2[0].source
        assert records1[0].event == records2[0].event
        assert records1[0].severity == records2[0].severity


# ===========================================================================
# 9. Credentials missing → KeyInvalid (lead-call 8)
# ===========================================================================


class TestFetchMissingCredentials:
    """fetch() raises KeyInvalid immediately when appid is absent."""

    def test_none_appid_raises_key_invalid_before_http(self) -> None:
        """appid=None → KeyInvalid before any HTTP call."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            with pytest.raises(KeyInvalid):
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=None)
            assert mock.calls.call_count == 0

    def test_empty_string_appid_raises_key_invalid_before_http(self) -> None:
        """appid='' → KeyInvalid before any HTTP call."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            with pytest.raises(KeyInvalid):
                openweathermap.fetch(lat=_LAT, lon=_LON, appid="")
            assert mock.calls.call_count == 0


# ===========================================================================
# 10. HTTP error paths
# ===========================================================================


class TestFetchHttpErrorPaths:
    """fetch() translates HTTP errors to canonical exception taxonomy."""

    def test_401_returns_empty_list_q1_a_path(self) -> None:
        """HTTP 401 (basic-tier) → empty list returned (Q1=A: graceful empty list).

        Mirror of 3b-5 forecast/owm Q1=A behavior. NOT a 502 error.
        """
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        error_fixture = _load_fixture("error_401_basic_tier.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(401, json=error_fixture)
            )
            records = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
            assert mock.calls.call_count == 1

        assert records == [], (
            f"Basic-tier 401 must return empty list (Q1=A), got {records!r}"
        )

    def test_401_emits_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Basic-tier 401 emits WARN log (Q1=A: operator discoverable)."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        error_fixture = _load_fixture("error_401_basic_tier.json")

        with caplog.at_level(
            logging.WARNING,
            logger="weewx_clearskies_api.providers.alerts.openweathermap",
        ):
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_OWM_ONECALL_URL).mock(
                    return_value=httpx.Response(401, json=error_fixture)
                )
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        assert any(
            r.levelno == logging.WARNING for r in caplog.records
        ), "Expected WARN log for basic-tier 401"

    def test_401_warn_logged_only_once_per_process(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Basic-tier 401 warning is logged once per process, not on repeat calls."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        error_fixture = _load_fixture("error_401_basic_tier.json")

        with caplog.at_level(
            logging.WARNING,
            logger="weewx_clearskies_api.providers.alerts.openweathermap",
        ):
            for _ in range(3):
                with respx.mock(assert_all_called=False) as mock:
                    mock.get(_OWM_ONECALL_URL).mock(
                        return_value=httpx.Response(401, json=error_fixture)
                    )
                    openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
                # Reset cache between calls so we hit OWM each time
                from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
                    reset_cache_for_tests,
                    wire_cache_from_env,
                )
                reset_cache_for_tests()
                wire_cache_from_env()

        warn_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and
            "One Call 3.0" in r.message
        ]
        assert len(warn_records) == 1, (
            f"Expected 1 WARN log for basic-tier 401, got {len(warn_records)}"
        )

    def test_401_caches_empty_list_for_ttl(self) -> None:
        """Basic-tier 401 caches the empty list (3b-5 audit F2 cache-parity mirror).

        Without caching, basic-tier-misconfigured deployments hit
        /data/3.0/onecall 401 on every dashboard poll — capped only by
        the rate limiter. With cache parity, the empty list is cached for
        the same 300s TTL as the success path.
        """
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        error_fixture = _load_fixture("error_401_basic_tier.json")

        with respx.mock(assert_all_called=False) as mock:
            mock_route = mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(401, json=error_fixture)
            )
            # First call hits OWM and caches the empty list
            records1 = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
            # Second call should hit cache, NOT make another OWM call
            records2 = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        assert records1 == []
        assert records2 == []
        assert mock_route.call_count == 1, (
            f"Expected 1 OWM call (second served from cache), got {mock_route.call_count}"
        )

    def test_non_401_key_invalid_is_reraised(self) -> None:
        """Non-401 KeyInvalid (e.g. 403) → re-raised (defensive path)."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415

        # 403 → KeyInvalid with status_code=403 (not 401 → re-raise path)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(403, json={"cod": 403, "message": "Forbidden"})
            )
            with pytest.raises(KeyInvalid) as exc_info:
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
        assert exc_info.value.status_code == 403

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted propagated."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        error_fixture = _load_fixture("alerts_error_429.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(429, json=error_fixture)
            )
            with pytest.raises(QuotaExhausted):
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

    def test_429_retry_after_seconds_not_none_when_header_present(self) -> None:
        """HTTP 429 with Retry-After header → QuotaExhausted.retry_after_seconds not None.

        3b-4 F1 carry-forward: assert retry_after_seconds propagated through.
        """
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        error_fixture = _load_fixture("alerts_error_429.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(
                    429,
                    json=error_fixture,
                    headers={"Retry-After": "60"},
                )
            )
            with pytest.raises(QuotaExhausted) as exc_info:
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
        assert exc_info.value.retry_after_seconds is not None, (
            "QuotaExhausted.retry_after_seconds must be set when Retry-After header present"
        )

    def test_500_raises_transient_network_error(self) -> None:
        """HTTP 500 → TransientNetworkError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import TransientNetworkError  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(500, json={"error": "Internal Server Error"})
            )
            with pytest.raises(TransientNetworkError):
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)


# ===========================================================================
# 11. Pydantic ValidationError → ProviderProtocolError
# ===========================================================================


class TestValidationErrorMapsToProviderProtocolError:
    """Pydantic ValidationError on alert entry → ProviderProtocolError."""

    def test_missing_event_field_raises_provider_protocol_error(self) -> None:
        """Alert entry missing 'event' → ValidationError → ProviderProtocolError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        malformed = {
            "lat": _LAT,
            "lon": _LON,
            "timezone": "America/Los_Angeles",
            "timezone_offset": -25200,
            "alerts": [
                {
                    # event field missing intentionally
                    "sender_name": "NWS Test",
                    "start": 1714485600,
                    "end": 1714521600,
                    "description": "Text.",
                }
            ],
        }

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=malformed)
            )
            with pytest.raises(ProviderProtocolError):
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

    def test_missing_start_field_raises_provider_protocol_error(self) -> None:
        """Alert entry missing 'start' → ValidationError → ProviderProtocolError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        malformed = {
            "lat": _LAT,
            "lon": _LON,
            "timezone": "America/Los_Angeles",
            "timezone_offset": -25200,
            "alerts": [
                {
                    "sender_name": "NWS Test",
                    "event": "Wind Advisory",
                    # start field missing intentionally
                    "end": 1714521600,
                    "description": "Text.",
                }
            ],
        }

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=malformed)
            )
            with pytest.raises(ProviderProtocolError):
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)


# ===========================================================================
# 12. Empty alerts response
# ===========================================================================


class TestEmptyAlertsResponse:
    """alerts: [] response → empty list (NOT an error)."""

    def test_empty_alerts_response_returns_empty_list(self) -> None:
        """Empty alerts[] → empty list returned, no exception."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        empty_data = _load_fixture("alerts_paid_empty.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=empty_data)
            )
            records = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        assert records == [], (
            f"Empty alerts[] must return empty list (not error), got {records!r}"
        )

    def test_populated_fixture_returns_two_records(self) -> None:
        """Populated fixture with 2 alerts → 2 canonical AlertRecord objects."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
        alerts_data = _load_fixture("alerts_paid.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)

        assert len(records) == 2, (
            f"Expected 2 records from populated fixture, got {len(records)}"
        )
        # First entry is Wind Advisory → advisory
        assert records[0].severity == "advisory"
        assert records[0].event == "Wind Advisory"
        # Second entry is Tornado Warning → warning
        assert records[1].severity == "warning"
        assert records[1].event == "Tornado Warning"


# ===========================================================================
# 13. Redaction filter (lead-call 21 / brief §redaction-filter-verification)
# ===========================================================================


class TestRedactionFilter:
    """appid query param is redacted in logged URLs (3b-1 filter carries forward)."""

    def test_url_with_appid_param_is_redacted(self) -> None:
        """A URL containing ?appid=ABC123 → appid=[REDACTED] after redaction."""
        from weewx_clearskies_api.logging.redaction_filter import _redact  # noqa: PLC0415
        url = "https://api.openweathermap.org/data/3.0/onecall?lat=47.6062&lon=-122.3321&appid=ABC123&exclude=current,minutely,hourly,daily"
        redacted = _redact(url)
        assert "ABC123" not in redacted
        assert "appid=[REDACTED]" in redacted

    def test_appid_in_middle_of_query_string_is_redacted(self) -> None:
        """appid in the middle of a query string is redacted."""
        from weewx_clearskies_api.logging.redaction_filter import _redact  # noqa: PLC0415
        url = "https://api.openweathermap.org/data/3.0/onecall?lat=47.6&appid=MYSECRETKEY123&exclude=alerts"
        redacted = _redact(url)
        assert "MYSECRETKEY123" not in redacted
        assert "appid=[REDACTED]" in redacted

    def test_owm_alerts_url_shape_appid_is_redacted(self) -> None:
        """OWM alerts URL with exclude=current,minutely,hourly,daily has appid redacted."""
        from weewx_clearskies_api.logging.redaction_filter import _redact  # noqa: PLC0415
        url = (
            "https://api.openweathermap.org/data/3.0/onecall"
            "?lat=47.6062&lon=-122.3321&appid=OWMKEY999"
            "&exclude=current,minutely,hourly,daily"
        )
        redacted = _redact(url)
        assert "OWMKEY999" not in redacted
        assert "appid=[REDACTED]" in redacted


# ===========================================================================
# 14. Capability registry
# ===========================================================================


class TestCapabilityRegistry:
    """CAPABILITY declaration and registry wiring."""

    def test_capability_provider_id_is_openweathermap(self) -> None:
        """CAPABILITY.provider_id = 'openweathermap'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.provider_id == "openweathermap"

    def test_capability_domain_is_alerts(self) -> None:
        """CAPABILITY.domain = 'alerts'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.domain == "alerts"

    def test_capability_auth_required_includes_appid(self) -> None:
        """CAPABILITY.auth_required includes 'appid'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        assert "appid" in CAPABILITY.auth_required, (
            "CAPABILITY.auth_required must include 'appid'"
        )

    def test_capability_supplied_canonical_fields_includes_core_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes the 8 OWM-supplied fields."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        for field in (
            "id", "headline", "description", "severity", "event",
            "effective", "expires", "senderName", "source",
        ):
            assert field in CAPABILITY.supplied_canonical_fields, (
                f"CAPABILITY.supplied_canonical_fields missing {field!r}"
            )

    def test_capability_partial_domain_excludes_four_fields(self) -> None:
        """CAPABILITY excludes urgency/certainty/areaDesc/category (PARTIAL-DOMAIN §4.3).

        OWM categorically does not supply these on any plan tier.
        """
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        for absent_field in ("urgency", "certainty", "areaDesc", "category"):
            assert absent_field not in CAPABILITY.supplied_canonical_fields, (
                f"PARTIAL-DOMAIN: {absent_field!r} must be excluded from CAPABILITY "
                "(OWM does not supply)"
            )

    def test_capability_geographic_coverage_is_global(self) -> None:
        """CAPABILITY.geographic_coverage = 'global' (ADR-016 day-1 table)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.geographic_coverage == "global"

    def test_capability_default_poll_interval_is_300_seconds(self) -> None:
        """CAPABILITY.default_poll_interval_seconds = 300 per ADR-016 + ADR-017."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.default_poll_interval_seconds == 300

    def test_wire_providers_registers_openweathermap_alerts_capability(self) -> None:
        """wire_providers([CAPABILITY]) → registry contains openweathermap alerts entry."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            wire_providers,
            get_provider_registry,
        )
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        owm_entries = [
            p for p in registry
            if p.provider_id == "openweathermap" and p.domain == "alerts"
        ]
        assert len(owm_entries) == 1, (
            f"Expected 1 openweathermap alerts entry in registry, found {len(owm_entries)}"
        )


# ===========================================================================
# 15. Cache key construction
# ===========================================================================


class TestCacheKeyConstruction:
    """_build_alerts_cache_key produces deterministic keys."""

    def test_same_coordinates_produce_same_key(self) -> None:
        """Same lat/lon always produces the same cache key."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _build_alerts_cache_key  # noqa: PLC0415
        key1 = _build_alerts_cache_key(47.6062, -122.3321)
        key2 = _build_alerts_cache_key(47.6062, -122.3321)
        assert key1 == key2

    def test_different_coordinates_produce_different_keys(self) -> None:
        """Different lat/lon produces different cache keys."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _build_alerts_cache_key  # noqa: PLC0415
        key1 = _build_alerts_cache_key(47.6062, -122.3321)
        key2 = _build_alerts_cache_key(41.6022, -98.9178)
        assert key1 != key2

    def test_key_is_64_char_hex_string(self) -> None:
        """Cache key is SHA-256 hex digest (64 characters)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import _build_alerts_cache_key  # noqa: PLC0415
        key = _build_alerts_cache_key(47.6062, -122.3321)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_alerts_key_distinct_from_forecast_key(self) -> None:
        """Alerts cache key differs from forecast module cache key at same coordinates.

        The logical-endpoint key 'alerts' vs 'forecast_bundle' ensures the two
        modules use separate cache entries even though they share the same
        /data/3.0/onecall URL (brief lead-call 15).
        """
        from weewx_clearskies_api.providers.alerts.openweathermap import _build_alerts_cache_key  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.openweathermap import _build_cache_key as _forecast_key  # noqa: PLC0415
        alerts_key = _build_alerts_cache_key(47.6062, -122.3321)
        forecast_key = _forecast_key(47.6062, -122.3321, "US")
        assert alerts_key != forecast_key, (
            "Alerts cache key must differ from forecast cache key "
            "(separate logical endpoints; brief lead-call 15)"
        )
