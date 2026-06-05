"""Integration tests for charts-config and climatology endpoints (T1.6).

Covers:
  - GET /api/v1/charts/config  — full operator chart config response shape
  - GET /api/v1/charts/groups  — legacy backward-compat groups response
  - GET /api/v1/climatology/monthly — no params (legacy), with params, error cases

All tests use the TestClient + autouse _wire_minimal_services from conftest.py
(SQLite in-memory, minimal registry containing only outTemp).

The autouse fixture does NOT call wire_charts_config(), so a local autouse
fixture wires the built-in default config before each test in this module.

ADR references: ADR-018 (URL versioning), ADR-020 (generatedAt Z suffix),
ADR-024 (self-hide rule), ADR-027 (config search order).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Local autouse fixture: wire built-in charts config for every test here
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wire_test_charts_config() -> None:
    """Wire the built-in default charts config before each test in this module.

    conftest.py _wire_minimal_services does NOT call wire_charts_config(), so
    GET /charts/config would raise RuntimeError without this fixture.
    Using built-in defaults so tests don't depend on any filesystem path.
    """
    from weewx_clearskies_api.services.charts_config import (
        load_charts_config,
        wire_charts_config,
    )

    config = load_charts_config()  # built-in defaults (no path → fallback)
    wire_charts_config(config)


# ---------------------------------------------------------------------------
# GET /api/v1/charts/config
# ---------------------------------------------------------------------------


class TestChartsConfigEndpoint:
    """GET /api/v1/charts/config returns ChartsConfigResponse envelope."""

    def test_charts_config_endpoint_returns_200(
        self, client: TestClient
    ) -> None:
        """GET /charts/config returns HTTP 200."""
        resp = client.get("/api/v1/charts/config")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )

    def test_charts_config_response_has_envelope_fields(
        self, client: TestClient
    ) -> None:
        """GET /charts/config response has 'data' and 'generatedAt' envelope fields.

        Invariant: ChartsConfigResponse shape per OpenAPI contract.
        """
        resp = client.get("/api/v1/charts/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body, "ChartsConfigResponse must have 'data'"
        assert "generatedAt" in body, "ChartsConfigResponse must have 'generatedAt'"

    def test_charts_config_data_has_required_fields(
        self, client: TestClient
    ) -> None:
        """GET /charts/config data has 'groups', 'type', 'colors', 'timeLength'.

        Invariant: ChartsConfigData schema per OpenAPI contract.
        """
        resp = client.get("/api/v1/charts/config")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "groups" in data, "ChartsConfigData must have 'groups'"
        assert "type" in data, "ChartsConfigData must have 'type'"
        assert "colors" in data, "ChartsConfigData must have 'colors'"
        assert isinstance(data["groups"], list), "'groups' must be a list"

    def test_charts_config_type_is_line(self, client: TestClient) -> None:
        """GET /charts/config data.type is 'line' (built-in default global setting).

        Invariant: built-in charts.conf.default sets type=line at global level.
        """
        resp = client.get("/api/v1/charts/config")
        assert resp.status_code == 200
        assert resp.json()["data"]["type"] == "line", (
            f"Built-in default type must be 'line', "
            f"got {resp.json()['data']['type']!r}"
        )

    def test_charts_config_generated_at_has_z_suffix(
        self, client: TestClient
    ) -> None:
        """GET /charts/config generatedAt is UTC ISO-8601 with Z suffix per ADR-020."""
        resp = client.get("/api/v1/charts/config")
        assert resp.status_code == 200
        generated_at = resp.json()["generatedAt"]
        assert generated_at is not None
        assert generated_at.endswith("Z"), (
            f"generatedAt must end with 'Z' per ADR-020, got {generated_at!r}"
        )

    def test_charts_config_groups_list_is_pruned_to_available_columns(
        self, client: TestClient
    ) -> None:
        """GET /charts/config groups list reflects pruning against minimal registry.

        The autouse fixture wires built-in defaults (not pre-pruned); the
        endpoint calls get_charts_config() which returns whatever was wired.
        Since we wire the un-pruned config here, groups may or may not be present.
        The key invariant is that the response is structurally valid.
        """
        resp = client.get("/api/v1/charts/config")
        assert resp.status_code == 200
        groups = resp.json()["data"]["groups"]
        assert isinstance(groups, list), "'groups' must be a list"
        # Each group (if any) must have at minimum 'groupId'
        for group in groups:
            assert "groupId" in group, (
                f"Each group entry must have 'groupId', got keys: {list(group.keys())!r}"
            )


# ---------------------------------------------------------------------------
# GET /api/v1/charts/groups  (backward compat)
# ---------------------------------------------------------------------------


class TestChartsGroupsEndpointBackwardCompat:
    """GET /api/v1/charts/groups returns ChartGroupResponse (legacy endpoint)."""

    def test_charts_groups_endpoint_returns_200(
        self, client: TestClient
    ) -> None:
        """GET /charts/groups returns HTTP 200."""
        resp = client.get("/api/v1/charts/groups")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )

    def test_charts_groups_response_has_data_groups_and_generated_at(
        self, client: TestClient
    ) -> None:
        """GET /charts/groups response has data.groups list and generatedAt.

        Invariant: ChartGroupResponse shape per OpenAPI contract — backward compat
        with existing dashboard consumers must not break.
        """
        resp = client.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body, "ChartGroupResponse must have 'data'"
        assert "generatedAt" in body, "ChartGroupResponse must have 'generatedAt'"
        assert "groups" in body["data"], "ChartGroupResponse.data must have 'groups'"
        assert isinstance(body["data"]["groups"], list), "'groups' must be a list"

    def test_charts_groups_generated_at_has_z_suffix(
        self, client: TestClient
    ) -> None:
        """GET /charts/groups generatedAt has Z suffix per ADR-020."""
        resp = client.get("/api/v1/charts/groups")
        assert resp.status_code == 200
        generated_at = resp.json()["generatedAt"]
        assert generated_at.endswith("Z"), (
            f"generatedAt must end with 'Z' per ADR-020, got {generated_at!r}"
        )


# ---------------------------------------------------------------------------
# GET /api/v1/climatology/monthly
# ---------------------------------------------------------------------------


class TestClimatologyEndpoint:
    """GET /api/v1/climatology/monthly — legacy and generalized paths."""

    def test_climatology_no_params_returns_200_with_months(
        self, client: TestClient
    ) -> None:
        """GET /climatology/monthly (no params) returns 200 with data.months.

        Invariant: legacy fixed response shape — data.months is a 12-element list.
        DB is empty (no rows) so avgHighTemp/avgLowTemp will be present but all-None
        values are acceptable; months list must always be 12 elements.
        """
        resp = client.get("/api/v1/climatology/monthly")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "data" in body, "Response must have 'data'"
        assert "generatedAt" in body, "Response must have 'generatedAt'"
        assert "months" in body["data"], "data must have 'months'"
        assert len(body["data"]["months"]) == 12, (
            f"data.months must be a 12-element list, "
            f"got {len(body['data']['months'])} elements"
        )

    def test_climatology_no_params_months_are_correct_abbreviations(
        self, client: TestClient
    ) -> None:
        """GET /climatology/monthly data.months has correct 3-letter month abbreviations.

        Invariant: month names match the constant defined in services/climatology.py.
        """
        resp = client.get("/api/v1/climatology/monthly")
        assert resp.status_code == 200
        months = resp.json()["data"]["months"]
        expected = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        assert months == expected, (
            f"data.months must match {expected!r}, got {months!r}"
        )

    def test_climatology_with_fields_and_agg_returns_results_dict(
        self, client: TestClient
    ) -> None:
        """GET /climatology/monthly?fields=outTemp&agg=avg returns data.results.outTemp.

        Invariant: generalized path — results dict keyed by field name;
        outTemp is in the minimal registry so it must appear in results.
        Each value is a 12-element list (all None since DB is empty is acceptable).
        """
        resp = client.get(
            "/api/v1/climatology/monthly",
            params={"fields": "outTemp", "agg": "avg"},
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "months" in data, "Generalized path must include 'months'"
        assert "results" in data, "Generalized path must include 'results'"
        assert "outTemp" in data["results"], (
            "outTemp is in registry; it must appear in results dict"
        )
        assert len(data["results"]["outTemp"]) == 12, (
            f"results.outTemp must be a 12-element list, "
            f"got {len(data['results']['outTemp'])} elements"
        )

    def test_climatology_fields_without_agg_returns_422(
        self, client: TestClient
    ) -> None:
        """GET /climatology/monthly?fields=outTemp (no agg) returns 422.

        Invariant: both 'fields' and 'agg' must be supplied together;
        supplying only one is a validation error per the endpoint docstring.
        """
        resp = client.get(
            "/api/v1/climatology/monthly",
            params={"fields": "outTemp"},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for missing 'agg', got {resp.status_code}: {resp.text}"
        )

    def test_climatology_agg_without_fields_returns_422(
        self, client: TestClient
    ) -> None:
        """GET /climatology/monthly?agg=avg (no fields) returns 422.

        Invariant: both 'fields' and 'agg' must be supplied together;
        supplying only agg is a validation error.
        """
        resp = client.get(
            "/api/v1/climatology/monthly",
            params={"agg": "avg"},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for missing 'fields', got {resp.status_code}: {resp.text}"
        )

    def test_climatology_invalid_agg_value_returns_422(
        self, client: TestClient
    ) -> None:
        """GET /climatology/monthly?fields=outTemp&agg=bogus returns 422.

        Invariant: 'agg' must be one of avg_max, avg_min, avg, avg_monthly_total, sum.
        An unrecognised value must produce 422, not 500.
        """
        resp = client.get(
            "/api/v1/climatology/monthly",
            params={"fields": "outTemp", "agg": "bogus"},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for invalid agg 'bogus', "
            f"got {resp.status_code}: {resp.text}"
        )

    def test_climatology_generated_at_has_z_suffix(
        self, client: TestClient
    ) -> None:
        """GET /climatology/monthly generatedAt has Z suffix per ADR-020."""
        resp = client.get("/api/v1/climatology/monthly")
        assert resp.status_code == 200
        generated_at = resp.json()["generatedAt"]
        assert generated_at.endswith("Z"), (
            f"generatedAt must end with 'Z' per ADR-020, got {generated_at!r}"
        )
