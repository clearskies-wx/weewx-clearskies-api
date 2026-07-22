"""HTTP client for the SwellTrack/SurfBeat compute offloading service (T3.2).

Sends computation requests to the remote compute service (T3.1) when
``surf_compute_host`` is configured.  Falls back to in-process execution
on network/auth/server errors.

Auth: ``Authorization: Bearer <SURF_COMPUTE_SECRET>`` on every request.
Secret: ``os.environ.get("SURF_COMPUTE_SECRET", "")``.

Endpoints:
  POST /compute/swelltrack — run the SwellTrack per-partition pipeline.
  POST /compute/surfbeat   — run a SurfBeat IG strip computation.

Timeouts:
  SwellTrack: 60 s (expected < 500 ms per timestep).
  SurfBeat:  120 s (expected ~30 s per strip run).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import numpy as np

from weewx_clearskies_api.services.surf_1d_analytical import BreakPoint
from weewx_clearskies_api.services.surf_1d_pipeline import (
    PartitionBreakInfo,
    PartitionBreakResult,
    PipelineResult,
    TransectResult,
)
from weewx_clearskies_api.services.surfbeat_runner import SurfBeatResult
from weewx_clearskies_api.services.swan_formats import TransectInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

_SWELLTRACK_TIMEOUT_S: float = 60.0
_SURFBEAT_TIMEOUT_S: float = 120.0


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ComputeServiceError(Exception):
    """Raised when the compute service is unreachable or returns an error."""


# ---------------------------------------------------------------------------
# Serialization helpers — Python/numpy → JSON
# ---------------------------------------------------------------------------


def _serialize_transect(t: TransectInfo) -> dict[str, Any]:
    """Serialize a TransectInfo to the dict shape the compute service expects."""
    return {
        "index": t.index,
        "origin_lat": float(t.origin_lat),
        "origin_lon": float(t.origin_lon),
        "is_structure_affected": bool(t.is_structure_affected),
        "bathymetric_profile": list(t.bathymetric_profile),
    }


# ---------------------------------------------------------------------------
# Deserialization helpers — JSON → Python/numpy
# ---------------------------------------------------------------------------


def _deserialize_break_point(d: dict[str, Any]) -> BreakPoint:
    return BreakPoint(
        distance_m=float(d["distance_m"]),
        depth_m=float(d["depth_m"]),
        hs_m=float(d["hs_m"]),
        breaker_type=str(d["breaker_type"]),
        iribarren=float(d["iribarren"]),
    )


def _deserialize_partition_break_result(d: dict[str, Any] | None) -> PartitionBreakResult | None:
    if d is None:
        return None
    return PartitionBreakResult(
        partition_index=int(d["partition_index"]),
        break_points=[_deserialize_break_point(bp) for bp in d["break_points"]],
        face_height_m=float(d["face_height_m"]),
        hs_at_break_m=float(d["hs_at_break_m"]),
    )


def _deserialize_transect_result(d: dict[str, Any]) -> TransectResult:
    return TransectResult(
        transect_index=int(d["transect_index"]),
        is_structure_affected=bool(d["is_structure_affected"]),
        hs_total_profile=np.array(d["hs_total_profile"], dtype=float),
        distances=np.array(d["distances"], dtype=float),
        depths=np.array(d["depths"], dtype=float),
        per_partition=[
            _deserialize_partition_break_result(pbr) for pbr in d["per_partition"]
        ],
        best_face_height_m=float(d["best_face_height_m"]),
    )


def _deserialize_partition_break_info(d: dict[str, Any]) -> PartitionBreakInfo:
    return PartitionBreakInfo(
        partition_index=int(d["partition_index"]),
        period_s=float(d["period_s"]),
        direction_deg=float(d["direction_deg"]),
        height_m=float(d["height_m"]),
        classification=str(d["classification"]),
        mean_break_distance_m=(
            float(d["mean_break_distance_m"])
            if d.get("mean_break_distance_m") is not None
            else None
        ),
        mean_face_height_m=(
            float(d["mean_face_height_m"])
            if d.get("mean_face_height_m") is not None
            else None
        ),
        peak_face_height_m=(
            float(d["peak_face_height_m"])
            if d.get("peak_face_height_m") is not None
            else None
        ),
        mean_break_depth_m=(
            float(d["mean_break_depth_m"])
            if d.get("mean_break_depth_m") is not None
            else None
        ),
        dominant_breaker_type=d.get("dominant_breaker_type"),
    )


def _deserialize_pipeline_result(data: dict[str, Any]) -> PipelineResult:
    return PipelineResult(
        best_peak_face_height_m=float(data["best_peak_face_height_m"]),
        spot_average_face_height_m=float(data["spot_average_face_height_m"]),
        peel_angle_deg=(
            float(data["peel_angle_deg"])
            if data.get("peel_angle_deg") is not None
            else None
        ),
        peel_classification=data.get("peel_classification"),
        peel_direction=data.get("peel_direction"),
        transect_count=int(data["transect_count"]),
        open_transect_count=int(data["open_transect_count"]),
        per_transect=[_deserialize_transect_result(tr) for tr in data["per_transect"]],
        per_partition_breaks=[
            _deserialize_partition_break_info(pbi) for pbi in data["per_partition_breaks"]
        ],
        degraded=bool(data["degraded"]),
    )


def _deserialize_surfbeat_result(data: dict[str, Any]) -> SurfBeatResult:
    return SurfBeatResult(
        hs_ig_shoreline=float(data["hs_ig_shoreline"]),
        set_timing_minutes=(
            float(data["set_timing_minutes"])
            if data.get("set_timing_minutes") is not None
            else None
        ),
        set_amplitude_m=(
            float(data["set_amplitude_m"])
            if data.get("set_amplitude_m") is not None
            else None
        ),
        hs_sw_profile=[
            {"distance_m": float(p["distance_m"]), "hs_m": float(p["hs_m"])}
            for p in data["hs_sw_profile"]
        ],
        runtime_ms=float(data["runtime_ms"]),
    )


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _auth_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def remote_swelltrack(
    compute_host: str,
    compute_secret: str,
    verify_tls: bool,
    *,
    specout_data: dict | None,
    transects: list[TransectInfo],
    tide_level: float,
    beach_facing: float,
    gamma: float,
    cfjon: float,
    bulk_hs: float | None = None,
    bulk_tp: float | None = None,
    bulk_dir: float | None = None,
    canonical_partitions: list[dict] | None = None,
) -> PipelineResult:
    """POST to /compute/swelltrack on the remote compute service.

    Serializes TransectInfo objects and numpy arrays to JSON.
    Deserializes the response back into a PipelineResult with numpy arrays.

    Args:
        compute_host: Base URL of the compute service (e.g. "https://librewxr:8770").
        compute_secret: Bearer token shared with the service.
        verify_tls: When False, skip TLS certificate verification (self-signed certs).
        specout_data: Parsed SPECOUT spectrum dict for this timestep, or None.
        transects: List of TransectInfo objects with populated bathymetric profiles.
        tide_level: Tide offset in metres.
        beach_facing: Shore-normal direction in degrees.
        gamma: Depth-limited breaking parameter.
        cfjon: JONSWAP bottom friction coefficient.
        bulk_hs: Fallback significant wave height (m) for T4.5 bulk mode.
        bulk_tp: Fallback peak period (s) for T4.5 bulk mode.
        bulk_dir: Fallback wave direction (degrees) for T4.5 bulk mode.
        canonical_partitions: Deep-water SPECOUT partitions for T4.5b index mapping.

    Returns:
        PipelineResult reconstructed from the remote response.

    Raises:
        ComputeServiceError: On network errors, auth failures, or server errors.
    """
    url = f"{compute_host.rstrip('/')}/compute/swelltrack"
    payload: dict[str, Any] = {
        "specout_data": specout_data,
        "transects": [_serialize_transect(t) for t in transects],
        "tide_level": float(tide_level),
        "beach_facing": float(beach_facing),
        "gamma": float(gamma),
        "cfjon": float(cfjon),
        "bulk_hs": float(bulk_hs) if bulk_hs is not None else None,
        "bulk_tp": float(bulk_tp) if bulk_tp is not None else None,
        "bulk_dir": float(bulk_dir) if bulk_dir is not None else None,
        "canonical_partitions": canonical_partitions,
    }

    try:
        response = httpx.post(
            url,
            json=payload,
            headers=_auth_headers(compute_secret),
            verify=verify_tls,
            timeout=_SWELLTRACK_TIMEOUT_S,
        )
    except httpx.TimeoutException as exc:
        raise ComputeServiceError(
            f"SwellTrack request timed out after {_SWELLTRACK_TIMEOUT_S}s: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise ComputeServiceError(
            f"SwellTrack request failed (network error): {exc}"
        ) from exc

    if response.status_code != 200:
        raise ComputeServiceError(
            f"SwellTrack remote returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        data = response.json()
        return _deserialize_pipeline_result(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ComputeServiceError(
            f"SwellTrack response deserialization failed: {exc}"
        ) from exc


def remote_surfbeat(
    compute_host: str,
    compute_secret: str,
    verify_tls: bool,
    *,
    spot_id: str,
    profile: np.ndarray,
    hs: float,
    tp: float,
    direction: float,
    cfjon: float,
) -> SurfBeatResult:
    """POST to /compute/surfbeat on the remote compute service.

    Serializes the numpy profile array to a nested list.
    Deserializes the response back into a SurfBeatResult dataclass.

    Args:
        compute_host: Base URL of the compute service.
        compute_secret: Bearer token shared with the service.
        verify_tls: When False, skip TLS certificate verification.
        spot_id: Surf spot identifier (for logging and SWAN workdir naming).
        profile: Nx2 numpy array ``[[distance_from_shore_m, depth_m], ...]``.
        hs: Significant wave height at the offshore boundary (m).
        tp: Peak period (s).
        direction: Wave direction (degrees).
        cfjon: JONSWAP bottom friction coefficient.

    Returns:
        SurfBeatResult reconstructed from the remote response.

    Raises:
        ComputeServiceError: On network errors, auth failures, or server errors.
    """
    url = f"{compute_host.rstrip('/')}/compute/surfbeat"
    payload: dict[str, Any] = {
        "spot_id": spot_id,
        "profile": profile.tolist(),
        "hs": float(hs),
        "tp": float(tp),
        "direction": float(direction),
        "cfjon": float(cfjon),
    }

    try:
        response = httpx.post(
            url,
            json=payload,
            headers=_auth_headers(compute_secret),
            verify=verify_tls,
            timeout=_SURFBEAT_TIMEOUT_S,
        )
    except httpx.TimeoutException as exc:
        raise ComputeServiceError(
            f"SurfBeat request timed out after {_SURFBEAT_TIMEOUT_S}s: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise ComputeServiceError(
            f"SurfBeat request failed (network error): {exc}"
        ) from exc

    if response.status_code != 200:
        raise ComputeServiceError(
            f"SurfBeat remote returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        data = response.json()
        return _deserialize_surfbeat_result(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ComputeServiceError(
            f"SurfBeat response deserialization failed: {exc}"
        ) from exc
