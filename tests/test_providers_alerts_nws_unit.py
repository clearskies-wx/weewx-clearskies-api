"""Unit tests for the NWS alerts provider (ADR-052 event-name-tier severity).

Covers:

  Severity normalization — _normalize_severity (ADR-052):
  - "Tornado Warning"          → (4, "Warning")
  - "Flash Flood Watch"        → (3, "Watch")
  - "Wind Advisory"            → (2, "Advisory")
  - "Special Weather Statement"→ (1, "Statement")
  - Unknown suffix / no match  → (1, "Statement") + WARNING log
  - Multiple suffix cases for all four tiers.

  Wire → canonical — _to_canonical:
  - severityLevel + severityLabel from event name suffix.
  - alertSystem = "nws".
  - hazardType = None.
  - nativeName = None.
  - color = None.
  - description = description + "\\n\\n" + instruction when instruction present.
  - description = description alone when instruction is None.
  - effective / expires UTC conversion.
  - Real fixture (alerts_active.json) flows through without error.

  Wire-shape Pydantic:
  - alerts_active.json fixture loads cleanly.
  - alerts_active_empty.json → empty features list.
  - Missing required "event" field raises ValidationError.
  - Missing required "effective" field raises ValidationError.
  - Extra fields in properties are ignored.

  Cache hit/miss — fetch():
  - Cache miss → outbound HTTP call → records stored.
  - Cache hit → no HTTP call.
  - Cached records round-trip through model_dump/model_validate.

  User-Agent construction:
  - Contact set → UA string includes contact.
  - Contact None → UA string without contact + WARNING log.

  HTTP error paths:
  - HTTP 429 → QuotaExhausted; retry_after_seconds propagated.
  - HTTP 500 → TransientNetworkError.
  - Malformed JSON → ProviderProtocolError.

  Empty response (outside US coverage):
  - Empty features [] → empty canonical list (no exception).

  Capability registry:
  - CAPABILITY.provider_id = "nws", domain = "alerts".
  - CAPABILITY.auth_required is empty (keyless provider).
  - CAPABILITY.supplied_canonical_fields includes severityLevel, severityLabel, alertSystem.
  - wire_providers([nws.CAPABILITY]) → registry has nws alerts entry.

No DB, no live network. respx mocks outbound httpx calls.
Wire-shape rule: fixtures loaded from tests/fixtures/providers/nws/*.json.
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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "nws"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file from tests/fixtures/providers/nws/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helpers
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, and re-wire memory cache."""
    import weewx_clearskies_api.providers.alerts.nws as _nws  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
        _reset_http_client_for_tests,
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    # Clear rate-limiter deque so consecutive tests don't trip each other.
    _nws._rate_limiter._calls.clear()
    # Re-wire a clean memory cache (CLEARSKIES_CACHE_URL unset in unit test env).
    wire_cache_from_env()


# NWS alerts URL for respx mocking
_LAT = 47.6062
_LON = -122.3321
_NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
_TEST_UA_CONTACT = "test@example.com"


# ===========================================================================
# 1. Severity normalization — _normalize_severity (ADR-052)
# ===========================================================================


class TestNwsNormalizeSeverity:
    """_normalize_severity derives (severityLevel, severityLabel) from event name suffix.

    ADR-052 replaces the unreliable NWS CAP severity field (Extreme/Severe/Moderate/Minor)
    with tier derived from the event name suffix:
      " Warning"   → (4, "Warning")
      " Watch"     → (3, "Watch")
      " Advisory"  → (2, "Advisory")
      " Statement" → (1, "Statement")
      no match     → (1, "Statement") + WARNING log
    """

    def test_tornado_warning_yields_level_4_warning(self) -> None:
        """'Tornado Warning' → (4, 'Warning')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Tornado Warning") == (4, "Warning")

    def test_flash_flood_warning_yields_level_4_warning(self) -> None:
        """'Flash Flood Warning' → (4, 'Warning')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Flash Flood Warning") == (4, "Warning")

    def test_winter_storm_warning_yields_level_4_warning(self) -> None:
        """'Winter Storm Warning' → (4, 'Warning')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Winter Storm Warning") == (4, "Warning")

    def test_flash_flood_watch_yields_level_3_watch(self) -> None:
        """'Flash Flood Watch' → (3, 'Watch')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Flash Flood Watch") == (3, "Watch")

    def test_severe_thunderstorm_watch_yields_level_3_watch(self) -> None:
        """'Severe Thunderstorm Watch' → (3, 'Watch')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Severe Thunderstorm Watch") == (3, "Watch")

    def test_tornado_watch_yields_level_3_watch(self) -> None:
        """'Tornado Watch' → (3, 'Watch')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Tornado Watch") == (3, "Watch")

    def test_wind_advisory_yields_level_2_advisory(self) -> None:
        """'Wind Advisory' → (2, 'Advisory')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Wind Advisory") == (2, "Advisory")

    def test_small_craft_advisory_yields_level_2_advisory(self) -> None:
        """'Small Craft Advisory' → (2, 'Advisory')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Small Craft Advisory") == (2, "Advisory")

    def test_lake_wind_advisory_yields_level_2_advisory(self) -> None:
        """'Lake Wind Advisory' → (2, 'Advisory')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Lake Wind Advisory") == (2, "Advisory")

    def test_special_weather_statement_yields_level_1_statement(self) -> None:
        """'Special Weather Statement' → (1, 'Statement')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        assert _normalize_severity("Special Weather Statement") == (1, "Statement")

    def test_hazardous_weather_outlook_yields_level_1_statement(self) -> None:
        """'Hazardous Weather Outlook' → (1, 'Statement') (no recognized suffix → default)."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        level, label = _normalize_severity("Hazardous Weather Outlook")
        assert level == 1
        assert label == "Statement"

    def test_unknown_event_name_yields_level_1_statement(self) -> None:
        """Unknown event name with no recognized suffix → (1, 'Statement')."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        level, label = _normalize_severity("Some New NWS Event Type")
        assert level == 1
        assert label == "Statement"

    def test_unknown_event_name_emits_warning_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown event name emits WARNING log to surface schema drift."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        with caplog.at_level(
            logging.WARNING, logger="weewx_clearskies_api.providers.alerts.nws"
        ):
            _normalize_severity("Some Unknown Event")
        assert len(caplog.records) >= 1, "Expected WARNING log for unknown event name suffix"

    def test_warning_suffix_is_case_exact_match(self) -> None:
        """Suffix matching is exact (ends with ' Warning', not case-insensitive)."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        # Real NWS names always capitalize; the suffix check is endswith, not case-fold.
        # " warning" (lower case) must NOT match the " Warning" tier.
        level, label = _normalize_severity("tornado warning")
        # Provider code checks for " Warning" exactly — lower-case won't match any tier
        # and falls to the default (1, "Statement").
        assert level == 1

    def test_return_type_is_tuple_of_int_and_str(self) -> None:
        """_normalize_severity returns (int, str) tuple."""
        from weewx_clearskies_api.providers.alerts.nws import _normalize_severity  # noqa: PLC0415
        result = _normalize_severity("Wind Advisory")
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], str)


# ===========================================================================
# 2. Wire → canonical — _to_canonical (ADR-052 fields)
# ===========================================================================


class TestNwsToCanonical:
    """_to_canonical maps _NwsAlertProperties to AlertRecord with ADR-052 fields."""

    def _make_props(self, **overrides: Any) -> Any:
        """Build a minimal _NwsAlertProperties with known-good defaults."""
        from weewx_clearskies_api.providers.alerts.nws import _NwsAlertProperties  # noqa: PLC0415
        base: dict[str, Any] = {
            "id": "urn:oid:test.001",
            "event": "Wind Advisory",
            "effective": "2026-04-30T16:00:00-07:00",
            "expires": "2026-04-30T22:00:00-07:00",
            "headline": "Wind Advisory in effect",
            "description": "Strong winds expected.",
            "instruction": None,
            "severity": "Moderate",
        }
        base.update(overrides)
        return _NwsAlertProperties.model_validate(base)

    def test_wind_advisory_yields_severity_level_2_advisory(self) -> None:
        """'Wind Advisory' event → severityLevel=2, severityLabel='Advisory'."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(event="Wind Advisory")
        result = _to_canonical(props)
        assert result.severityLevel == 2
        assert result.severityLabel == "Advisory"

    def test_tornado_warning_yields_severity_level_4_warning(self) -> None:
        """'Tornado Warning' event → severityLevel=4, severityLabel='Warning'."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(event="Tornado Warning")
        result = _to_canonical(props)
        assert result.severityLevel == 4
        assert result.severityLabel == "Warning"

    def test_flash_flood_watch_yields_severity_level_3_watch(self) -> None:
        """'Flash Flood Watch' event → severityLevel=3, severityLabel='Watch'."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(event="Flash Flood Watch")
        result = _to_canonical(props)
        assert result.severityLevel == 3
        assert result.severityLabel == "Watch"

    def test_special_weather_statement_yields_severity_level_1_statement(self) -> None:
        """'Special Weather Statement' event → severityLevel=1, severityLabel='Statement'."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(event="Special Weather Statement")
        result = _to_canonical(props)
        assert result.severityLevel == 1
        assert result.severityLabel == "Statement"

    def test_alert_system_is_nws(self) -> None:
        """alertSystem is always 'nws' for NWS provider."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props()
        result = _to_canonical(props)
        assert result.alertSystem == "nws"

    def test_hazard_type_is_none(self) -> None:
        """hazardType is always None for NWS provider (ADR-052 passthrough)."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props()
        result = _to_canonical(props)
        assert result.hazardType is None

    def test_native_name_is_none(self) -> None:
        """nativeName is always None for NWS provider (English-only; no localLanguages)."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props()
        result = _to_canonical(props)
        assert result.nativeName is None

    def test_color_is_none(self) -> None:
        """color is always None for NWS provider (no color metadata in NWS wire)."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props()
        result = _to_canonical(props)
        assert result.color is None

    def test_source_is_nws(self) -> None:
        """source = 'nws' (provider_id literal)."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props()
        result = _to_canonical(props)
        assert result.source == "nws"

    def test_description_appends_instruction_with_double_newline(self) -> None:
        """description + '\\n\\n' + instruction when instruction is present."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(
            description="Strong winds expected.",
            instruction="Use extra caution when driving.",
        )
        result = _to_canonical(props)
        assert result.description == "Strong winds expected.\n\nUse extra caution when driving."

    def test_description_alone_when_instruction_is_none(self) -> None:
        """description passthrough without modification when instruction is None."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(
            description="Strong winds expected.",
            instruction=None,
        )
        result = _to_canonical(props)
        assert result.description == "Strong winds expected."

    def test_effective_converted_to_utc_z(self) -> None:
        """effective with -07:00 offset → UTC Z suffix."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(effective="2026-04-30T16:00:00-07:00")
        result = _to_canonical(props)
        assert result.effective == "2026-04-30T23:00:00Z", (
            f"Expected '2026-04-30T23:00:00Z', got {result.effective!r}"
        )

    def test_expires_converted_to_utc_z(self) -> None:
        """expires with -07:00 offset → UTC Z suffix."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(expires="2026-04-30T22:00:00-07:00")
        result = _to_canonical(props)
        assert result.expires == "2026-05-01T05:00:00Z", (
            f"Expected '2026-05-01T05:00:00Z', got {result.expires!r}"
        )

    def test_expires_none_maps_to_none(self) -> None:
        """expires=None on wire → expires=None on canonical record."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(expires=None)
        result = _to_canonical(props)
        assert result.expires is None

    def test_id_passthrough(self) -> None:
        """id passes through from properties.id."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(id="urn:oid:specific.test.id")
        result = _to_canonical(props)
        assert result.id == "urn:oid:specific.test.id"

    def test_headline_passthrough(self) -> None:
        """headline passes through from properties.headline."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(headline="Wind Advisory in effect until 10 PM")
        result = _to_canonical(props)
        assert result.headline == "Wind Advisory in effect until 10 PM"

    def test_urgency_passthrough(self) -> None:
        """urgency passes through from properties.urgency."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(urgency="Expected")
        result = _to_canonical(props)
        assert result.urgency == "Expected"

    def test_certainty_passthrough(self) -> None:
        """certainty passes through from properties.certainty."""
        from weewx_clearskies_api.providers.alerts.nws import _to_canonical  # noqa: PLC0415
        props = self._make_props(certainty="Likely")
        result = _to_canonical(props)
        assert result.certainty == "Likely"

    def test_real_fixture_wind_advisory_fields(self) -> None:
        """Real fixture 'Wind Advisory' → severity level 2 / label 'Advisory'."""
        from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
            _NwsAlertProperties,
            _to_canonical,
        )
        fixture = _load_fixture("alerts_active.json")
        first_props = fixture["features"][0]["properties"]
        props = _NwsAlertProperties.model_validate(first_props)
        result = _to_canonical(props)
        assert result.severityLevel == 2
        assert result.severityLabel == "Advisory"
        assert result.alertSystem == "nws"
        assert result.hazardType is None
        assert result.nativeName is None
        assert result.color is None

    def test_real_fixture_second_alert_small_craft_advisory(self) -> None:
        """Real fixture second alert 'Small Craft Advisory' → severity level 2 / 'Advisory'."""
        from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
            _NwsAlertProperties,
            _to_canonical,
        )
        fixture = _load_fixture("alerts_active.json")
        second_props = fixture["features"][1]["properties"]
        props = _NwsAlertProperties.model_validate(second_props)
        result = _to_canonical(props)
        assert result.severityLevel == 2
        assert result.severityLabel == "Advisory"


# ===========================================================================
# 3. Wire-shape Pydantic validation
# ===========================================================================


class TestNwsWireShapePydantic:
    """Wire-shape models validate correctly against fixture shapes."""

    def test_real_fixture_loads_cleanly(self) -> None:
        """alerts_active.json loads via _NwsAlertsActiveResponse (2 features)."""
        from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
            _NwsAlertsActiveResponse,
        )
        raw = _load_fixture("alerts_active.json")
        response = _NwsAlertsActiveResponse.model_validate(raw)
        assert len(response.features) == 2

    def test_empty_fixture_loads_with_no_features(self) -> None:
        """alerts_active_empty.json loads with features=[]."""
        from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
            _NwsAlertsActiveResponse,
        )
        raw = _load_fixture("alerts_active_empty.json")
        response = _NwsAlertsActiveResponse.model_validate(raw)
        assert response.features == []

    def test_missing_event_raises_validation_error(self) -> None:
        """Missing required 'event' field on properties raises ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
            _NwsAlertProperties,
        )
        with pytest.raises(ValidationError):
            _NwsAlertProperties.model_validate({
                "id": "urn:oid:test",
                # event missing intentionally
                "effective": "2026-04-30T16:00:00-07:00",
                "headline": "Test",
                "description": "Test.",
                "severity": "Moderate",
            })

    def test_missing_effective_raises_validation_error(self) -> None:
        """Missing required 'effective' field on properties raises ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
            _NwsAlertProperties,
        )
        with pytest.raises(ValidationError):
            _NwsAlertProperties.model_validate({
                "id": "urn:oid:test",
                "event": "Wind Advisory",
                # effective missing intentionally
                "headline": "Test",
                "description": "Test.",
                "severity": "Moderate",
            })

    def test_extra_fields_in_properties_ignored(self) -> None:
        """Unknown extra fields in alert properties are silently ignored (extra='ignore')."""
        from weewx_clearskies_api.providers.alerts.nws import (  # noqa: PLC0415
            _NwsAlertProperties,
        )
        props = _NwsAlertProperties.model_validate({
            "id": "urn:oid:test",
            "event": "Wind Advisory",
            "effective": "2026-04-30T16:00:00-07:00",
            "headline": "Test",
            "description": "Test.",
            "severity": "Moderate",
            "FUTURE_NWS_FIELD": "should be ignored",
        })
        assert props.event == "Wind Advisory"

    def test_real_fixture_first_alert_has_expected_fields(self) -> None:
        """Real fixture first alert has expected wire field values."""
        fixture = _load_fixture("alerts_active.json")
        first = fixture["features"][0]["properties"]
        assert first["event"] == "Wind Advisory"
        assert first["senderName"] == "NWS Seattle WA"
        assert first["severity"] == "Moderate"

    def test_real_fixture_second_alert_has_null_instruction(self) -> None:
        """Real fixture second alert has instruction=null (no instruction-append expected)."""
        fixture = _load_fixture("alerts_active.json")
        second = fixture["features"][1]["properties"]
        assert second["instruction"] is None
        assert second["event"] == "Small Craft Advisory"


# ===========================================================================
# 4. User-Agent construction — _build_user_agent
# ===========================================================================


class TestNwsUserAgentConstruction:
    """_build_user_agent constructs NWS User-Agent string per ADR-006."""

    def test_contact_set_includes_contact_in_ua(self) -> None:
        """Contact set → UA string includes '(weewx-clearskies-api/<version>, <contact>)'."""
        from weewx_clearskies_api.providers.alerts.nws import _build_user_agent  # noqa: PLC0415
        ua = _build_user_agent("test@example.com")
        assert "weewx-clearskies-api" in ua
        assert "test@example.com" in ua

    def test_contact_none_omits_contact_from_ua(self) -> None:
        """Contact None → UA string without contact."""
        from weewx_clearskies_api.providers.alerts.nws import _build_user_agent  # noqa: PLC0415
        ua = _build_user_agent(None)
        assert "weewx-clearskies-api" in ua
        assert "@" not in ua  # no email in UA when contact unset

    def test_contact_empty_string_omits_contact(self) -> None:
        """Contact empty string → treated as unset."""
        from weewx_clearskies_api.providers.alerts.nws import _build_user_agent  # noqa: PLC0415
        ua = _build_user_agent("")
        assert "weewx-clearskies-api" in ua

    def test_missing_contact_emits_warning_log_on_fetch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing contact → WARN log on first fetch call."""
        _reset_provider_state()
        empty_data = _load_fixture("alerts_active_empty.json")
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        with caplog.at_level(
            logging.WARNING, logger="weewx_clearskies_api.providers.alerts.nws"
        ):
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_NWS_ALERTS_URL).mock(
                    return_value=httpx.Response(200, json=empty_data)
                )
                nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=None)

        assert any(
            "nws_user_agent_contact" in r.message.lower()
            or "user-agent" in r.message.lower()
            or "contact" in r.message.lower()
            for r in caplog.records
        ), "Expected WARNING log about missing NWS User-Agent contact"


# ===========================================================================
# 5. Cache hit/miss — fetch() with respx-mocked HTTP
# ===========================================================================


class TestFetchCacheMissAndHit:
    """fetch() — cache miss makes outbound call; cache hit returns without call."""

    def _make_valid_alert_response(self) -> dict[str, Any]:
        """Build a valid NWS FeatureCollection with one Wind Advisory alert."""
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "id": "urn:oid:test.cache.001",
                    "type": "Feature",
                    "geometry": None,
                    "properties": {
                        "id": "urn:oid:test.cache.001",
                        "areaDesc": "King, WA",
                        "sent": "2026-04-30T16:00:00-07:00",
                        "effective": "2026-04-30T16:00:00-07:00",
                        "expires": "2026-04-30T22:00:00-07:00",
                        "status": "Actual",
                        "messageType": "Alert",
                        "category": "Met",
                        "severity": "Moderate",
                        "certainty": "Likely",
                        "urgency": "Expected",
                        "event": "Wind Advisory",
                        "sender": "w-nws.webmaster@noaa.gov",
                        "senderName": "NWS Seattle WA",
                        "headline": "Wind Advisory issued April 30",
                        "description": "Strong winds expected.",
                        "instruction": None,
                        "response": "Execute",
                    },
                }
            ],
            "title": "Current watches, warnings, and advisories",
            "updated": "2026-04-30T23:15:00+00:00",
        }

    def test_cache_miss_makes_outbound_call_and_returns_records(self) -> None:
        """Cache miss: HTTP call made; canonical records returned."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records = nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)
            assert mock.calls.call_count == 1

        assert len(records) == 1
        assert records[0].source == "nws"
        assert records[0].event == "Wind Advisory"
        assert records[0].severityLevel == 2
        assert records[0].severityLabel == "Advisory"
        assert records[0].alertSystem == "nws"

    def test_cache_hit_returns_records_without_outbound_call(self) -> None:
        """Cache hit: no HTTP call made; cached records returned."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        # Prime cache with first call
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)

        # Second call — should hit cache with zero HTTP calls
        with respx.mock(assert_all_called=False) as mock2:
            records = nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)
            assert mock2.calls.call_count == 0

        assert len(records) == 1
        assert records[0].source == "nws"

    def test_empty_response_cached_and_returns_empty_list(self) -> None:
        """Empty features[] → empty list; empty list cached."""
        _reset_provider_state()
        empty_data = _load_fixture("alerts_active_empty.json")
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=empty_data)
            )
            records = nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)
            assert mock.calls.call_count == 1

        assert records == []
        from weewx_clearskies_api.providers.alerts.nws import _build_cache_key  # noqa: PLC0415
        cached = get_cache().get(_build_cache_key(_LAT, _LON))
        assert cached == []

    def test_cached_records_round_trip_through_model_dump_validate(self) -> None:
        """Records cached as list[dict] and reconstructed via model_validate on hit."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        # First fetch — populates cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records1 = nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)

        # Second fetch — from cache
        with respx.mock(assert_all_called=False):
            records2 = nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)

        assert records1[0].id == records2[0].id
        assert records1[0].severityLevel == records2[0].severityLevel
        assert records1[0].severityLabel == records2[0].severityLabel
        assert records1[0].alertSystem == records2[0].alertSystem


# ===========================================================================
# 6. HTTP error paths
# ===========================================================================


class TestFetchHttpErrorPaths:
    """fetch() translates HTTP errors to canonical exception taxonomy."""

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(429, json={"title": "Too Many Requests"})
            )
            with pytest.raises(QuotaExhausted):
                nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)

    def test_429_retry_after_seconds_propagated(self) -> None:
        """HTTP 429 with Retry-After header → QuotaExhausted.retry_after_seconds not None."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"title": "Too Many Requests"},
                    headers={"Retry-After": "60"},
                )
            )
            with pytest.raises(QuotaExhausted) as exc_info:
                nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)
        assert exc_info.value.retry_after_seconds is not None, (
            "QuotaExhausted.retry_after_seconds must be set when Retry-After header present"
        )

    def test_500_raises_transient_network_error(self) -> None:
        """HTTP 500 → TransientNetworkError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import (  # noqa: PLC0415
            TransientNetworkError,
        )
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(500, json={"detail": "Internal Server Error"})
            )
            with pytest.raises(TransientNetworkError):
                nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)

    def test_malformed_json_raises_provider_protocol_error(self) -> None:
        """Malformed / schema-violating JSON → ProviderProtocolError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import (  # noqa: PLC0415
            ProviderProtocolError,
        )
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415
        malformed = _load_fixture("alerts_active_malformed.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=malformed)
            )
            with pytest.raises(ProviderProtocolError):
                nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)

    def test_real_fixture_flows_without_error(self) -> None:
        """Real fixture (alerts_active.json) flows through fetch() returning 2 records."""
        _reset_provider_state()
        real_fixture = _load_fixture("alerts_active.json")
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NWS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=real_fixture)
            )
            records = nws.fetch(lat=_LAT, lon=_LON, user_agent_contact=_TEST_UA_CONTACT)

        assert len(records) == 2
        assert all(r.source == "nws" for r in records)
        assert all(r.alertSystem == "nws" for r in records)
        assert all(r.severityLevel in (1, 2, 3, 4) for r in records)


# ===========================================================================
# 7. Capability registry
# ===========================================================================


class TestCapabilityRegistry:
    """CAPABILITY declaration and registry wiring."""

    def test_capability_provider_id_is_nws(self) -> None:
        """CAPABILITY.provider_id = 'nws'."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.provider_id == "nws"

    def test_capability_domain_is_alerts(self) -> None:
        """CAPABILITY.domain = 'alerts'."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.domain == "alerts"

    def test_capability_auth_required_is_empty(self) -> None:
        """CAPABILITY.auth_required is empty — NWS is a keyless provider."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert len(CAPABILITY.auth_required) == 0, (
            "NWS is keyless — auth_required must be empty"
        )

    def test_capability_supplied_fields_includes_severity_level(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes 'severityLevel' (ADR-052)."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert "severityLevel" in CAPABILITY.supplied_canonical_fields, (
            "ADR-052: severityLevel must be in CAPABILITY.supplied_canonical_fields"
        )

    def test_capability_supplied_fields_includes_severity_label(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes 'severityLabel' (ADR-052)."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert "severityLabel" in CAPABILITY.supplied_canonical_fields, (
            "ADR-052: severityLabel must be in CAPABILITY.supplied_canonical_fields"
        )

    def test_capability_supplied_fields_includes_alert_system(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes 'alertSystem' (ADR-052)."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert "alertSystem" in CAPABILITY.supplied_canonical_fields, (
            "ADR-052: alertSystem must be in CAPABILITY.supplied_canonical_fields"
        )

    def test_capability_supplied_fields_includes_core_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes id, headline, event, effective, source."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        for field in ("id", "headline", "description", "event", "effective", "source"):
            assert field in CAPABILITY.supplied_canonical_fields, (
                f"CAPABILITY.supplied_canonical_fields missing {field!r}"
            )

    def test_capability_geographic_coverage_is_us(self) -> None:
        """CAPABILITY.geographic_coverage = 'us' (US + territories + adjacent marine zones)."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.geographic_coverage == "us"

    def test_capability_default_poll_interval_is_300_seconds(self) -> None:
        """CAPABILITY.default_poll_interval_seconds = 300 per ADR-016 + ADR-017."""
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.default_poll_interval_seconds == 300

    def test_wire_providers_registers_nws_alerts_capability(self) -> None:
        """wire_providers([nws.CAPABILITY]) → registry contains nws alerts entry."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            wire_providers,
        )
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY  # noqa: PLC0415
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        nws_entries = [
            p for p in registry
            if p.provider_id == "nws" and p.domain == "alerts"
        ]
        assert len(nws_entries) == 1, (
            f"Expected 1 nws alerts entry in registry, found {len(nws_entries)}"
        )


# ===========================================================================
# 8. Cache key construction
# ===========================================================================


class TestCacheKeyConstruction:
    """_build_cache_key produces deterministic SHA-256 keys."""

    def test_same_coordinates_produce_same_key(self) -> None:
        """Same lat/lon always produces the same cache key."""
        from weewx_clearskies_api.providers.alerts.nws import _build_cache_key  # noqa: PLC0415
        key1 = _build_cache_key(47.6062, -122.3321)
        key2 = _build_cache_key(47.6062, -122.3321)
        assert key1 == key2

    def test_different_coordinates_produce_different_keys(self) -> None:
        """Different lat/lon produces different cache keys."""
        from weewx_clearskies_api.providers.alerts.nws import _build_cache_key  # noqa: PLC0415
        key1 = _build_cache_key(47.6062, -122.3321)
        key2 = _build_cache_key(41.6022, -98.9178)
        assert key1 != key2

    def test_key_is_64_char_hex_string(self) -> None:
        """Cache key is SHA-256 hex digest (64 characters)."""
        from weewx_clearskies_api.providers.alerts.nws import _build_cache_key  # noqa: PLC0415
        key = _build_cache_key(47.6062, -122.3321)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)
