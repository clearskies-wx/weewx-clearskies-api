"""Client-to-service payload round-trip tests for the T4A.9/T4A.10 per-hour
handoff — Phase 4A, reopened 2026-07-25.

Why this file exists: compute offloading is the LIVE production path
(``surf_compute_host`` is configured on both weewx and librewxr), not a
fallback. A unit test of ``run_pipeline()`` alone — which is all the round's
first pass had — cannot catch a bug where the per-hour handoff never reaches
the wire at all: ``services/compute_client.py`` silently dropped it from the
payload, and ``services/compute_service.py`` silently substituted a
hardcoded ``10.0`` when rebuilding transects server-side. Both sides passed
their own unit tests; the defect only existed at the HTTP boundary between
them. These tests exercise that boundary for real: build the payload with
``compute_client``'s own serialization function, POST it through a real
``TestClient`` to the real ``compute_service`` FastAPI app (real Pydantic
validation, real ``run_pipeline()`` call, real response serialization), and
deserialize the response with ``compute_client``'s own deserialization
function — the exact two functions that silently lost the data before.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import weewx_clearskies_api.services.compute_service as compute_service
from weewx_clearskies_api.services.compute_client import (
    _deserialize_pipeline_result,
    _serialize_transect,
)
from weewx_clearskies_api.services.swan_formats import TransectInfo
from weewx_clearskies_api.services.transect_handoff import L2_REFERENCE_DEPTH_M

_TEST_SECRET = "test-compute-secret"

_BATHY_PROFILE = [
    {"distance_m": float(500 - i * 15), "depth_m": max(1.0, 15.0 - i * 0.5)}
    for i in range(30)
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(compute_service, "_surf_compute_secret", _TEST_SECRET)
    return TestClient(compute_service.app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_SECRET}"}


def _transect(index: int) -> TransectInfo:
    return TransectInfo(
        index=index,
        origin_lat=33.66,
        origin_lon=-118.00,
        bearing_deg=210.0,
        handoff_depth_m=L2_REFERENCE_DEPTH_M,  # setup-time placeholder
        is_structure_affected=False,
        bathymetric_profile=list(_BATHY_PROFILE),
    )


def _base_payload(transects_json: list[dict]) -> dict:
    return {
        "spot_id": "test_spot",
        "specout_data": None,
        "transects": transects_json,
        "tide_level": 0.0,
        "beach_facing": 210.0,
        "gamma": 0.73,
        "cfjon": 0.038,
        "bulk_hs": 1.5,
        "bulk_tp": 12.0,
        "bulk_dir": 210.0,
        "canonical_partitions": None,
    }


def test_nondefault_handoff_survives_the_client_to_service_round_trip(client):
    """The headline regression guard: a non-default, per-transect-varying
    handoff selection sent by the client must come back unchanged on the
    other side of the HTTP boundary — not the old hardcoded 10.0/"L2"."""
    transects = [_transect(0), _transect(1)]
    handoff_by_transect = {0: (1.78, "L3"), 1: (7.12, "L3")}

    payload = _base_payload([
        _serialize_transect(t, i, handoff_by_transect) for i, t in enumerate(transects)
    ])

    # Prove the client actually put it in the payload (this is exactly what
    # was missing before the fix — the payload never carried the field).
    assert payload["transects"][0]["handoff_depth_m"] == 1.78
    assert payload["transects"][0]["handoff_source_level"] == "L3"
    assert payload["transects"][1]["handoff_depth_m"] == 7.12
    assert payload["transects"][1]["handoff_source_level"] == "L3"

    response = client.post(
        "/compute/swelltrack", json=payload, headers=_auth_headers()
    )
    assert response.status_code == 200

    result = _deserialize_pipeline_result(response.json())
    by_index = {tr.transect_index: tr for tr in result.per_transect}

    # Survived the full round trip: neither value is the old hardcoded
    # default (10.0 / a blanket "L2"), and each transect kept ITS OWN value.
    assert by_index[0].handoff_depth_m == 1.78
    assert by_index[0].handoff_source_level == "L3"
    assert by_index[1].handoff_depth_m == 7.12
    assert by_index[1].handoff_source_level == "L3"
    assert by_index[0].handoff_depth_m != 10.0
    assert by_index[1].handoff_depth_m != 10.0


def test_l2_sourced_handoff_also_survives(client):
    """Not just L3 — an L2-sourced (open-beach / no-L3-grid) selection must
    round-trip just as faithfully, since it's the majority case."""
    transects = [_transect(0)]
    handoff_by_transect = {0: (L2_REFERENCE_DEPTH_M, "L2")}

    payload = _base_payload([
        _serialize_transect(t, i, handoff_by_transect) for i, t in enumerate(transects)
    ])
    response = client.post(
        "/compute/swelltrack", json=payload, headers=_auth_headers()
    )
    assert response.status_code == 200

    result = _deserialize_pipeline_result(response.json())
    assert result.per_transect[0].handoff_depth_m == L2_REFERENCE_DEPTH_M
    assert result.per_transect[0].handoff_source_level == "L2"


def test_missing_handoff_fails_loudly_not_silently_to_ten(client, caplog):
    """T4A.9/T4A.10 reopen constraint 3: an older client (or malformed
    payload) that omits handoff_depth_m/handoff_source_level must not
    silently get the old 10.0 literal — the service must log ERROR naming
    what was missing, and use the documented L2 reference fallback (not an
    unexplained magic number)."""
    payload = _base_payload([
        {
            "index": 0,
            "origin_lat": 33.66,
            "origin_lon": -118.00,
            "is_structure_affected": False,
            "bathymetric_profile": _BATHY_PROFILE,
            # handoff_depth_m / handoff_source_level intentionally omitted —
            # simulates a pre-T4A.9 client.
        }
    ])

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/compute/swelltrack", json=payload, headers=_auth_headers()
        )
    assert response.status_code == 200

    assert any(
        "handoff_depth_m" in rec.message and rec.levelname == "ERROR"
        for rec in caplog.records
    )

    result = _deserialize_pipeline_result(response.json())
    # The documented, named fallback constant — not a silent 10.0.
    assert result.per_transect[0].handoff_depth_m == L2_REFERENCE_DEPTH_M
    assert result.per_transect[0].handoff_source_level == "L2"


def test_wire_shape_rejects_unknown_fields(client):
    """extra='forbid' guard: confirms the request model actually validates
    the transect payload shape rather than silently accepting garbage."""
    payload = _base_payload([
        {
            "index": 0,
            "origin_lat": 33.66,
            "origin_lon": -118.00,
            "is_structure_affected": False,
            "bathymetric_profile": [],
            "handoff_depth_m": 5.0,
            "handoff_source_level": "L3",
            "not_a_real_field": 123,
        }
    ])
    response = client.post(
        "/compute/swelltrack", json=payload, headers=_auth_headers()
    )
    assert response.status_code == 422
