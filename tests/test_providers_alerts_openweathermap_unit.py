"""Unit tests for the OpenWeatherMap alerts provider (ADR-052 passthrough, updated 2026-06-01).

Covers per the task-3b-8 brief §Test plan (unit tests section) as amended by ADR-052:

  Wire-shape Pydantic validation:
  - alerts_paid.json fixture loads cleanly against _OWMAlertEntry / _OWMOneCallAlertsResponse.
  - Extra fields (tags) NOT dropped — tags are now mapped to hazardType (ADR-052).
  - Missing required event field → ValidationError → ProviderProtocolError.
  - Missing required start field → ValidationError → ProviderProtocolError.

  ADR-052 passthrough mode:
  - severityLevel = None (OWM strips structured severity from originating agency data).
  - severityLabel = None (passthrough — no severity to label).
  - nativeName = None (OWM event field is already English).
  - color = None (OWM does not supply color codes).
  - hazardType from tags[0] when tags present.
  - hazardType = None when tags absent or empty.
  - alertSystem parsed from sender_name:
      "NWS ..." → "nws"
      "Met Office ..." → "ukmet"
      "Météo-France ..." → "meteofrance"
      Unknown → None

  alertSystem derivation — _owm_alert_system_from_sender:
  - "NWS Seattle WA" → "nws".
  - "Met Office UK" → "ukmet".
  - "Météo-France" → "meteofrance".
  - "Meteo-France Nord-Pas-de-Calais" → "meteofrance".
  - "Bureau of Meteorology" → None (unknown sender).
  - None sender → None.
  - Empty string sender → None.

  Datetime conversion (lead-call 14):
  - epoch_to_utc_iso8601: epoch UTC → ISO Z string.
  - start=1714485600 → "2024-04-30T14:00:00Z" (verified UTC).
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
  - CAPABILITY.supplied_canonical_fields includes expected fields (hazardType, alertSystem, NOT severity).
  - CAPABILITY.supplied_canonical_fields excludes urgency/certainty/areaDesc/category (PARTIAL-DOMAIN).
  - CAPABILITY.supplied_canonical_fields excludes severityLevel, severityLabel, nativeName, color
    (ADR-052 passthrough: OWM strips structured severity).
  - CAPABILITY.geographic_coverage = "global".
  - CAPABILITY.default_poll_interval_seconds = 300.
  - wire_providers([CAPABILITY]) → registry has openweathermap alerts entry.

No DB, no live network. respx mocks outbound httpx calls.
Wire-shape rule: fixtures loaded from tests/fixtures/providers/openweathermap/alerts_*.json
(synthetic-from-api-docs-example per brief L3 rule).
ADR references: ADR-006, ADR-016, ADR-017, ADR-038, ADR-052.
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
    import weewx_clearskies_api.providers.alerts.openweathermap as _owm_alerts  # noqa: PLC0415
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
# 1. ADR-052 passthrough mode — severityLevel/severityLabel/nativeName/color
# ===========================================================================


class TestOwmPassthroughSeverityFields:
    """ADR-052 passthrough: OWM strips structured severity — all None on canonical record.

    OWM does not preserve structured severity metadata from the originating
    national agency. The dashboard renders OWM alerts with generic/neutral treatment.
    """

    def _make_canonical_record(self, tags: list[str] | None = None) -> Any:
        """Make a canonical AlertRecord from a minimal OWM alert entry."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _owm_alert_to_canonical,
            _OWMAlertEntry,
        )
        entry_data: dict[str, Any] = {
            "sender_name": "NWS Seattle WA",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": "Advisory text.",
        }
        if tags is not None:
            entry_data["tags"] = tags
        entry = _OWMAlertEntry.model_validate(entry_data)
        return _owm_alert_to_canonical(entry)

    def test_severity_level_is_none_passthrough(self) -> None:
        """severityLevel = None (ADR-052 passthrough — OWM strips structured severity)."""
        record = self._make_canonical_record()
        assert record.severityLevel is None, (
            f"Expected severityLevel=None (ADR-052 passthrough), got {record.severityLevel!r}"
        )

    def test_severity_label_is_none_passthrough(self) -> None:
        """severityLabel = None (ADR-052 passthrough — no severity to label)."""
        record = self._make_canonical_record()
        assert record.severityLabel is None, (
            f"Expected severityLabel=None (ADR-052 passthrough), got {record.severityLabel!r}"
        )

    def test_native_name_is_none_passthrough(self) -> None:
        """nativeName = None (OWM event field is already English; no native-language original)."""
        record = self._make_canonical_record()
        assert record.nativeName is None, (
            f"Expected nativeName=None, got {record.nativeName!r}"
        )

    def test_color_is_none_passthrough(self) -> None:
        """color = None (OWM does not supply color codes)."""
        record = self._make_canonical_record()
        assert record.color is None, (
            f"Expected color=None, got {record.color!r}"
        )

    def test_hazard_type_from_tags_first_element(self) -> None:
        """hazardType = tags[0] when tags array is present and non-empty."""
        record = self._make_canonical_record(tags=["Wind"])
        assert record.hazardType == "Wind", (
            f"Expected hazardType='Wind' from tags[0], got {record.hazardType!r}"
        )

    def test_hazard_type_none_when_tags_absent(self) -> None:
        """hazardType = None when tags array is absent from wire entry."""
        record = self._make_canonical_record(tags=None)
        assert record.hazardType is None, (
            f"Expected hazardType=None when tags absent, got {record.hazardType!r}"
        )

    def test_hazard_type_none_when_tags_empty(self) -> None:
        """hazardType = None when tags array is present but empty."""
        record = self._make_canonical_record(tags=[])
        assert record.hazardType is None, (
            f"Expected hazardType=None when tags=[], got {record.hazardType!r}"
        )

    def test_hazard_type_uses_first_tag_only(self) -> None:
        """hazardType = tags[0] when multiple tags present (only first used)."""
        record = self._make_canonical_record(tags=["Thunderstorm", "Hail", "Wind"])
        assert record.hazardType == "Thunderstorm", (
            f"Expected hazardType='Thunderstorm' (first tag), got {record.hazardType!r}"
        )


# ===========================================================================
# 2. alertSystem derivation — _owm_alert_system_from_sender
# ===========================================================================


class TestOwmAlertSystemFromSender:
    """_owm_alert_system_from_sender parses alertSystem from OWM sender_name."""

    def test_nws_sender_name_maps_to_nws(self) -> None:
        """'NWS Seattle WA' → 'nws' (contains 'NWS')."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("NWS Seattle WA") == "nws"

    def test_nws_portland_maps_to_nws(self) -> None:
        """'NWS Portland OR' → 'nws'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("NWS Portland OR") == "nws"

    def test_met_office_maps_to_ukmet(self) -> None:
        """'Met Office' → 'ukmet'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("Met Office") == "ukmet"

    def test_met_office_uk_maps_to_ukmet(self) -> None:
        """'Met Office UK Amber Warning' → 'ukmet' (substring match)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("Met Office UK Amber Warning") == "ukmet"

    def test_meteo_france_unicode_maps_to_meteofrance(self) -> None:
        """'Météo-France' (accented) → 'meteofrance'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("Météo-France") == "meteofrance"

    def test_meteo_france_ascii_maps_to_meteofrance(self) -> None:
        """'Meteo-France Nord-Pas-de-Calais' (ASCII fallback) → 'meteofrance'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("Meteo-France Nord-Pas-de-Calais") == "meteofrance"

    def test_unknown_sender_returns_none(self) -> None:
        """Unknown sender (e.g. BoM) → None (passthrough mode per ADR-052)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("Bureau of Meteorology") is None

    def test_none_sender_returns_none(self) -> None:
        """None sender_name → None."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender(None) is None

    def test_empty_string_sender_returns_none(self) -> None:
        """Empty string sender_name → None."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_system_from_sender,  # noqa: PLC0415
        )
        assert _owm_alert_system_from_sender("") is None

    def test_alert_system_set_on_canonical_record_for_nws_sender(self) -> None:
        """Canonical record has alertSystem='nws' when sender contains 'NWS'."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _owm_alert_to_canonical,
            _OWMAlertEntry,
        )
        entry = _OWMAlertEntry.model_validate({
            "sender_name": "NWS Seattle WA",
            "event": "Wind Advisory",
            "start": 1714485600,
        })
        record = _owm_alert_to_canonical(entry)
        assert record.alertSystem == "nws"

    def test_alert_system_none_on_canonical_record_for_unknown_sender(self) -> None:
        """Canonical record has alertSystem=None for unknown sender."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _owm_alert_to_canonical,
            _OWMAlertEntry,
        )
        entry = _OWMAlertEntry.model_validate({
            "sender_name": "Bureau of Meteorology",
            "event": "Severe Thunderstorm Warning",
            "start": 1714485600,
        })
        record = _owm_alert_to_canonical(entry)
        assert record.alertSystem is None


# ===========================================================================
# 3. Datetime conversion (lead-call 14)
# ===========================================================================


class TestDatetimeConversion:
    """epoch_to_utc_iso8601 converts OWM epoch seconds to UTC ISO Z strings."""

    def test_epoch_converts_to_utc_iso8601_z_string(self) -> None:
        """epoch=1714485600 → UTC ISO-8601 Z string ending in Z."""
        from weewx_clearskies_api.providers._common.datetime_utils import (
            epoch_to_utc_iso8601,  # noqa: PLC0415
        )
        result = epoch_to_utc_iso8601(1714485600, provider_id="openweathermap", domain="alerts")
        assert result.endswith("Z"), f"Expected UTC Z suffix, got {result!r}"

    def test_epoch_converts_to_correct_utc_datetime(self) -> None:
        """epoch=1714485600 → 2024-04-30T14:00:00Z (verified UTC)."""
        from weewx_clearskies_api.providers._common.datetime_utils import (
            epoch_to_utc_iso8601,  # noqa: PLC0415
        )
        result = epoch_to_utc_iso8601(1714485600, provider_id="openweathermap", domain="alerts")
        assert result == "2024-04-30T14:00:00Z", (
            f"Expected '2024-04-30T14:00:00Z', got {result!r}"
        )

    def test_owm_alert_entry_effective_is_utc_z(self) -> None:
        """_owm_alert_to_canonical: start epoch → effective UTC Z."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _owm_alert_to_canonical,
            _OWMAlertEntry,
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
            _owm_alert_to_canonical,
            _OWMAlertEntry,
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
            _owm_alert_to_canonical,
            _OWMAlertEntry,
        )
        fixture = _load_fixture("alerts_paid.json")
        first_raw = fixture["alerts"][0]
        entry = _OWMAlertEntry.model_validate(first_raw)
        record = _owm_alert_to_canonical(entry)
        assert record.effective.endswith("Z"), (
            f"Real fixture effective must be UTC Z, got {record.effective!r}"
        )


# ===========================================================================
# 4. ID synthesis (lead-call 13)
# ===========================================================================


class TestIdSynthesis:
    """_synthesize_alert_id produces deterministic pipe-delimited IDs."""

    def test_normal_case_produces_pipe_delimited_id(self) -> None:
        """Normal: ("Wind Advisory", 1714485600, "NWS Seattle WA") → expected ID."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _synthesize_alert_id,  # noqa: PLC0415
        )
        result = _synthesize_alert_id("Wind Advisory", 1714485600, "NWS Seattle WA")
        assert result == "Wind Advisory|1714485600|NWS Seattle WA", (
            f"Expected pipe-delimited ID, got {result!r}"
        )

    def test_none_sender_name_produces_empty_trailing_segment(self) -> None:
        """sender_name=None → trailing empty segment after last pipe."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _synthesize_alert_id,  # noqa: PLC0415
        )
        result = _synthesize_alert_id("Foo", 100, None)
        assert result == "Foo|100|", (
            f"Expected 'Foo|100|' for None sender_name, got {result!r}"
        )

    def test_empty_sender_name_produces_empty_trailing_segment(self) -> None:
        """sender_name="" → trailing empty segment after last pipe."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _synthesize_alert_id,  # noqa: PLC0415
        )
        result = _synthesize_alert_id("Foo", 100, "")
        assert result == "Foo|100|", (
            f"Expected 'Foo|100|' for empty sender_name, got {result!r}"
        )

    def test_id_synthesis_from_real_fixture_first_entry(self) -> None:
        """Real fixture entry 1: synthesized ID has expected format."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _owm_alert_to_canonical,
            _OWMAlertEntry,
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
            _owm_alert_to_canonical,
            _OWMAlertEntry,
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
# 5. Description passthrough (no instruction-append)
# ===========================================================================


class TestDescriptionPassthrough:
    """description field passes through without modification.

    OWM has no instruction field (unlike NWS which appends instruction with
    double-newline). Canonical description = wire description, verbatim.
    """

    def _make_entry(self, description: str | None) -> Any:
        """Build a minimal _OWMAlertEntry with the given description."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
        return _OWMAlertEntry.model_validate({
            "sender_name": "NWS Test",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": description,
        })

    def test_description_text_is_passed_through_unchanged(self) -> None:
        """Wire description text is not modified."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_to_canonical,  # noqa: PLC0415
        )
        text = "* WHAT...Southerly winds 20 to 30 mph with gusts up to 45 mph."
        entry = self._make_entry(text)
        record = _owm_alert_to_canonical(entry)
        assert record.description == text, (
            f"Expected description unchanged, got {record.description!r}"
        )

    def test_description_does_not_append_instruction(self) -> None:
        """No NWS-style instruction-append for OWM (brief: passthrough only)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_to_canonical,  # noqa: PLC0415
        )
        text = "Test advisory body."
        entry = self._make_entry(text)
        record = _owm_alert_to_canonical(entry)
        assert record.description == text
        assert "\n\n" not in record.description, (
            "OWM description must not have instruction appended with double-newline"
        )

    def test_description_none_maps_to_none_or_empty(self) -> None:
        """Wire description=None → None or empty string on canonical record (not an error)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _owm_alert_to_canonical,  # noqa: PLC0415
        )
        entry = self._make_entry(None)
        record = _owm_alert_to_canonical(entry)
        # None or empty string both acceptable — no assertion on which; just that it doesn't raise.
        assert record.description is None or isinstance(record.description, str)


# ===========================================================================
# 6. PARTIAL-DOMAIN fields (lead-call 16)
# ===========================================================================


class TestPartialDomainFields:
    """urgency/certainty/areaDesc/category are always None (PARTIAL-DOMAIN).

    OWM categorically does not supply these fields on any plan tier.
    PARTIAL-DOMAIN per canonical-data-model §4.3 OWM column.
    """

    def _make_canonical_record(self) -> Any:
        """Make a canonical AlertRecord from a minimal OWM alert entry."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _owm_alert_to_canonical,
            _OWMAlertEntry,
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

        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
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

        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
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
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
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

    def test_tags_field_accepted_on_wire_entry(self) -> None:
        """tags array in wire entry loads cleanly into _OWMAlertEntry."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
        entry_data = {
            "sender_name": "NWS Seattle WA",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": "Winds 20-30 mph.",
            "tags": ["Wind"],
        }
        entry = _OWMAlertEntry.model_validate(entry_data)
        assert entry.event == "Wind Advisory"
        assert entry.tags == ["Wind"]

    def test_tags_field_maps_to_hazard_type_on_canonical_record(self) -> None:
        """Wire tags=['Wind'] → canonical record hazardType='Wind' (ADR-052)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (  # noqa: PLC0415
            _owm_alert_to_canonical,
            _OWMAlertEntry,
        )
        entry_data = {
            "sender_name": "NWS Seattle WA",
            "event": "Wind Advisory",
            "start": 1714485600,
            "end": 1714521600,
            "description": "Winds 20-30 mph.",
            "tags": ["Wind"],
        }
        entry = _OWMAlertEntry.model_validate(entry_data)
        record = _owm_alert_to_canonical(entry)
        assert record.hazardType == "Wind", (
            f"Expected hazardType='Wind' from tags[0], got {record.hazardType!r}"
        )

    def test_real_fixture_with_tags_loads_without_error(self) -> None:
        """Real fixture entry with tags=["Wind"] loads cleanly."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
        fixture = _load_fixture("alerts_paid.json")
        first_raw = fixture["alerts"][0]
        assert "tags" in first_raw, "Real fixture must have tags field for this test"
        entry = _OWMAlertEntry.model_validate(first_raw)
        assert entry.event == "Wind Advisory"

    def test_sender_name_none_is_accepted(self) -> None:
        """sender_name=None is valid (optional field)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
        entry = _OWMAlertEntry.model_validate({
            "event": "Wind Advisory",
            "start": 1714485600,
        })
        assert entry.sender_name is None

    def test_end_none_is_accepted(self) -> None:
        """end=None is valid (nullable field)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _OWMAlertEntry,  # noqa: PLC0415
        )
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
        assert records[0].severityLevel is None
        assert records[0].severityLabel is None
        assert records[0].alertSystem == "nws"
        assert records[0].hazardType == "Wind"

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
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_ONECALL_URL).mock(
                return_value=httpx.Response(200, json=empty_data)
            )
            records = openweathermap.fetch(lat=_LAT, lon=_LON, appid=_TEST_APPID)
            assert mock.calls.call_count == 1

        assert records == []
        # Cache was populated with empty list
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _build_alerts_cache_key,  # noqa: PLC0415
        )
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
        assert records1[0].severityLevel == records2[0].severityLevel  # both None
        assert records1[0].alertSystem == records2[0].alertSystem


# ===========================================================================
# 9. Credentials missing → KeyInvalid (lead-call 8)
# ===========================================================================


class TestFetchMissingCredentials:
    """fetch() raises KeyInvalid immediately when appid is absent."""

    def test_none_appid_raises_key_invalid_before_http(self) -> None:
        """appid=None → KeyInvalid before any HTTP call."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            with pytest.raises(KeyInvalid):
                openweathermap.fetch(lat=_LAT, lon=_LON, appid=None)
            assert mock.calls.call_count == 0

    def test_empty_string_appid_raises_key_invalid_before_http(self) -> None:
        """appid='' → KeyInvalid before any HTTP call."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

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
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

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
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
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
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
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
        from weewx_clearskies_api.providers._common.errors import (
            TransientNetworkError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

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
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
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
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415
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

    def test_populated_fixture_returns_two_records_with_passthrough_severity(self) -> None:
        """Populated fixture with 2 alerts → 2 canonical AlertRecord objects, severityLevel=None."""
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
        # ADR-052 passthrough: severityLevel and severityLabel are None for all OWM records
        for record in records:
            assert record.severityLevel is None, (
                f"ADR-052 passthrough: severityLevel must be None, got {record.severityLevel!r}"
            )
            assert record.severityLabel is None, (
                f"ADR-052 passthrough: severityLabel must be None, got {record.severityLabel!r}"
            )
        # First entry is Wind Advisory
        assert records[0].event == "Wind Advisory"
        # Second entry is Tornado Warning
        assert records[1].event == "Tornado Warning"
        # alertSystem parsed from sender_name ("NWS ...") → "nws"
        assert records[0].alertSystem == "nws"
        assert records[1].alertSystem == "nws"


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

    def test_capability_supplied_fields_includes_core_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes the OWM-supplied fields."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        for field in (
            "id", "headline", "description", "event",
            "effective", "expires", "senderName", "source",
            "hazardType", "alertSystem",
        ):
            assert field in CAPABILITY.supplied_canonical_fields, (
                f"CAPABILITY.supplied_canonical_fields missing {field!r}"
            )

    def test_capability_does_not_include_old_severity_string(self) -> None:
        """CAPABILITY.supplied_canonical_fields does NOT include old 'severity' string field."""
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        assert "severity" not in CAPABILITY.supplied_canonical_fields, (
            "Old 'severity' string field must NOT be in CAPABILITY (replaced by ADR-052 passthrough)"
        )

    def test_capability_excludes_adr052_passthrough_fields(self) -> None:
        """CAPABILITY excludes severityLevel, severityLabel, nativeName, color (ADR-052 passthrough).

        OWM strips structured severity from the originating agency — these fields
        are NOT in CAPABILITY because OWM cannot supply them on any tier.
        """
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
        for passthrough_field in ("severityLevel", "severityLabel", "nativeName", "color"):
            assert passthrough_field not in CAPABILITY.supplied_canonical_fields, (
                f"ADR-052 passthrough: {passthrough_field!r} must NOT be in CAPABILITY "
                "(OWM strips structured severity data)"
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
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            wire_providers,
        )
        from weewx_clearskies_api.providers.alerts.openweathermap import CAPABILITY  # noqa: PLC0415
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
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _build_alerts_cache_key,  # noqa: PLC0415
        )
        key1 = _build_alerts_cache_key(47.6062, -122.3321)
        key2 = _build_alerts_cache_key(47.6062, -122.3321)
        assert key1 == key2

    def test_different_coordinates_produce_different_keys(self) -> None:
        """Different lat/lon produces different cache keys."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _build_alerts_cache_key,  # noqa: PLC0415
        )
        key1 = _build_alerts_cache_key(47.6062, -122.3321)
        key2 = _build_alerts_cache_key(41.6022, -98.9178)
        assert key1 != key2

    def test_key_is_64_char_hex_string(self) -> None:
        """Cache key is SHA-256 hex digest (64 characters)."""
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _build_alerts_cache_key,  # noqa: PLC0415
        )
        key = _build_alerts_cache_key(47.6062, -122.3321)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_alerts_key_distinct_from_forecast_key(self) -> None:
        """Alerts cache key differs from forecast module cache key at same coordinates.

        The logical-endpoint key 'alerts' vs 'forecast_bundle' ensures the two
        modules use separate cache entries even though they share the same
        /data/3.0/onecall URL (brief lead-call 15).
        """
        from weewx_clearskies_api.providers.alerts.openweathermap import (
            _build_alerts_cache_key,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast.openweathermap import (
            _build_cache_key as _forecast_key,  # noqa: PLC0415
        )
        alerts_key = _build_alerts_cache_key(47.6062, -122.3321)
        forecast_key = _forecast_key(47.6062, -122.3321, "US")
        assert alerts_key != forecast_key, (
            "Alerts cache key must differ from forecast cache key "
            "(separate logical endpoints; brief lead-call 15)"
        )
