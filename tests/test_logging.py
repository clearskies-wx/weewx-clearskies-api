"""Tests for JSON logging and the redaction filter per ADR-029 and §3.4.

Verifies:
  - JsonFormatter produces valid JSON with required fields.
  - RedactionFilter strips Authorization header values.
  - RedactionFilter strips X-Clearskies-Proxy-Auth header values.
  - RedactionFilter strips appid= and client_secret= query parameters.
  - RequestIdFilter injects request_id into records.
"""

from __future__ import annotations

import json
import logging
import io

from weewx_clearskies_api.logging.json_formatter import JsonFormatter
from weewx_clearskies_api.logging.redaction_filter import (
    RedactionFilter,
    RequestIdFilter,
    request_id_var,
)


def _make_logger(name: str = "test") -> tuple[logging.Logger, io.StringIO]:
    """Create a test logger that writes to a StringIO buffer."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger(name)
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log, buf


class TestJsonFormatter:
    """Verify JsonFormatter output structure."""

    def test_required_fields_present(self) -> None:
        """Every log record must have timestamp, level, logger, message."""
        log, buf = _make_logger("test_json_formatter")
        log.info("hello world")
        line = buf.getvalue().strip()
        doc = json.loads(line)
        assert "timestamp" in doc
        assert "level" in doc
        assert "logger" in doc
        assert "message" in doc

    def test_timestamp_has_z_suffix(self) -> None:
        """Timestamp must be ISO-8601 UTC with Z suffix per ADR-029."""
        log, buf = _make_logger("test_ts")
        log.info("ts test")
        doc = json.loads(buf.getvalue().strip())
        assert doc["timestamp"].endswith("Z")

    def test_level_is_string(self) -> None:
        """Level field must be one of the standard level strings."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        log, buf = _make_logger("test_level")
        log.warning("warn test")
        doc = json.loads(buf.getvalue().strip())
        assert doc["level"] in valid_levels

    def test_extra_fields_included(self) -> None:
        """Extra structured fields appear as top-level JSON keys."""
        log, buf = _make_logger("test_extra")
        log.info("structured", extra={"endpoint": "/api/v1/station", "duration_ms": 42})
        doc = json.loads(buf.getvalue().strip())
        assert doc["endpoint"] == "/api/v1/station"
        assert doc["duration_ms"] == 42

    def test_request_id_absent_when_not_set(self) -> None:
        """request_id is absent when not in request context."""
        log, buf = _make_logger("test_no_rid")
        # Ensure context var is cleared.
        token = request_id_var.set("")
        try:
            log.info("no rid")
        finally:
            request_id_var.reset(token)
        doc = json.loads(buf.getvalue().strip())
        assert not doc.get("request_id")


class TestRedactionFilter:
    """Verify RedactionFilter strips secrets from log records."""

    def _make_redacting_logger(self, name: str) -> tuple[logging.Logger, io.StringIO]:
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JsonFormatter())
        log = logging.getLogger(name)
        log.handlers.clear()
        log.addHandler(handler)
        log.addFilter(RedactionFilter())
        log.setLevel(logging.DEBUG)
        log.propagate = False
        return log, buf

    def test_authorization_header_value_redacted(self) -> None:
        """Authorization header value is stripped from log messages."""
        log, buf = self._make_redacting_logger("test_auth_redact")
        log.info("Request received: Authorization: Bearer supersecrettoken")
        doc = json.loads(buf.getvalue().strip())
        assert "supersecrettoken" not in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_proxy_auth_header_redacted(self) -> None:
        """X-Clearskies-Proxy-Auth value is stripped."""
        log, buf = self._make_redacting_logger("test_proxy_redact")
        log.info("X-Clearskies-Proxy-Auth: mysecret123")
        doc = json.loads(buf.getvalue().strip())
        assert "mysecret123" not in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_appid_query_param_redacted(self) -> None:
        """appid= query parameter value is stripped."""
        log, buf = self._make_redacting_logger("test_appid_redact")
        log.info("Calling provider: https://api.example.com?appid=myapikey123")
        doc = json.loads(buf.getvalue().strip())
        assert "myapikey123" not in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_client_secret_query_param_redacted(self) -> None:
        """client_secret= query parameter value is stripped."""
        log, buf = self._make_redacting_logger("test_cs_redact")
        log.info("OAuth call: https://api.example.com?client_secret=topsecret")
        doc = json.loads(buf.getvalue().strip())
        assert "topsecret" not in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_sql_literal_values_redacted(self) -> None:
        """SQL quoted literals are stripped (security baseline §3.4 row 4).

        Verifies that _SQL_LITERAL_RE is wired into _PATTERNS and fires.
        Both single-quoted WHERE literals and VALUES literals must be redacted.
        """
        log, buf = self._make_redacting_logger("test_sql_redact")
        log.info("DB query: SELECT * FROM archive WHERE name = 'alice' AND city = 'london'")
        doc = json.loads(buf.getvalue().strip())
        assert "alice" not in doc["message"]
        assert "london" not in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_sql_values_literal_redacted(self) -> None:
        """SQL VALUES literals are stripped."""
        log, buf = self._make_redacting_logger("test_sql_values_redact")
        log.info("INSERT query: INSERT INTO t (a, b) VALUES (42, 'bob')")
        doc = json.loads(buf.getvalue().strip())
        assert "bob" not in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_redaction_apikey_query_param_at_start(self) -> None:
        """URL with ?apiKey=ABC123 → apiKey=[REDACTED] in log output.

        Covers Wunderground's apiKey= query param credential (F13, 3b-6).
        Pattern: ?apiKey= at start of query string.
        """
        log, buf = self._make_redacting_logger("test_apikey_start_redact")
        log.info("Calling provider: https://api.weather.com/v3/wx/forecast/daily/5day?geocode=47.6062,-122.3321&format=json&apiKey=ABC123")
        doc = json.loads(buf.getvalue().strip())
        assert "ABC123" not in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_redaction_apikey_query_param_mid_string(self) -> None:
        """URL with ?stationId=K1&apiKey=XYZ789 → only apiKey value redacted.

        Covers Wunderground's apiKey= when it appears mid-querystring after another param.
        The stationId value must survive; only the apiKey value is redacted.
        """
        log, buf = self._make_redacting_logger("test_apikey_mid_redact")
        log.info("PWS call: https://api.weather.com/v2/pws/observations/current?stationId=KMAHANOV10&apiKey=XYZ789&format=json")
        doc = json.loads(buf.getvalue().strip())
        assert "XYZ789" not in doc["message"]
        assert "KMAHANOV10" in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_redaction_apikey_followed_by_another_param(self) -> None:
        """URL with ?apiKey=NOPE&format=json → apiKey value redacted but format=json preserved.

        Covers the case where apiKey= is followed by another query parameter.
        The regex must not consume the trailing &format=json into the redacted value.
        """
        log, buf = self._make_redacting_logger("test_apikey_followed_redact")
        log.info("Request: https://api.weather.com/v3/wx/forecast/daily/5day?geocode=47.6,-122.3&units=e&apiKey=NOPE&format=json")
        doc = json.loads(buf.getvalue().strip())
        assert "NOPE" not in doc["message"]
        assert "format=json" in doc["message"]
        assert "[REDACTED]" in doc["message"]

    def test_redaction_iqair_key_query_param_at_question_mark(self) -> None:
        """IQAir ?key=SECRET → key=[REDACTED] (no leak of SECRET; _KEY_RE pattern — 3b-12).

        IQAir AirVisual v2 API uses query-param 'key=' for auth (NOT header X-Key).
        _KEY_RE is a generic 'key=' redactor; added because IQAir is the fourth
        keyed query-param provider on this project. Over-redaction risk accepted per
        LC22 / _SQL_LITERAL_RE precedent.
        """
        log, buf = self._make_redacting_logger("test_iqair_key_start_redact")
        log.info(
            "GET /v2/nearest_city?lat=36.1767&lon=-86.7386&key=SECRET_VALUE_xyz"
        )
        doc = json.loads(buf.getvalue().strip())
        assert "SECRET_VALUE_xyz" not in doc["message"], (
            "IQAir key= value must be redacted from log output"
        )
        assert "[REDACTED]" in doc["message"]

    def test_redaction_iqair_key_param_mid_querystring(self) -> None:
        """key= value redacted when key= appears after other query params.

        Verifies the &key= (ampersand-delimited) form is also captured by _KEY_RE.
        """
        log, buf = self._make_redacting_logger("test_iqair_key_mid_redact")
        log.info(
            "GET https://api.airvisual.com/v2/nearest_city?lat=36.18&lon=-86.74&key=MY_SECRET_KEY"
        )
        doc = json.loads(buf.getvalue().strip())
        assert "MY_SECRET_KEY" not in doc["message"], (
            "key= value in mid-querystring must be redacted"
        )
        assert "[REDACTED]" in doc["message"]

    def test_redaction_iqair_key_param_followed_by_another_param(self) -> None:
        """?key=SECRET&other=value → key=[REDACTED] but other=value preserved.

        The regex must not consume the trailing &other=value into the redacted value.
        """
        log, buf = self._make_redacting_logger("test_iqair_key_followed_redact")
        log.info(
            "GET https://api.airvisual.com/v2/nearest_city?lat=36.18&key=MYSECRET&format=json"
        )
        doc = json.loads(buf.getvalue().strip())
        assert "MYSECRET" not in doc["message"], (
            "key= value must be redacted even when followed by another param"
        )
        assert "format=json" in doc["message"], (
            "Params after key= must NOT be consumed into the redacted value"
        )
        assert "[REDACTED]" in doc["message"]

    def test_existing_appid_pattern_unaffected_by_key_re_addition(self) -> None:
        """_APPID_RE still fires correctly after _KEY_RE is added (no pattern regression)."""
        log, buf = self._make_redacting_logger("test_appid_regression_guard")
        log.info("OWM call: https://api.openweathermap.org/data/2.5/air_pollution?lat=42.3&lon=-71.1&appid=MY_OWM_KEY")
        doc = json.loads(buf.getvalue().strip())
        assert "MY_OWM_KEY" not in doc["message"], (
            "appid= value must still be redacted (no regression from _KEY_RE addition)"
        )
        assert "[REDACTED]" in doc["message"]

    def test_existing_apikey_pattern_unaffected_by_key_re_addition(self) -> None:
        """_APIKEY_RE still fires correctly after _KEY_RE is added (no pattern regression)."""
        log, buf = self._make_redacting_logger("test_apikey_regression_guard")
        log.info("Wunderground call: https://api.weather.com/v2/pws/observations/current?stationId=KMA10&apiKey=MY_WU_KEY")
        doc = json.loads(buf.getvalue().strip())
        assert "MY_WU_KEY" not in doc["message"], (
            "apiKey= value must still be redacted (no regression from _KEY_RE addition)"
        )
        assert "[REDACTED]" in doc["message"]


class TestRequestIdFilter:
    """Verify RequestIdFilter injects the context variable into records."""

    def test_request_id_injected(self) -> None:
        """RequestIdFilter injects the current request_id_var value."""
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JsonFormatter())
        log = logging.getLogger("test_rid_filter")
        log.handlers.clear()
        log.addHandler(handler)
        log.addFilter(RequestIdFilter())
        log.setLevel(logging.DEBUG)
        log.propagate = False

        token = request_id_var.set("test-rid-12345")
        try:
            log.info("with request id")
        finally:
            request_id_var.reset(token)

        doc = json.loads(buf.getvalue().strip())
        assert doc.get("request_id") == "test-rid-12345"
