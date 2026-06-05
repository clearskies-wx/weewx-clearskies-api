"""Unit tests for AstronomyApiClient — T1.10.

Tests the HTTP client for AstronomyAPI.com without any live network calls.
Mocks are applied with respx (the project's established httpx mock library).

Coverage:
  - Valid lunar eclipse response parsed correctly.
  - Valid solar eclipse response parsed correctly.
  - Auth header is HTTP Basic with base64(app_id:app_secret).
  - Timeout returns empty list (no exception raised).
  - HTTP 500 returns empty list (no exception raised).
  - Malformed JSON returns empty list (no exception raised).
  - Date range > 366 days is split into multiple requests.
  - Credentials are NOT logged in warning messages.

ADR references: ADR-038 (no weewx extensions), ADR-018 (RFC 9457 errors).
"""

from __future__ import annotations

import base64
import logging
from datetime import date

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Sample AstronomyAPI response shapes
# ---------------------------------------------------------------------------

_LUNAR_EVENT = {
    "type": "total_lunar_eclipse",
    "eventHighlights": {
        "penumbralStart": {"date": "2026-09-07T16:30:00.000Z", "altitude": 5.0},
        "partialStart": {"date": "2026-09-07T17:30:00.000Z", "altitude": 15.0},
        "fullStart": {"date": "2026-09-07T18:20:00.000Z", "altitude": 25.0},
        "peak": {"date": "2026-09-07T18:44:10.000Z", "altitude": 30.0},
        "fullEnd": {"date": "2026-09-07T19:08:00.000Z", "altitude": 28.0},
        "partialEnd": {"date": "2026-09-07T19:58:00.000Z", "altitude": 20.0},
        "penumbralEnd": {"date": "2026-09-07T20:58:00.000Z", "altitude": 10.0},
    },
    "extraInfo": {"obscuration": 85.5},
}

_SOLAR_EVENT = {
    "type": "partial_solar_eclipse",
    "eventHighlights": {
        "partialStart": {"date": "2026-08-12T15:00:00.000Z", "altitude": 40.0},
        "totalStart": None,
        "peak": {"date": "2026-08-12T16:00:00.000Z", "altitude": 50.0},
        "totalEnd": None,
        "partialEnd": {"date": "2026-08-12T17:00:00.000Z", "altitude": 35.0},
    },
    "extraInfo": {"obscuration": 60.0},
}


def _lunar_payload(events: list | None = None) -> dict:
    """Build a well-formed AstronomyAPI lunar eclipse response payload."""
    return {
        "data": {
            "rows": [
                {"events": events if events is not None else [_LUNAR_EVENT]}
            ]
        }
    }


def _solar_payload(events: list | None = None) -> dict:
    """Build a well-formed AstronomyAPI solar eclipse response payload."""
    return {
        "data": {
            "rows": [
                {"events": events if events is not None else [_SOLAR_EVENT]}
            ]
        }
    }


_BASE_URL = "https://api.astronomyapi.com/api/v2"
_MOON_URL = f"{_BASE_URL}/bodies/events/moon"
_SUN_URL = f"{_BASE_URL}/bodies/events/sun"

_FROM = date(2026, 1, 1)
_TO = date(2026, 12, 31)


# ---------------------------------------------------------------------------
# Helper: build the client
# ---------------------------------------------------------------------------


def _make_client(app_id: str = "test_id", app_secret: str = "test_secret") -> "AstronomyApiClient":
    from weewx_clearskies_api.services.astronomyapi_client import AstronomyApiClient

    return AstronomyApiClient(app_id=app_id, app_secret=app_secret, timeout_seconds=5)


# ===========================================================================
# 1. Parsing — lunar eclipses
# ===========================================================================


class TestLunarEclipseParsing:
    """Valid lunar eclipse response is parsed into the expected dict shape."""

    def test_lunar_response_returns_list_with_one_entry(self) -> None:
        """A single lunar event in the response yields exactly one parsed dict."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload())
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert len(results) == 1, f"Expected 1 result, got {len(results)}"

    def test_lunar_event_type_is_extracted(self) -> None:
        """Parsed event has 'type' key matching the API's event type field."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload())
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["type"] == "total_lunar_eclipse"

    def test_lunar_date_extracted_from_peak(self) -> None:
        """Parsed 'date' is 'YYYY-MM-DD' taken from the peak contact's date."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload())
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["date"] == "2026-09-07"

    def test_lunar_contact_times_all_seven_fields_present(self) -> None:
        """contactTimes dict contains all 7 LUNAR_CONTACT_FIELDS keys."""
        expected_keys = {
            "penumbralStart", "partialStart", "fullStart", "peak",
            "fullEnd", "partialEnd", "penumbralEnd",
        }
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload())
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        ct = results[0]["contactTimes"]
        assert set(ct.keys()) == expected_keys

    def test_lunar_peak_contact_has_date_and_altitude(self) -> None:
        """contactTimes['peak'] has 'date' and 'altitude' sub-fields."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload())
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        peak = results[0]["contactTimes"]["peak"]
        assert isinstance(peak, dict), "peak contact must be a dict"
        assert "date" in peak
        assert "altitude" in peak
        assert peak["altitude"] == 30.0
        assert peak["date"] == "2026-09-07T18:44:10.000Z"

    def test_lunar_obscuration_extracted_from_extra_info(self) -> None:
        """Parsed 'obscuration' is taken from event's extraInfo.obscuration."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload())
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["obscuration"] == pytest.approx(85.5)

    def test_lunar_absent_contact_field_is_none(self) -> None:
        """A contact field absent from eventHighlights becomes None in contactTimes."""
        # Build an event with no penumbralStart field at all.
        event = {
            "type": "partial_lunar_eclipse",
            "eventHighlights": {
                # penumbralStart deliberately omitted
                "partialStart": {"date": "2026-03-03T10:00:00.000Z", "altitude": 10.0},
                "fullStart": None,
                "peak": {"date": "2026-03-03T11:00:00.000Z", "altitude": 20.0},
                "fullEnd": None,
                "partialEnd": {"date": "2026-03-03T12:00:00.000Z", "altitude": 8.0},
                "penumbralEnd": {"date": "2026-03-03T13:00:00.000Z", "altitude": 2.0},
            },
            "extraInfo": None,
        }
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload(events=[event]))
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["contactTimes"]["penumbralStart"] is None
        assert results[0]["contactTimes"]["fullStart"] is None
        assert results[0]["obscuration"] is None

    def test_multiple_events_all_parsed(self) -> None:
        """Multiple events in a single row are all parsed."""
        events = [_LUNAR_EVENT, dict(_LUNAR_EVENT, type="partial_lunar_eclipse")]
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload(events=events))
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert len(results) == 2


# ===========================================================================
# 2. Parsing — solar eclipses
# ===========================================================================


class TestSolarEclipseParsing:
    """Valid solar eclipse response is parsed into the expected dict shape."""

    def test_solar_response_returns_list_with_one_entry(self) -> None:
        """A single solar event yields exactly one parsed dict."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_SUN_URL).mock(
                return_value=httpx.Response(200, json=_solar_payload())
            )
            with _make_client() as client:
                results = client.get_solar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert len(results) == 1

    def test_solar_event_type_is_extracted(self) -> None:
        """Parsed solar event has 'type' matching the API field."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_SUN_URL).mock(
                return_value=httpx.Response(200, json=_solar_payload())
            )
            with _make_client() as client:
                results = client.get_solar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["type"] == "partial_solar_eclipse"

    def test_solar_date_extracted_from_peak(self) -> None:
        """Solar 'date' is 'YYYY-MM-DD' from peak.date."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_SUN_URL).mock(
                return_value=httpx.Response(200, json=_solar_payload())
            )
            with _make_client() as client:
                results = client.get_solar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["date"] == "2026-08-12"

    def test_solar_contact_times_five_fields_present(self) -> None:
        """contactTimes dict contains all 5 SOLAR_CONTACT_FIELDS keys."""
        expected_keys = {"partialStart", "totalStart", "peak", "totalEnd", "partialEnd"}
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_SUN_URL).mock(
                return_value=httpx.Response(200, json=_solar_payload())
            )
            with _make_client() as client:
                results = client.get_solar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        ct = results[0]["contactTimes"]
        assert set(ct.keys()) == expected_keys

    def test_solar_null_contact_field_is_none(self) -> None:
        """A null contact field (totalStart=None) is represented as None."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_SUN_URL).mock(
                return_value=httpx.Response(200, json=_solar_payload())
            )
            with _make_client() as client:
                results = client.get_solar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["contactTimes"]["totalStart"] is None

    def test_solar_obscuration_extracted(self) -> None:
        """Parsed 'obscuration' is taken from extraInfo.obscuration."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_SUN_URL).mock(
                return_value=httpx.Response(200, json=_solar_payload())
            )
            with _make_client() as client:
                results = client.get_solar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert results[0]["obscuration"] == pytest.approx(60.0)


# ===========================================================================
# 3. Auth header
# ===========================================================================


class TestAuthHeader:
    """HTTP Basic auth header carries base64(app_id:app_secret)."""

    def test_auth_header_is_basic_base64(self) -> None:
        """Request Authorization header is 'Basic <base64(app_id:app_secret)>'."""
        app_id = "my_app_id"
        app_secret = "my_app_secret"
        expected_token = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
        expected_header = f"Basic {expected_token}"

        captured_auth: list[str] = []

        with respx.mock(assert_all_called=False) as mock:
            def _capture(request: httpx.Request) -> httpx.Response:
                captured_auth.append(request.headers.get("authorization", ""))
                return httpx.Response(200, json=_lunar_payload())

            mock.get(_MOON_URL).mock(side_effect=_capture)
            with _make_client(app_id=app_id, app_secret=app_secret) as client:
                client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert len(captured_auth) == 1, "Expected exactly one request to be made"
        assert captured_auth[0] == expected_header, (
            f"Auth header mismatch. Got: {captured_auth[0]!r}, "
            f"Expected: {expected_header!r}"
        )

    def test_auth_header_differs_for_different_credentials(self) -> None:
        """Different app_id/app_secret produce a different Authorization header value."""
        def _get_auth(app_id: str, app_secret: str) -> str:
            captured: list[str] = []
            with respx.mock(assert_all_called=False) as mock:
                def _capture(request: httpx.Request) -> httpx.Response:
                    captured.append(request.headers.get("authorization", ""))
                    return httpx.Response(200, json=_lunar_payload())
                mock.get(_MOON_URL).mock(side_effect=_capture)
                with _make_client(app_id=app_id, app_secret=app_secret) as client:
                    client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)
            return captured[0]

        auth_a = _get_auth("id_a", "secret_a")
        auth_b = _get_auth("id_b", "secret_b")
        assert auth_a != auth_b, "Different credentials must produce different auth headers"


# ===========================================================================
# 4. Error handling — returns empty list on failure
# ===========================================================================


class TestErrorHandling:
    """Client returns [] on timeout, HTTP error, or malformed JSON. Never raises."""

    def test_timeout_returns_empty_list(self) -> None:
        """Timeout during request returns [] without raising."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(side_effect=httpx.TimeoutException("timed out"))
            with _make_client() as client:
                result = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert result == [], f"Expected empty list on timeout, got {result!r}"

    def test_http_500_returns_empty_list(self) -> None:
        """HTTP 500 response returns [] without raising."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with _make_client() as client:
                result = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert result == [], f"Expected empty list on HTTP 500, got {result!r}"

    def test_http_401_returns_empty_list(self) -> None:
        """HTTP 401 (bad credentials) returns [] without raising."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            with _make_client() as client:
                result = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert result == [], f"Expected empty list on HTTP 401, got {result!r}"

    def test_malformed_json_returns_empty_list(self) -> None:
        """Non-JSON response body returns [] without raising."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, text="this is not json {{{")
            )
            with _make_client() as client:
                result = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert result == [], f"Expected empty list on malformed JSON, got {result!r}"

    def test_missing_data_rows_returns_empty_list(self) -> None:
        """Response missing data.rows returns [] without raising."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json={"data": {}})
            )
            with _make_client() as client:
                result = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert result == [], f"Expected empty list on missing data.rows, got {result!r}"

    def test_connect_error_returns_empty_list(self) -> None:
        """Network connection error returns [] without raising."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MOON_URL).mock(side_effect=httpx.ConnectError("refused"))
            with _make_client() as client:
                result = client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        assert result == [], f"Expected empty list on ConnectError, got {result!r}"

    def test_no_exception_raised_on_any_error(self) -> None:
        """Client never raises on error — only returns []."""
        error_scenarios = [
            httpx.TimeoutException("timed out"),
            httpx.ConnectError("refused"),
        ]
        for error in error_scenarios:
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_MOON_URL).mock(side_effect=error)
                with _make_client() as client:
                    # Must not raise:
                    client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)


# ===========================================================================
# 5. Date range splitting (> 366 days → multiple requests)
# ===========================================================================


class TestDateRangeSplitting:
    """Ranges > 366 days are split into multiple ≤366-day sub-requests."""

    def test_range_under_366_days_makes_one_request(self) -> None:
        """A 365-day range (≤366 days) triggers exactly 1 HTTP request."""
        from_date = date(2026, 1, 1)
        to_date = date(2026, 12, 31)  # 365 days

        call_count = 0

        with respx.mock(assert_all_called=False) as mock:
            def _count(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(200, json=_lunar_payload())

            mock.get(_MOON_URL).mock(side_effect=_count)
            with _make_client() as client:
                client.get_lunar_eclipses(42.0, -72.0, 100.0, from_date, to_date)

        assert call_count == 1, (
            f"Expected 1 HTTP request for 365-day range, got {call_count}"
        )

    def test_range_exactly_366_days_makes_one_request(self) -> None:
        """A 366-day range (== _MAX_RANGE_DAYS) triggers exactly 1 HTTP request."""
        from_date = date(2026, 1, 1)
        to_date = date(2027, 1, 1)  # 366 days

        call_count = 0

        with respx.mock(assert_all_called=False) as mock:
            def _count(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(200, json=_lunar_payload())

            mock.get(_MOON_URL).mock(side_effect=_count)
            with _make_client() as client:
                client.get_lunar_eclipses(42.0, -72.0, 100.0, from_date, to_date)

        assert call_count == 1, (
            f"Expected 1 HTTP request for 366-day range, got {call_count}"
        )

    def test_range_over_366_days_makes_multiple_requests(self) -> None:
        """A 730-day range (>366 days) triggers at least 2 HTTP requests."""
        from_date = date(2026, 1, 1)
        to_date = date(2028, 1, 1)  # ~730 days

        call_count = 0

        with respx.mock(assert_all_called=False) as mock:
            def _count(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(200, json=_lunar_payload())

            mock.get(_MOON_URL).mock(side_effect=_count)
            with _make_client() as client:
                client.get_lunar_eclipses(42.0, -72.0, 100.0, from_date, to_date)

        assert call_count >= 2, (
            f"Expected >= 2 HTTP requests for 730-day range, got {call_count}"
        )

    def test_results_from_multiple_chunks_are_concatenated(self) -> None:
        """Results from each chunk are concatenated into one list."""
        from_date = date(2026, 1, 1)
        to_date = date(2028, 1, 1)  # ~730 days → 2 chunks

        with respx.mock(assert_all_called=False) as mock:
            # Each chunk returns one event.
            mock.get(_MOON_URL).mock(
                return_value=httpx.Response(200, json=_lunar_payload())
            )
            with _make_client() as client:
                results = client.get_lunar_eclipses(42.0, -72.0, 100.0, from_date, to_date)

        # 2+ chunks × 1 event each = 2+ results.
        assert len(results) >= 2, (
            f"Expected >= 2 results from chunked requests, got {len(results)}"
        )


# ===========================================================================
# 6. Credentials not logged
# ===========================================================================


class TestCredentialsNotLogged:
    """app_id and app_secret do NOT appear in any logged warning messages."""

    def test_credentials_not_in_timeout_log_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """On timeout, the log warning must NOT contain the app_id or app_secret."""
        app_id = "supersecret_app_id_abc123"
        app_secret = "supersecret_app_secret_xyz789"

        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_MOON_URL).mock(
                    side_effect=httpx.TimeoutException("timed out")
                )
                with _make_client(app_id=app_id, app_secret=app_secret) as client:
                    client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        for record in caplog.records:
            assert app_id not in record.getMessage(), (
                f"app_id appeared in log message: {record.getMessage()!r}"
            )
            assert app_secret not in record.getMessage(), (
                f"app_secret appeared in log message: {record.getMessage()!r}"
            )

    def test_credentials_not_in_http_error_log_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """On HTTP 500, the log warning must NOT contain the app_id or app_secret."""
        app_id = "uniqueid_sentinel_abc"
        app_secret = "uniquesecret_sentinel_xyz"

        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get(_MOON_URL).mock(
                    return_value=httpx.Response(500, text="error")
                )
                with _make_client(app_id=app_id, app_secret=app_secret) as client:
                    client.get_lunar_eclipses(42.0, -72.0, 100.0, _FROM, _TO)

        for record in caplog.records:
            assert app_id not in record.getMessage(), (
                f"app_id appeared in log message: {record.getMessage()!r}"
            )
            assert app_secret not in record.getMessage(), (
                f"app_secret appeared in log message: {record.getMessage()!r}"
            )
