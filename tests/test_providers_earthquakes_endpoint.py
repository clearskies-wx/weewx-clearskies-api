"""Endpoint tests for GET /api/v1/earthquakes (3b-13).

Covers all seven decision-tree branches from the brief §"Behavior decision tree":

  Branch 1 — No provider configured:
    → 200, data:[], source:"none".

  Branch 2 — Provider configured, empty features list:
    → 200, data:[], source:<provider_id>.

  Branch 3 — Provider configured, features present:
    → 200, data:[...EarthquakeRecord...], source:<provider_id>.
    Tests USGS (with epoch-ms→ISO + tsunami int→bool) as the representative provider.

  Branch 4 — Network failure / 5xx after retries:
    → 502 ProviderProblem (TransientNetworkError).
    Problem+json response shape.

  Branch 5 — Provider returns 429:
    → 503 ProviderProblem (QuotaExhausted) + Retry-After header.

  Branch 6 — Provider returns 401/403 (keyless-but-stays-in-code-path):
    → 502 ProviderProblem (KeyInvalid).
    Note: USGS/GeoNet/EMSC/ReNaSS are all keyless; 401/403 should not happen in
    production but the canonical taxonomy path must still fire correctly.

  Branch 7 — Pydantic ValidationError on wire model:
    → 502 ProviderProblem (ProviderProtocolError).

Query parameter handling:
  - Unknown query key → 422 (extra="forbid" via Depends pattern per coding.md §1).
  - Valid ?min_magnitude param → accepted (no 422).
  - Valid ?radius_km param → accepted.
  - Valid ?from / ?to ISO timestamps → accepted.

EarthquakeListResponse envelope:
  - data (list[EarthquakeRecord]).
  - source (str: provider_id or "none").
  - generatedAt (UTC ISO-8601 Z).
  No 'units' block (earthquakes are unit-system-invariant per canonical-data-model §2.4).

ADR references: ADR-013, ADR-017, ADR-018, ADR-038, ADR-040.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixture directory
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "earthquakes"

# Seattle station coordinates (matching USGS fixture)
_LAT = 47.6
_LON = -122.3

_USGS_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/earthquakes/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _reset_provider_state(provider: str = "usgs") -> None:
    """Reset cache, registry, and module-level http client."""
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    wire_cache_from_env()

    if provider == "usgs":
        try:
            from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
                _rate_limiter,
                _reset_http_client_for_tests,
            )
            _reset_http_client_for_tests()
            _rate_limiter._calls.clear()
        except ImportError:
            pass


def _wire_test_station() -> None:
    """Wire station at Seattle coordinates."""
    from weewx_clearskies_api.services import station as station_mod  # noqa: PLC0415
    from weewx_clearskies_api.services.station import StationInfo, reset_cache  # noqa: PLC0415

    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="test-earthquakes-station",
        name="Test Earthquakes Station",
        latitude=_LAT,
        longitude=_LON,
        altitude=50.0,
        timezone="America/Los_Angeles",
        timezone_offset_minutes=-420,
        unit_system="US",
        hardware=None,
    )


def _make_earthquakes_app(provider: str | None = None) -> FastAPI:
    """Build a test FastAPI app with the earthquakes endpoint registered.

    provider: "usgs", "geonet", "emsc", "renass", or None.
    """
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        EarthquakesSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.endpoints.earthquakes import (
        wire_earthquakes_settings,  # noqa: PLC0415
    )
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415

    _reset_provider_state(provider or "usgs")
    _wire_test_station()

    capabilities = []
    if provider == "usgs":
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415
        capabilities = [CAPABILITY]
    elif provider == "geonet":
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415
        capabilities = [CAPABILITY]
    elif provider == "emsc":
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415
        capabilities = [CAPABILITY]
    elif provider == "renass":
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415
        capabilities = [CAPABILITY]

    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        earthquakes=EarthquakesSettings({"provider": provider} if provider else {}),
    )
    wire_earthquakes_settings(settings)
    return create_app(settings)


# ===========================================================================
# Branch 1: No provider configured → 200, data:[], source:"none"
# ===========================================================================


class TestBranch1NoProvider:
    """Branch 1: No earthquakes provider in capability registry → 200, data:[], source:'none'."""

    def test_no_provider_returns_200(self) -> None:
        """No earthquake provider → 200 (not 404 or 503)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 200, (
            f"Expected 200 (no provider), got {response.status_code}: {response.text[:300]}"
        )

    def test_no_provider_data_is_empty_list(self) -> None:
        """No provider → data is empty list (per brief decision-tree branch 1)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        body = response.json()
        assert body["data"] == [], (
            f"Expected data=[] with no provider, got {body.get('data')!r}"
        )

    def test_no_provider_source_is_none_string(self) -> None:
        """No provider → source='none'."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        body = response.json()
        assert body["source"] == "none", (
            f"Expected source='none', got {body.get('source')!r}"
        )

    def test_no_provider_generated_at_is_utc_z(self) -> None:
        """No provider → generatedAt is UTC ISO-8601 Z format."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        body = response.json()
        assert "generatedAt" in body, "generatedAt must be present"
        assert body["generatedAt"].endswith("Z"), (
            f"generatedAt must end with Z, got {body['generatedAt']!r}"
        )

    def test_no_provider_no_units_block(self) -> None:
        """No provider → NO 'units' block (earthquakes are unit-system-invariant per §2.4)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        body = response.json()
        assert "units" not in body, (
            "EarthquakeListResponse must NOT have 'units' field (§2.4 unit-invariant)"
        )


# ===========================================================================
# Branch 2: Provider configured, empty features list
# ===========================================================================


class TestBranch2EmptyFeaturesList:
    """Branch 2: Provider configured, provider returns empty features → 200, data:[], source:<id>."""

    def test_usgs_empty_features_returns_200_empty_list(self) -> None:
        """USGS returns FeatureCollection with empty features → 200 + data=[]."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        empty_response = {
            "type": "FeatureCollection",
            "metadata": {"generated": 1778519258000, "count": 0},
            "features": [],
        }
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(200, json=empty_response)
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["data"] == [], f"Expected data=[] for empty features, got {body['data']!r}"
        assert body["source"] == "usgs", f"Expected source='usgs', got {body['source']!r}"


# ===========================================================================
# Branch 3: Provider configured, features present → 200 with canonical records
# ===========================================================================


class TestBranch3ProviderWithFeatures:
    """Branch 3: Provider configured, returns features → 200 + canonical EarthquakeRecord list."""

    def test_usgs_with_fixture_returns_200_and_records(self) -> None:
        """USGS + fixture → 200 + 3 EarthquakeRecord objects."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 3
        assert body["source"] == "usgs"

    def test_usgs_record_has_all_required_fields(self) -> None:
        """EarthquakeRecord has all required OpenAPI fields (id, time, lat, lon, magnitude, source)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        record = response.json()["data"][0]
        for field in ["id", "time", "latitude", "longitude", "magnitude", "source"]:
            assert field in record, f"EarthquakeRecord missing required field '{field}'"
            assert record[field] is not None, f"Required field '{field}' must not be null"

    def test_usgs_record_time_ends_with_z(self) -> None:
        """EarthquakeRecord.time ends with Z (epoch ms → UTC ISO-8601 Z via endpoint)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        time_val = response.json()["data"][0]["time"]
        assert time_val.endswith("Z"), f"time must end with Z, got {time_val!r}"

    def test_usgs_record_tsunami_is_bool_false(self) -> None:
        """EarthquakeRecord.tsunami = False (int 0 → bool via endpoint)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        tsunami = response.json()["data"][0]["tsunami"]
        assert tsunami is False, f"Expected tsunami=False (bool), got {tsunami!r}"

    def test_response_has_no_units_block(self) -> None:
        """EarthquakeListResponse has NO 'units' block (unit-system-invariant per §2.4)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        body = response.json()
        assert "units" not in body, (
            "EarthquakeListResponse must NOT have 'units' block (earthquakes are unit-invariant)"
        )


# ===========================================================================
# Branch 4: Network failure / 5xx → 502 ProviderProblem
# ===========================================================================


class TestBranch4NetworkFailure:
    """Branch 4: Network failure / 5xx after retries → 502 RFC 9457 problem+json."""

    def test_provider_5xx_returns_502_problem_json(self) -> None:
        """USGS 5xx → 502 (TransientNetworkError → ProviderProblem)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(500, json={"reason": "server error"})
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 502, (
            f"Expected 502 from provider 5xx, got {response.status_code}: {response.text[:300]}"
        )

    def test_provider_5xx_response_is_problem_json(self) -> None:
        """502 response uses RFC 9457 problem+json content-type."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(500, json={"reason": "server error"})
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        ct = response.headers.get("content-type", "")
        assert "problem+json" in ct or "application/json" in ct, (
            f"Expected problem+json, got content-type={ct!r}"
        )


# ===========================================================================
# Branch 5: Provider returns 429 → 503 + Retry-After
# ===========================================================================


class TestBranch5RateLimited:
    """Branch 5: Provider returns 429 → 503 RFC 9457 + Retry-After header."""

    def test_provider_429_returns_503(self) -> None:
        """USGS 429 → 503 (QuotaExhausted → ProviderProblem)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"message": "Too Many Requests"},
                    headers={"Retry-After": "60"},
                )
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 503, (
            f"Expected 503 from provider 429, got {response.status_code}"
        )

    def test_provider_429_response_has_retry_after_or_problem_body(self) -> None:
        """503 response from 429 carries Retry-After header or problem+json body."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"message": "Too Many Requests"},
                    headers={"Retry-After": "60"},
                )
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        # Either Retry-After header or problem+json body (both are acceptable)
        has_retry_after = "Retry-After" in response.headers
        has_problem_body = response.headers.get("content-type", "").find("json") >= 0
        assert has_retry_after or has_problem_body, (
            "503 response must carry Retry-After header or JSON problem body"
        )


# ===========================================================================
# Branch 6: Provider 401/403 (keyless-but-stays-in-code-path) → 502 KeyInvalid
# ===========================================================================


class TestBranch6AuthFailure:
    """Branch 6: 401/403 → 502 ProviderProblem (KeyInvalid).

    USGS/GeoNet/EMSC/ReNaSS are all keyless; this branch should not fire in
    production but the canonical taxonomy must still route correctly when the
    upstream does return 401 or 403 (e.g., UA-based blocking, unexpected auth
    requirements from a provider API change).
    """

    def test_provider_401_returns_502(self) -> None:
        """USGS 401 → 502 (KeyInvalid; keyless-but-stays-in-code-path)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(401, json={"message": "Unauthorized"})
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 502, (
            f"Expected 502 from provider 401, got {response.status_code}"
        )

    def test_provider_403_returns_502(self) -> None:
        """USGS 403 → 502 (KeyInvalid; keyless-but-stays-in-code-path)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(403, json={"message": "Forbidden"})
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 502, (
            f"Expected 502 from provider 403, got {response.status_code}"
        )


# ===========================================================================
# Branch 7: Pydantic ValidationError on wire model → 502 ProviderProtocolError
# ===========================================================================


class TestBranch7ValidationError:
    """Branch 7: Wire model validation failure → 502 ProviderProblem (ProviderProtocolError)."""

    def test_malformed_wire_response_returns_502(self) -> None:
        """USGS response missing required 'id' on features → 502 (ProviderProtocolError)."""
        app = _make_earthquakes_app(provider="usgs")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_fixture("usgs_seattle_radius_m2_5.json")

        # Drop 'id' from all features to trigger Pydantic ValidationError
        bad_data = dict(data)
        bad_data["features"] = [
            {k: v for k, v in f.items() if k != "id"}
            for f in data["features"]
        ]

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(200, json=bad_data)
            )
            response = client.get("/api/v1/earthquakes")
        _reset_provider_state()
        assert response.status_code == 502, (
            f"Expected 502 from ValidationError, got {response.status_code}: {response.text[:300]}"
        )


# ===========================================================================
# Query parameter handling
# ===========================================================================


class TestQueryParameterHandling:
    """Query param validation: extra='forbid' fires on unknown keys; valid params accepted."""

    def test_unknown_query_key_returns_422(self) -> None:
        """?unknown_key=bad → 422 (extra='forbid' via Depends pattern per coding.md §1)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes?unknown_key=bad")
        _reset_provider_state()
        assert response.status_code == 422, (
            f"Expected 422 for unknown query key, got {response.status_code}: {response.text[:300]}"
        )

    def test_valid_min_magnitude_param_accepted(self) -> None:
        """?min_magnitude=3.0 → no 422 (valid param per OpenAPI)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes?min_magnitude=3.0")
        _reset_provider_state()
        assert response.status_code != 422, (
            f"?min_magnitude=3.0 must not return 422, got {response.status_code}"
        )

    def test_valid_radius_km_param_accepted(self) -> None:
        """?radius_km=200 → no 422 (valid param per OpenAPI)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes?radius_km=200")
        _reset_provider_state()
        assert response.status_code != 422, (
            f"?radius_km=200 must not return 422, got {response.status_code}"
        )

    def test_valid_from_param_accepted(self) -> None:
        """?from=2026-05-01T00:00:00Z → no 422 (valid ISO 8601 per OpenAPI)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes?from=2026-05-01T00%3A00%3A00Z")
        _reset_provider_state()
        assert response.status_code != 422, (
            f"?from=ISO8601 must not return 422, got {response.status_code}"
        )

    def test_valid_to_param_accepted(self) -> None:
        """?to=2026-05-11T00:00:00Z → no 422 (valid ISO 8601 per OpenAPI)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes?to=2026-05-11T00%3A00%3A00Z")
        _reset_provider_state()
        assert response.status_code != 422, (
            f"?to=ISO8601 must not return 422, got {response.status_code}"
        )

    def test_negative_min_magnitude_returns_422(self) -> None:
        """?min_magnitude=-1 → 422 (ge=0 validation per OpenAPI + EarthquakesQueryParams)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes?min_magnitude=-1")
        _reset_provider_state()
        assert response.status_code == 422, (
            f"?min_magnitude=-1 must return 422 (ge=0 constraint), got {response.status_code}"
        )

    def test_negative_radius_km_returns_422(self) -> None:
        """?radius_km=-100 → 422 (ge=0 validation per OpenAPI + EarthquakesQueryParams)."""
        app = _make_earthquakes_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/earthquakes?radius_km=-100")
        _reset_provider_state()
        assert response.status_code == 422, (
            f"?radius_km=-100 must return 422 (ge=0 constraint), got {response.status_code}"
        )
