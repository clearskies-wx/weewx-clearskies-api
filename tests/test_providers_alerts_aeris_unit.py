"""Unit tests for the Aeris alerts provider (3b round 7).

Covers per the task-3b-7 brief §Test plan (unit tests section):

  Wire-shape Pydantic:
  - Real fixture (alerts.json) loads cleanly against _AerisAlertRecord / _AerisEnvelope.
  - details.emergency=False (boolean) wire-shape behaviour — real fixture has boolean.
  - Missing required `id` field raises ValidationError.
  - Extra fields ignored (extra="ignore").
  - Envelope: success=true parses, success=false parses, warn_location parses.
  - All fields in real fixture validate; urgency/certainty/category are absent
    from real free-tier wire (PARTIAL-DOMAIN call 16 confirmed).

  Severity normalization (canonical-data-model §4.3 amended 2026-05-09):
  - VTEC suffix dispatch: .W→warning, .A→watch, .Y→advisory, .S→advisory.
  - Aeris suffix dispatch (non-US): .EX→warning, .SV→watch, .MD/.MN→advisory.
  - Unknown suffix (e.g. real fixture "FW.A" → "watch") and no-suffix → "advisory" + WARNING log.
  - None / empty type code → "advisory" + WARNING log.

  Datetime conversion:
  - Offset-aware ISO string → UTC Z via to_utc_iso8601_from_offset.
  - expiresISO None → expires=None on canonical record.
  - issuedISO absent → effective=None (with WARNING log).

  senderName disjunction (brief call 19, Q2 user decision 2026-05-09):
  - emergency non-empty string → senderName = emergency.strip().
  - emergency absent/None/False (real wire) → fall back to place.name.
  - emergency="" (empty string) → fall back to place.name.
  - place.name only (no emergency) → senderName = place.name.
  - both empty → senderName = None per Q2 decision.
  - emergency=False boolean (real wire) → senderName falls back to place.name
    (because bool False is falsy; the str | None type coercion matters).

  Description passthrough:
  - details.body passed through without modification (no instruction-append).
  - details.body=None → empty string on canonical record.

  urgency/certainty/category passthrough:
  - When absent from wire (real fixture) → None on canonical record.
  - When present in wire → passed through unchanged.

  Cache hit/miss:
  - Cache miss → outbound HTTP call → records stored.
  - Cache hit → no HTTP call → cached records returned.
  - Cached records round-trip through model_dump/model_validate.

  Credentials missing → KeyInvalid (brief call 8):
  - client_id=None → KeyInvalid before any HTTP call.
  - client_secret=None → KeyInvalid before any HTTP call.
  - both missing → KeyInvalid.

  HTTP error paths:
  - HTTP 401 → KeyInvalid (exc.status_code == 401; F2 attribute-dispatch).
  - HTTP 429 → QuotaExhausted; retry_after_seconds propagated.
  - HTTP 500 → TransientNetworkError.

  Aeris envelope error paths:
  - success=false → ProviderProtocolError.
  - success=true + warn_location → WARNING log + empty list.
  - Pydantic ValidationError on record → ProviderProtocolError + body logged.

  Capability registry:
  - CAPABILITY.provider_id = "aeris", domain = "alerts".
  - CAPABILITY.auth_required includes "client_id" and "client_secret".
  - CAPABILITY.supplied_canonical_fields includes expected fields.
  - wire_providers([aeris.CAPABILITY]) → registry has aeris alerts entry.

No DB, no live network. respx mocks outbound httpx calls.
Wire-shape rule: fixtures loaded from tests/fixtures/providers/aeris/*.json
(real Aeris response shapes per rules/clearskies-process.md).

Wire-shape findings from real capture (see alerts.md sidecar) — RESOLVED via 2026-05-09 amendment:
  - details.priority is NOT severity; it's a NOAA hazard-map display-priority code.
    Severity now derives from the suffix on details.type (VTEC for US/CA, EX/SV/MD/MN for non-US).
  - details.emergency type is bool | str | None (real wire returns False when no text).
  - details.urgency/certainty are not Aeris response fields; PARTIAL-DOMAIN — always None.
  - category reads from details.cat (real wire field name), not details.category.

ADR references: ADR-006, ADR-016, ADR-017, ADR-018, ADR-038.
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

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "aeris"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file from tests/fixtures/providers/aeris/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helpers
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, and re-wire memory cache."""
    import weewx_clearskies_api.providers.alerts.aeris as _aeris  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.alerts.aeris import (
        _reset_http_client_for_tests,  # noqa: PLC0415
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    # Clear rate-limiter deque so consecutive tests don't trip each other.
    _aeris._rate_limiter._calls.clear()
    # Re-wire a clean memory cache (CLEARSKIES_CACHE_URL unset in unit test env).
    wire_cache_from_env()


# Aeris alerts URL for respx mocking
_LAT = 41.6022
_LON = -98.9178
_LOCATION = f"{round(_LAT, 4)},{round(_LON, 4)}"
_AERIS_ALERTS_URL = f"https://data.api.xweather.com/alerts/{_LOCATION}"

_TEST_CLIENT_ID = "TEST_CLIENT_ID"
_TEST_CLIENT_SECRET = "TEST_CLIENT_SECRET"


# ===========================================================================
# 1. Severity normalization — _parse_severity_from_type (amended 2026-05-09)
# ===========================================================================


class TestAerisSeverityFromType:
    """_parse_severity_from_type parses details.type suffix to canonical severity enum.

    canonical-data-model §4.3 amendment 2026-05-09: severity is encoded in the
    details.type suffix (VTEC for US/CA, EX/SV/MD/MN for non-US), not the
    details.priority field which is a NOAA hazard-map display-priority code.
    """

    # --- US/Canadian VTEC suffixes ---

    def test_vtec_warning_suffix_maps_to_warning(self) -> None:
        """`TO.W` (Tornado Warning, VTEC `.W`) → 'warning'."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("TO.W") == "warning"

    def test_vtec_watch_suffix_maps_to_watch(self) -> None:
        """`FW.A` (Fire Weather Watch, VTEC `.A`) → 'watch'. Real fixture case."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("FW.A") == "watch"

    def test_vtec_advisory_suffix_maps_to_advisory(self) -> None:
        """`WI.Y` (Wind Advisory, VTEC `.Y`) → 'advisory'."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("WI.Y") == "advisory"

    def test_vtec_statement_suffix_maps_to_advisory(self) -> None:
        """`SV.S` (Severe Weather Statement, VTEC `.S`) → 'advisory'."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("SV.S") == "advisory"

    # --- Non-US Aeris severity suffixes ---

    def test_aeris_extreme_suffix_maps_to_warning(self) -> None:
        """`AW.TS.EX` (Extreme thunderstorm) → 'warning'."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("AW.TS.EX") == "warning"

    def test_aeris_severe_suffix_maps_to_watch(self) -> None:
        """`AW.TS.SV` (Severe thunderstorm) → 'watch'."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("AW.TS.SV") == "watch"

    def test_aeris_moderate_suffix_maps_to_advisory(self) -> None:
        """`AW.TS.MD` (Moderate thunderstorm — api-docs example) → 'advisory'."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("AW.TS.MD") == "advisory"

    def test_aeris_minor_suffix_maps_to_advisory(self) -> None:
        """`AW.TS.MN` (Minor thunderstorm) → 'advisory'."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("AW.TS.MN") == "advisory"

    # --- Dispatch-table coverage ---

    def test_vtec_dispatch_table_covers_all_four_codes(self) -> None:
        """_VTEC_SUFFIX_TO_SEVERITY has W, A, Y, S keys (NWS VTEC action codes)."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _VTEC_SUFFIX_TO_SEVERITY,  # noqa: PLC0415
        )
        for suffix in ("W", "A", "Y", "S"):
            assert suffix in _VTEC_SUFFIX_TO_SEVERITY, (
                f"_VTEC_SUFFIX_TO_SEVERITY missing suffix {suffix!r}"
            )

    def test_aeris_dispatch_table_covers_all_four_codes(self) -> None:
        """_AERIS_SUFFIX_TO_SEVERITY has EX, SV, MD, MN keys (Aeris non-US severity codes)."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _AERIS_SUFFIX_TO_SEVERITY,  # noqa: PLC0415
        )
        for suffix in ("EX", "SV", "MD", "MN"):
            assert suffix in _AERIS_SUFFIX_TO_SEVERITY, (
                f"_AERIS_SUFFIX_TO_SEVERITY missing suffix {suffix!r}"
            )

    # --- Unknown / empty / fallback ---

    def test_unknown_suffix_defaults_to_advisory(self) -> None:
        """Unknown suffix → 'advisory' default."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("XX.YY.ZZ") == "advisory"

    def test_no_suffix_defaults_to_advisory(self) -> None:
        """type code with no dot (e.g. 'TOR') → suffix='TOR', not in dispatch tables → advisory."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("TOR") == "advisory"

    def test_unknown_suffix_emits_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Unknown suffix emits WARNING log to surface schema drift to operator."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.alerts.aeris"):
            _parse_severity_from_type("XX.YY.ZZ")
        assert any("ZZ" in record.message for record in caplog.records), (
            "Expected WARNING log mentioning unknown suffix"
        )

    def test_none_type_defaults_to_advisory(self) -> None:
        """None type → 'advisory' default with WARNING log."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type(None) == "advisory"

    def test_empty_type_defaults_to_advisory(self) -> None:
        """Empty string type → 'advisory' default with WARNING log."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        assert _parse_severity_from_type("") == "advisory"

    def test_none_type_emits_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """None/empty type emits WARNING log."""
        from weewx_clearskies_api.providers.alerts.aeris import (
            _parse_severity_from_type,  # noqa: PLC0415
        )
        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.alerts.aeris"):
            _parse_severity_from_type(None)
        assert any("null" in record.message.lower() or "empty" in record.message.lower()
                   for record in caplog.records), (
            "Expected WARNING log for null/empty type"
        )

    # --- Real-fixture integration ---

    def test_real_fixture_type_FW_A_yields_watch(self) -> None:
        """Real fixture 'FW.A' (Fire Weather Watch) → severity 'watch'."""
        from weewx_clearskies_api.providers.alerts.aeris import (  # noqa: PLC0415
            _AerisAlertRecord,
            _to_canonical,
        )
        fixture = _load_fixture("alerts.json")
        first_raw = fixture["response"][0]
        record = _AerisAlertRecord.model_validate(first_raw)
        result = _to_canonical(record)
        assert result.severity == "watch", (
            f"Real fixture FW.A should yield severity='watch', got {result.severity!r}"
        )


# ===========================================================================
# 2. Datetime conversion
# ===========================================================================


class TestAerisDatetimeConversion:
    """Datetime fields converted via to_utc_iso8601_from_offset."""

    def _make_record(self, **overrides: Any) -> Any:
        """Build a minimal _AerisAlertRecord with known-good defaults."""
        from weewx_clearskies_api.providers.alerts.aeris import (  # noqa: PLC0415
            _AerisAlertRecord,
        )
        base: dict[str, Any] = {
            "id": "test-alert-001",
            "details": {
                "type": "WI.Y",
                "name": "Wind Advisory",
                "priority": 2,
                "body": "Test body.",
                # emergency is absent (not present = None, which is valid for str|None)
            },
            "timestamps": {
                "issuedISO": "2026-05-09T10:00:00-05:00",
                "expiresISO": "2026-05-09T22:00:00-05:00",
            },
            "place": {"name": "king", "state": "wa", "country": "us"},
        }
        # Apply overrides at the top level or nested
        base.update(overrides)
        return _AerisAlertRecord.model_validate(base)

    def test_issued_iso_converted_to_utc_z(self) -> None:
        """issuedISO with -05:00 offset → UTC Z suffix on effective."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record(
            timestamps={"issuedISO": "2026-05-09T10:00:00-05:00", "expiresISO": None}
        )
        result = _to_canonical(record)
        assert result.effective == "2026-05-09T15:00:00Z", (
            f"Expected '2026-05-09T15:00:00Z', got {result.effective!r}"
        )

    def test_expires_iso_converted_to_utc_z(self) -> None:
        """expiresISO with -05:00 offset → UTC Z suffix on expires."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record(
            timestamps={"issuedISO": "2026-05-09T10:00:00-05:00", "expiresISO": "2026-05-09T22:00:00-05:00"}
        )
        result = _to_canonical(record)
        assert result.expires == "2026-05-10T03:00:00Z", (
            f"Expected '2026-05-10T03:00:00Z', got {result.expires!r}"
        )

    def test_expires_iso_none_maps_to_none(self) -> None:
        """expiresISO=None → expires=None on canonical record."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record(
            timestamps={"issuedISO": "2026-05-09T10:00:00-05:00", "expiresISO": None}
        )
        result = _to_canonical(record)
        assert result.expires is None

    def test_real_fixture_timestamps_convert_to_utc_z(self) -> None:
        """Real fixture (alerts.json) timestamps are ISO+offset → Z after conversion."""
        from weewx_clearskies_api.providers.alerts.aeris import (  # noqa: PLC0415
            _AerisAlertRecord,
            _to_canonical,
        )
        # Use the real fixture but bypass the emergency=False bug by patching it out
        fixture = _load_fixture("alerts.json")
        first_raw = dict(fixture["response"][0])
        # Remove emergency=False to allow the record to validate (known bug)
        first_raw["details"] = dict(first_raw["details"])
        del first_raw["details"]["emergency"]  # Remove to allow str|None model to pass

        record = _AerisAlertRecord.model_validate(first_raw)
        result = _to_canonical(record)
        # Real fixture issuedISO = "2026-05-09T23:54:00-05:00" → UTC = "2026-05-10T04:54:00Z"
        assert result.effective is not None
        assert result.effective.endswith("Z"), (
            f"effective should be UTC Z, got {result.effective!r}"
        )


# ===========================================================================
# 3. senderName disjunction (brief call 19, Q2)
# ===========================================================================


class TestAerisSenderNameDisjunction:
    """senderName: prefer emergency, else place.name, else None."""

    def _make_record_for_sender(
        self,
        emergency: str | None = None,
        place_name: str | None = None,
    ) -> Any:
        """Build a minimal _AerisAlertRecord for senderName testing.

        emergency and place_name are passed as strings or None.
        (The boolean emergency=False case is tested separately.)
        """
        from weewx_clearskies_api.providers.alerts.aeris import _AerisAlertRecord  # noqa: PLC0415
        details: dict[str, Any] = {
            "type": "WI.Y",
            "name": "Wind Advisory",
            "priority": 2,
            "body": "Test.",
        }
        if emergency is not None:
            details["emergency"] = emergency

        record_data: dict[str, Any] = {
            "id": "test-sender-001",
            "details": details,
            "timestamps": {
                "issuedISO": "2026-05-09T10:00:00-05:00",
                "expiresISO": "2026-05-09T22:00:00-05:00",
            },
        }
        if place_name is not None:
            record_data["place"] = {"name": place_name, "state": "wa", "country": "us"}

        return _AerisAlertRecord.model_validate(record_data)

    def test_emergency_non_empty_string_used_as_sender_name(self) -> None:
        """Non-empty emergency string → senderName = that string."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_for_sender(
            emergency="NWS Hastings NE", place_name="valley"
        )
        result = _to_canonical(record)
        assert result.senderName == "NWS Hastings NE", (
            f"Expected 'NWS Hastings NE', got {result.senderName!r}"
        )

    def test_emergency_stripped_of_whitespace(self) -> None:
        """emergency with leading/trailing whitespace → stripped."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_for_sender(
            emergency="  NWS Hastings NE  ", place_name="valley"
        )
        result = _to_canonical(record)
        assert result.senderName == "NWS Hastings NE", (
            f"Expected 'NWS Hastings NE', got {result.senderName!r}"
        )

    def test_emergency_none_falls_back_to_place_name(self) -> None:
        """emergency=None → fall back to place.name."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_for_sender(emergency=None, place_name="valley")
        result = _to_canonical(record)
        assert result.senderName == "valley", (
            f"Expected 'valley', got {result.senderName!r}"
        )

    def test_emergency_empty_string_falls_back_to_place_name(self) -> None:
        """emergency='' (empty string) → fall back to place.name."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_for_sender(emergency="", place_name="valley")
        result = _to_canonical(record)
        assert result.senderName == "valley", (
            f"Expected 'valley', got {result.senderName!r}"
        )

    def test_place_name_only_no_emergency(self) -> None:
        """No emergency field, place.name present → senderName = place.name."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_for_sender(emergency=None, place_name="valley")
        result = _to_canonical(record)
        assert result.senderName == "valley"

    def test_both_empty_returns_none(self) -> None:
        """emergency=None and no place → senderName = None per Q2 user decision."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_for_sender(emergency=None, place_name=None)
        result = _to_canonical(record)
        assert result.senderName is None, (
            f"Expected None when both emergency and place.name absent, got {result.senderName!r}"
        )

    def test_real_fixture_boolean_emergency_accepted_and_falls_back_to_place_name(self) -> None:
        """Real fixture has emergency=False (boolean) → accepted by bool|str|None field.

        After api-dev fix (bool | str | None), _AerisAlertRecord.model_validate()
        succeeds with emergency=False (boolean). senderName falls back to place.name
        because False is not a non-empty string (isinstance check).

        Real fixture: place.name="valley" → senderName="valley".
        """
        from weewx_clearskies_api.providers.alerts.aeris import (  # noqa: PLC0415
            _AerisAlertRecord,
            _to_canonical,
        )
        fixture = _load_fixture("alerts.json")
        first_raw = fixture["response"][0]
        # Confirm emergency=False is boolean in real fixture
        assert first_raw["details"]["emergency"] is False, (
            "Real fixture must have emergency=False (boolean) to test this path"
        )
        # After fix: model_validate succeeds (bool|str|None accepts bool)
        record = _AerisAlertRecord.model_validate(first_raw)
        assert record.details.emergency is False
        # senderName falls back to place.name because False is not a non-empty string
        canonical = _to_canonical(record)
        assert canonical.senderName == "valley", (
            f"Expected senderName='valley' (place.name fallback), got {canonical.senderName!r}"
        )


# ===========================================================================
# 4. Description passthrough (brief call 13)
# ===========================================================================


class TestAerisDescriptionPassthrough:
    """details.body passes through without modification."""

    def _make_record_for_desc(self, body: str | None) -> Any:
        """Build a minimal _AerisAlertRecord with the given body."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisAlertRecord  # noqa: PLC0415
        return _AerisAlertRecord.model_validate({
            "id": "test-desc-001",
            "details": {
                "type": "WI.Y",
                "name": "Wind Advisory",
                "priority": 2,
                "body": body,
            },
            "timestamps": {
                "issuedISO": "2026-05-09T10:00:00-05:00",
                "expiresISO": None,
            },
        })

    def test_body_text_is_passed_through_unchanged(self) -> None:
        """details.body text is not modified (no instruction-append unlike NWS)."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        body_text = "Strong winds expected across the area."
        record = self._make_record_for_desc(body_text)
        result = _to_canonical(record)
        assert result.description == body_text, (
            f"Expected body unchanged, got {result.description!r}"
        )

    def test_body_none_maps_to_empty_string(self) -> None:
        """details.body=None → empty string on canonical record (not None)."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_for_desc(None)
        result = _to_canonical(record)
        assert result.description == "", (
            f"Expected empty string for None body, got {result.description!r}"
        )

    def test_body_does_not_append_instruction(self) -> None:
        """No NWS-style instruction-append for Aeris (brief call 13: passthrough only)."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        body_text = "Test weather advisory body."
        record = self._make_record_for_desc(body_text)
        result = _to_canonical(record)
        # Confirm description does not have anything appended
        assert result.description == body_text
        assert "\n\n" not in result.description, (
            "Aeris description must not have instruction appended with double-newline"
        )


# ===========================================================================
# 5. urgency / certainty / category passthrough (call 16, PARTIAL-DOMAIN)
# ===========================================================================


class TestAerisFieldPassthrough:
    """category passes through from details.cat; urgency/certainty are PARTIAL-DOMAIN (always None).

    canonical-data-model §4.3 amendment 2026-05-09:
      - urgency, certainty: not Aeris response fields → always None on canonical record.
      - category: real-wire field is `details.cat` (NOT `details.category`).
      - event: maps from `details.name` (human-readable), NOT `details.type` (structured code).
    """

    def _make_record_with_fields(self, **details_overrides: Any) -> Any:
        """Build a minimal record with overridden details fields."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisAlertRecord  # noqa: PLC0415
        details: dict[str, Any] = {
            "type": "WI.Y",
            "name": "Wind Advisory",
            "priority": 2,
            "body": "Test.",
        }
        details.update(details_overrides)
        return _AerisAlertRecord.model_validate({
            "id": "test-passthrough-001",
            "details": details,
            "timestamps": {
                "issuedISO": "2026-05-09T10:00:00-05:00",
                "expiresISO": None,
            },
        })

    def test_urgency_always_none_partial_domain(self) -> None:
        """urgency is PARTIAL-DOMAIN — always None on canonical record (Aeris doesn't supply)."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_with_fields()
        result = _to_canonical(record)
        assert result.urgency is None, (
            f"Expected urgency=None (PARTIAL-DOMAIN), got {result.urgency!r}"
        )

    def test_certainty_always_none_partial_domain(self) -> None:
        """certainty is PARTIAL-DOMAIN — always None on canonical record (Aeris doesn't supply)."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_with_fields()
        result = _to_canonical(record)
        assert result.certainty is None, (
            f"Expected certainty=None (PARTIAL-DOMAIN), got {result.certainty!r}"
        )

    def test_category_none_when_cat_absent_from_wire(self) -> None:
        """`details.cat` absent → category=None on canonical record."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_with_fields()  # no cat in details
        result = _to_canonical(record)
        assert result.category is None, (
            f"Expected category=None when cat absent from wire, got {result.category!r}"
        )

    def test_category_passed_through_from_cat_field(self) -> None:
        """`details.cat` present in wire → passed through to canonical category field."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_with_fields(cat="fire")
        result = _to_canonical(record)
        assert result.category == "fire", (
            f"Expected category='fire' from details.cat, got {result.category!r}"
        )

    def test_real_fixture_yields_category_fire(self) -> None:
        """Real fixture has details.cat='fire' → canonical category='fire'."""
        from weewx_clearskies_api.providers.alerts.aeris import (  # noqa: PLC0415
            _AerisAlertRecord,
            _to_canonical,
        )
        fixture = _load_fixture("alerts.json")
        record = _AerisAlertRecord.model_validate(fixture["response"][0])
        result = _to_canonical(record)
        assert result.category == "fire", (
            f"Real fixture details.cat='fire' should yield category='fire', got {result.category!r}"
        )

    def test_event_from_details_name(self) -> None:
        """event = details.name (human-readable), NOT details.type (structured code).

        canonical-data-model §4.3 amendment 2026-05-09: event maps from the
        human-readable name field, not the VTEC/structured type code.
        """
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_with_fields(type="FW.A", name="FIRE WEATHER WATCH")
        result = _to_canonical(record)
        assert result.event == "FIRE WEATHER WATCH", (
            f"Expected event='FIRE WEATHER WATCH' (from details.name), got {result.event!r}"
        )

    def test_source_is_aeris_provider_id(self) -> None:
        """source = 'aeris' (provider_id literal) on all canonical records."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_with_fields()
        result = _to_canonical(record)
        assert result.source == "aeris", (
            f"Expected source='aeris', got {result.source!r}"
        )

    def test_headline_from_details_name(self) -> None:
        """headline = details.name passthrough."""
        from weewx_clearskies_api.providers.alerts.aeris import _to_canonical  # noqa: PLC0415
        record = self._make_record_with_fields(name="FIRE WEATHER WATCH")
        result = _to_canonical(record)
        assert result.headline == "FIRE WEATHER WATCH", (
            f"Expected headline='FIRE WEATHER WATCH', got {result.headline!r}"
        )

    def test_area_desc_from_place_name(self) -> None:
        """areaDesc = place.name passthrough."""
        from weewx_clearskies_api.providers.alerts.aeris import (  # noqa: PLC0415
            _AerisAlertRecord,
            _to_canonical,
        )
        record_data = {
            "id": "test-area-001",
            "details": {"type": "WI.Y", "name": "Wind Advisory", "priority": 2, "body": "Test."},
            "timestamps": {"issuedISO": "2026-05-09T10:00:00-05:00", "expiresISO": None},
            "place": {"name": "king county", "state": "wa", "country": "us"},
        }
        record = _AerisAlertRecord.model_validate(record_data)
        result = _to_canonical(record)
        assert result.areaDesc == "king county", (
            f"Expected areaDesc='king county', got {result.areaDesc!r}"
        )


# ===========================================================================
# 6. Wire-shape Pydantic validation
# ===========================================================================


class TestAerisWireShapePydantic:
    """Wire-shape models validate correctly against the real fixture shapes."""

    def test_real_fixture_envelope_loads_cleanly(self) -> None:
        """alerts.json fixture envelope (success=true, 1 alert) loads via _AerisEnvelope."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisEnvelope  # noqa: PLC0415
        raw = _load_fixture("alerts.json")
        envelope = _AerisEnvelope.model_validate(raw)
        assert envelope.success is True
        assert envelope.error is None
        assert len(envelope.response) == 1

    def test_real_fixture_has_expected_alert_fields(self) -> None:
        """Real fixture first response has id, details.name, details.type, timestamps.issuedISO."""
        fixture = _load_fixture("alerts.json")
        first = fixture["response"][0]
        assert first["id"] == "6a000faac27d070dad868226"
        assert first["details"]["name"] == "FIRE WEATHER WATCH"
        assert first["details"]["type"] == "FW.A"
        assert first["timestamps"]["issuedISO"] == "2026-05-09T23:54:00-05:00"

    def test_real_fixture_urgency_certainty_absent(self) -> None:
        """Real fixture details has no urgency/certainty fields (PARTIAL-DOMAIN per amended §4.3)."""
        fixture = _load_fixture("alerts.json")
        details = fixture["response"][0]["details"]
        assert "urgency" not in details, (
            "urgency should be absent from real Aeris alert wire shape (not a documented response field)"
        )
        assert "certainty" not in details, (
            "certainty should be absent from real Aeris alert wire shape (not a documented response field)"
        )

    def test_real_fixture_has_cat_field_carrying_category(self) -> None:
        """Real wire uses 'cat' for the category field (canonical §4.3 amended 2026-05-09).

        canonical-data-model §4.3 originally mapped category=details.category but the
        real wire field name is 'cat'. Amendment routes category=details.cat.
        """
        fixture = _load_fixture("alerts.json")
        details = fixture["response"][0]["details"]
        assert "cat" in details, "Real fixture must have 'cat' field"
        assert details["cat"] == "fire"
        assert "category" not in details, "Real wire uses 'cat', not 'category'"

    def test_real_fixture_emergency_is_boolean_false(self) -> None:
        """Real fixture details.emergency = False (boolean).

        The wire-shape model now declares emergency: bool | str | None; boolean
        False loads cleanly. senderName logic uses isinstance(..., str) to skip
        boolean values and fall through to place.name fallback.
        """
        fixture = _load_fixture("alerts.json")
        details = fixture["response"][0]["details"]
        assert "emergency" in details
        assert details["emergency"] is False  # boolean, not null or string

    def test_record_loads_without_emergency_field(self) -> None:
        """_AerisAlertRecord loads cleanly when emergency field is absent (not boolean)."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisAlertRecord  # noqa: PLC0415
        record_data = {
            "id": "test-clean-001",
            "details": {
                "type": "WI.Y",
                "name": "Wind Advisory",
                "priority": 2,
                "body": "Strong winds.",
                # emergency field absent — this is valid for str|None default=None
            },
            "timestamps": {
                "issuedISO": "2026-05-09T10:00:00-05:00",
                "expiresISO": "2026-05-09T22:00:00-05:00",
            },
            "place": {"name": "king", "state": "wa", "country": "us"},
        }
        record = _AerisAlertRecord.model_validate(record_data)
        assert record.id == "test-clean-001"
        assert record.details.emergency is None  # absent → default None

    def test_missing_required_id_raises_validation_error(self) -> None:
        """Missing required 'id' field on alert record raises ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.alerts.aeris import _AerisAlertRecord  # noqa: PLC0415
        with pytest.raises(ValidationError):
            _AerisAlertRecord.model_validate({
                "details": {"type": "WI.Y", "name": "Wind Advisory"},
                "timestamps": {"issuedISO": "2026-05-09T10:00:00-05:00"},
            })

    def test_extra_fields_in_alert_record_ignored(self) -> None:
        """Unknown extra fields in alert record are silently ignored."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisAlertRecord  # noqa: PLC0415
        record = _AerisAlertRecord.model_validate({
            "id": "test-extra-001",
            "FUTURE_AERIS_FIELD": "ignored",
            "details": {
                "type": "WI.Y",
                "name": "Wind Advisory",
                "priority": 2,
                "FUTURE_DETAIL_FIELD": "also_ignored",
            },
            "timestamps": {
                "issuedISO": "2026-05-09T10:00:00-05:00",
                "FUTURE_TIMESTAMP_FIELD": "ignored_too",
            },
        })
        assert record.id == "test-extra-001"

    def test_envelope_success_false_from_401_fixture(self) -> None:
        """alerts_error_401.json envelope has success=false, error.code=invalid_credentials."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisEnvelope  # noqa: PLC0415
        raw = _load_fixture("alerts_error_401.json")
        env = _AerisEnvelope.model_validate(raw)
        assert env.success is False
        assert env.error is not None
        assert env.error["code"] == "invalid_credentials"
        assert env.response == []

    def test_envelope_success_false_from_429_fixture(self) -> None:
        """alerts_error_429.json envelope has success=false, error.code=maxhits_min."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisEnvelope  # noqa: PLC0415
        raw = _load_fixture("alerts_error_429.json")
        env = _AerisEnvelope.model_validate(raw)
        assert env.success is False
        assert env.error is not None
        assert env.error["code"] == "maxhits_min"

    def test_envelope_warn_location_fixture(self) -> None:
        """alerts_warn_invalid_location.json has success=true, error.code=warn_location, response=[]."""
        from weewx_clearskies_api.providers.alerts.aeris import _AerisEnvelope  # noqa: PLC0415
        raw = _load_fixture("alerts_warn_invalid_location.json")
        env = _AerisEnvelope.model_validate(raw)
        assert env.success is True
        assert env.error is not None
        assert env.error["code"] == "warn_location"
        assert env.response == []

    def test_real_fixture_with_boolean_emergency_succeeds_end_to_end(self) -> None:
        """Real fixture (emergency=False boolean) flows through fetch() without ValidationError.

        Pre-amendment _AerisAlertDetails declared `emergency: str | None`; real wire's boolean
        false triggered ValidationError → ProviderProtocolError. Post-amendment
        (`bool | str | None`), the real fixture parses cleanly. senderName falls back to
        `place.name` because the isinstance(..., str) check skips the boolean.

        End-to-end coverage complementing the model-level test in TestAerisSenderNameDisjunction.
        """
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        # Use the real fixture shape including emergency=False
        real_fixture = _load_fixture("alerts.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=real_fixture)
            )
            # After amendment: no exception; returns list with 1 AlertRecord
            records = aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )
        assert len(records) == 1, f"Expected 1 record from real fixture, got {len(records)}"
        # senderName falls back to place.name ("valley") because emergency=False is falsy
        assert records[0].senderName == "valley", (
            f"Expected senderName='valley', got {records[0].senderName!r}"
        )


# ===========================================================================
# 7. Cache hit/miss — fetch() with respx-mocked HTTP
# ===========================================================================


class TestFetchCacheMissAndHit:
    """fetch() — cache miss makes outbound call; cache hit returns without call."""

    def _make_valid_alert_response(self) -> dict[str, Any]:
        """Build a valid Aeris envelope with one alert (no boolean emergency)."""
        return {
            "success": True,
            "error": None,
            "response": [
                {
                    "id": "cache-test-alert-001",
                    "dataSource": "noaa_nws",
                    "active": True,
                    "details": {
                        "type": "WI.Y",
                        "name": "Wind Advisory",
                        "priority": 2,
                        "color": "AAAAAA",
                        "body": "Wind advisory in effect.",
                        # emergency absent — avoids bool validation issue
                    },
                    "timestamps": {
                        "issued": 1778388840,
                        "issuedISO": "2026-05-09T10:00:00-05:00",
                        "expires": 1778554800,
                        "expiresISO": "2026-05-09T22:00:00-05:00",
                    },
                    "place": {"name": "king", "state": "wa", "country": "us"},
                }
            ],
        }

    def test_cache_miss_makes_outbound_call_and_returns_records(self) -> None:
        """Cache miss: HTTP call made; canonical records returned."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            records = aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )
            assert mock.calls.call_count == 1

        assert len(records) == 1
        assert records[0].source == "aeris"
        assert records[0].event == "Wind Advisory"  # event = details.name (amended §4.3)

    def test_cache_hit_returns_records_without_outbound_call(self) -> None:
        """Cache hit: no HTTP call made; cached records returned."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        # Prime cache with first call
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=alerts_data)
            )
            aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )

        # Second call — should hit cache with zero HTTP calls
        with respx.mock(assert_all_called=False) as mock2:
            records = aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )
            assert mock2.calls.call_count == 0  # No calls on cache hit

        assert len(records) == 1
        assert records[0].source == "aeris"

    def test_empty_response_cached_and_returns_empty_list(self) -> None:
        """Empty response[] (no active alerts) → empty list; empty list cached."""
        _reset_provider_state()
        empty_data = {"success": True, "error": None, "response": []}
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=empty_data)
            )
            records = aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )
            assert mock.calls.call_count == 1

        assert records == []
        # Cache was populated with empty list
        from weewx_clearskies_api.providers.alerts.aeris import _build_cache_key  # noqa: PLC0415
        cached = get_cache().get(_build_cache_key(_LAT, _LON))
        assert cached == []

    def test_cached_records_round_trip_through_model_dump_validate(self) -> None:
        """Records cached as list[dict] and reconstructed via model_validate on hit."""
        _reset_provider_state()
        alerts_data = self._make_valid_alert_response()
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False):
            # First fetch — populates cache
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_AERIS_ALERTS_URL).mock(
                    return_value=httpx.Response(200, json=alerts_data)
                )
                records1 = aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )

        # Second fetch — from cache
        with respx.mock(assert_all_called=False):
            records2 = aeris.fetch(
                lat=_LAT, lon=_LON,
                client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
            )

        assert records1[0].id == records2[0].id
        assert records1[0].source == records2[0].source
        assert records1[0].event == records2[0].event


# ===========================================================================
# 8. Credentials missing → KeyInvalid (brief call 8)
# ===========================================================================


class TestFetchMissingCredentials:
    """fetch() raises KeyInvalid immediately when credentials are absent."""

    def test_missing_client_id_raises_key_invalid_before_http(self) -> None:
        """client_id=None → KeyInvalid before any HTTP call."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            with pytest.raises(KeyInvalid):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=None, client_secret=_TEST_CLIENT_SECRET,
                )
            assert mock.calls.call_count == 0

    def test_missing_client_secret_raises_key_invalid_before_http(self) -> None:
        """client_secret=None → KeyInvalid before any HTTP call."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            with pytest.raises(KeyInvalid):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=None,
                )
            assert mock.calls.call_count == 0

    def test_both_credentials_missing_raises_key_invalid(self) -> None:
        """Both None → KeyInvalid before any HTTP call."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            with pytest.raises(KeyInvalid):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=None, client_secret=None,
                )
            assert mock.calls.call_count == 0

    def test_empty_string_client_id_raises_key_invalid(self) -> None:
        """Empty string client_id (falsy) → KeyInvalid."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            with pytest.raises(KeyInvalid):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id="", client_secret=_TEST_CLIENT_SECRET,
                )
            assert mock.calls.call_count == 0


# ===========================================================================
# 9. HTTP error paths
# ===========================================================================


class TestFetchHttpErrorPaths:
    """fetch() translates HTTP errors to canonical exception taxonomy."""

    def test_401_raises_key_invalid_with_status_code_attribute(self) -> None:
        """HTTP 401 → KeyInvalid; exc.status_code == 401 (F2 attribute-dispatch)."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        error_data = _load_fixture("alerts_error_401.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(401, json=error_data)
            )
            with pytest.raises(KeyInvalid) as exc_info:
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
        # F2 carry-forward: dispatch on attribute, not message string
        assert exc_info.value.status_code == 401, (
            f"Expected status_code=401, got {exc_info.value.status_code!r}"
        )

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        error_data = _load_fixture("alerts_error_429.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(429, json=error_data)
            )
            with pytest.raises(QuotaExhausted):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )

    def test_429_retry_after_seconds_not_none_when_header_present(self) -> None:
        """HTTP 429 with Retry-After header → QuotaExhausted.retry_after_seconds is not None.

        3b-4 F1 carry-forward: assert retry_after_seconds propagated through.
        """
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        error_data = _load_fixture("alerts_error_429.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(
                    429,
                    json=error_data,
                    headers={"Retry-After": "60"},
                )
            )
            with pytest.raises(QuotaExhausted) as exc_info:
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )
        assert exc_info.value.retry_after_seconds is not None, (
            "QuotaExhausted.retry_after_seconds must be set when Retry-After header present"
        )

    def test_500_raises_transient_network_error(self) -> None:
        """HTTP 500 → TransientNetworkError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import (
            TransientNetworkError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(500, json={"error": "Internal Server Error"})
            )
            with pytest.raises(TransientNetworkError):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )


# ===========================================================================
# 10. Aeris envelope error paths
# ===========================================================================


class TestFetchEnvelopeErrorPaths:
    """fetch() handles Aeris-level envelope errors correctly."""

    def test_success_false_envelope_raises_provider_protocol_error(self) -> None:
        """Aeris envelope success=false → ProviderProtocolError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        error_envelope = {
            "success": False,
            "error": {"code": "internal_error", "description": "Internal API error"},
            "response": [],
        }

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=error_envelope)
            )
            with pytest.raises(ProviderProtocolError):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )

    def test_warn_location_returns_empty_list_with_warning_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """success=true + warn_location → WARNING log + empty list returned."""
        _reset_provider_state()
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        warn_fixture = _load_fixture("alerts_warn_invalid_location.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=warn_fixture)
            )
            with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.alerts.aeris"):
                records = aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )

        # Empty list returned — NOT an exception
        assert records == [], (
            f"Expected empty list for warn_location, got {records!r}"
        )
        # WARNING log emitted
        assert any("warn_location" in record.message for record in caplog.records), (
            "Expected WARNING log mentioning 'warn_location'"
        )

    def test_pydantic_validation_error_on_record_raises_provider_protocol_error(self) -> None:
        """Pydantic ValidationError on alert record → ProviderProtocolError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415
        # Valid envelope but record missing required 'id' field
        malformed = {
            "success": True,
            "error": None,
            "response": [
                {
                    # id missing intentionally
                    "details": {"type": "WI.Y", "name": "Wind Advisory"},
                    "timestamps": {"issuedISO": "2026-05-09T10:00:00-05:00"},
                }
            ],
        }

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_AERIS_ALERTS_URL).mock(
                return_value=httpx.Response(200, json=malformed)
            )
            with pytest.raises(ProviderProtocolError):
                aeris.fetch(
                    lat=_LAT, lon=_LON,
                    client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET,
                )


# ===========================================================================
# 11. Capability registry
# ===========================================================================


class TestCapabilityRegistry:
    """CAPABILITY declaration and registry wiring."""

    def test_capability_provider_id_is_aeris(self) -> None:
        """CAPABILITY.provider_id = 'aeris'."""
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.provider_id == "aeris"

    def test_capability_domain_is_alerts(self) -> None:
        """CAPABILITY.domain = 'alerts'."""
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.domain == "alerts"

    def test_capability_auth_required_includes_client_id_and_secret(self) -> None:
        """CAPABILITY.auth_required includes 'client_id' and 'client_secret'."""
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        assert "client_id" in CAPABILITY.auth_required, (
            "CAPABILITY.auth_required must include 'client_id'"
        )
        assert "client_secret" in CAPABILITY.auth_required, (
            "CAPABILITY.auth_required must include 'client_secret'"
        )

    def test_capability_supplied_canonical_fields_includes_core_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes id, headline, severity, etc."""
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        for field in ("id", "headline", "description", "severity", "event",
                      "effective", "expires", "senderName", "areaDesc", "source"):
            assert field in CAPABILITY.supplied_canonical_fields, (
                f"CAPABILITY.supplied_canonical_fields missing {field!r}"
            )

    def test_capability_partial_domain_excludes_urgency_and_certainty(self) -> None:
        """CAPABILITY excludes urgency + certainty per PARTIAL-DOMAIN (canonical §4.3 amended 2026-05-09).

        Aeris does not document or return urgency / certainty fields. PARTIAL-DOMAIN per
        L1 rule extension drops them from CAPABILITY (auditor accepts; canonical §4.3 amended).
        category remains in CAPABILITY because Aeris DOES supply it via `details.cat`.
        """
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        assert "urgency" not in CAPABILITY.supplied_canonical_fields, (
            "PARTIAL-DOMAIN: urgency must be excluded from CAPABILITY (Aeris does not supply)"
        )
        assert "certainty" not in CAPABILITY.supplied_canonical_fields, (
            "PARTIAL-DOMAIN: certainty must be excluded from CAPABILITY (Aeris does not supply)"
        )
        assert "category" in CAPABILITY.supplied_canonical_fields, (
            "category MUST be in CAPABILITY — Aeris supplies it via details.cat (canonical §4.3 amended)"
        )

    def test_wire_providers_registers_aeris_alerts_capability(self) -> None:
        """wire_providers([aeris.CAPABILITY]) → registry contains aeris alerts entry."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            wire_providers,
        )
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        aeris_entries = [p for p in registry if p.provider_id == "aeris" and p.domain == "alerts"]
        assert len(aeris_entries) == 1, (
            f"Expected 1 aeris alerts entry in registry, found {len(aeris_entries)}"
        )

    def test_capability_geographic_coverage_is_us_ca_eu(self) -> None:
        """CAPABILITY.geographic_coverage = 'us-ca-eu' per ADR-016 day-1 table."""
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.geographic_coverage == "us-ca-eu"

    def test_capability_default_poll_interval_is_300_seconds(self) -> None:
        """CAPABILITY.default_poll_interval_seconds = 300 per ADR-016 + ADR-017."""
        from weewx_clearskies_api.providers.alerts.aeris import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.default_poll_interval_seconds == 300


# ===========================================================================
# 12. Cache key construction
# ===========================================================================


class TestCacheKeyConstruction:
    """_build_cache_key produces deterministic keys."""

    def test_same_coordinates_produce_same_key(self) -> None:
        """Same lat/lon always produces the same cache key."""
        from weewx_clearskies_api.providers.alerts.aeris import _build_cache_key  # noqa: PLC0415
        key1 = _build_cache_key(47.6062, -122.3321)
        key2 = _build_cache_key(47.6062, -122.3321)
        assert key1 == key2

    def test_different_coordinates_produce_different_keys(self) -> None:
        """Different lat/lon produces different cache keys."""
        from weewx_clearskies_api.providers.alerts.aeris import _build_cache_key  # noqa: PLC0415
        key1 = _build_cache_key(47.6062, -122.3321)
        key2 = _build_cache_key(41.6022, -98.9178)
        assert key1 != key2

    def test_key_is_64_char_hex_string(self) -> None:
        """Cache key is SHA-256 hex digest (64 characters)."""
        from weewx_clearskies_api.providers.alerts.aeris import _build_cache_key  # noqa: PLC0415
        key = _build_cache_key(47.6062, -122.3321)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)
