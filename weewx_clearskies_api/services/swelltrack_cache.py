"""Trimmed JSON codec for caching a per-timestep SwellTrack ``PipelineResult``.

Used by ``providers/nearshore/swan.py`` to precompute and cache the 1D
(SwellTrack) pipeline result once per forecast timestep per spot, at the end
of a successful SWAN cycle, instead of leaving every ``GET /surf/{spot_id}``
request to re-run the pipeline for all ~67-72 timesteps (the caching change
this module supports — see PROVIDER-MANUAL §14.15 "Precomputed SwellTrack
cache").

Why "trimmed" — three fields are deliberately dropped:

    ``TransectResult.hs_total_profile``, ``.distances``, ``.depths``

Measured (30 transects, ~594 bathymetric profile points each — matching the
PCHIP fine+shoaling+approach zone spacing in PROVIDER-MANUAL §14.15): a full
``PipelineResult`` (all three arrays included, via
``services/compute_service.py``'s existing ``_pipeline_result_to_dict()``)
serializes to **558 KB for one timestep, one spot**. Extrapolated to a full
72-timestep forecast: **~39 MB/spot/cycle**, **~196 MB across 5 spots** —
written to disk every SWAN cycle (~every 6 hours) and shipped over the
separated-service HTTP sync (30 s timeout, PROVIDER-MANUAL §14.15 "Optional
separated service"). The SAME data with those three arrays dropped
serializes to **5.2 KB/timestep — ~0.37 MB/spot/cycle, ~1.84 MB for 5
spots**.

Nothing in ``endpoints/surf.py`` reads those three arrays — it only checks
``PipelineResult.per_transect`` for truthiness (see
``_determine_model_status()``). Only ``endpoints/beach_profile.py`` reads
them (to render the cross-shore wave-height envelope), and only for ONE
timestep per request (whichever is closest to now), not the full forecast.
``beach_profile.py`` was never part of the reported problem (1 pipeline call
per profile-page request, vs. 67-72 calls per ``GET /surf`` request) and is
therefore left on its existing on-demand ``run_pipeline()``/
``remote_swelltrack()`` call — it does NOT read this cache. Do not add a
cache-lookup to beach_profile.py against this trimmed shape: the three
arrays it needs are never present here, so the lookup would structurally
miss on every request — dead code that reads as working caching and isn't.
If full per-transect envelope caching is wanted for beach_profile.py in the
future, it needs its own (much smaller — single timestep) cache entry, not
an extension of this one; re-measure before adding it back.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from weewx_clearskies_api.services.surf_1d_analytical import BreakPoint
from weewx_clearskies_api.services.surf_1d_pipeline import (
    PartitionBreakInfo,
    PartitionBreakResult,
    PipelineResult,
    TransectResult,
)

# ---------------------------------------------------------------------------
# Encode (swan.py precompute -> cache payload)
# ---------------------------------------------------------------------------


def _break_point_to_dict(bp: BreakPoint) -> dict[str, Any]:
    return {
        "distance_m": float(bp.distance_m),
        "depth_m": float(bp.depth_m),
        "hs_m": float(bp.hs_m),
        "breaker_type": bp.breaker_type,
        "iribarren": float(bp.iribarren),
    }


def _partition_break_result_to_dict(pbr: PartitionBreakResult | None) -> dict[str, Any] | None:
    if pbr is None:
        return None
    return {
        "partition_index": pbr.partition_index,
        "break_points": [_break_point_to_dict(bp) for bp in pbr.break_points],
        "face_height_m": float(pbr.face_height_m),
        "hs_at_break_m": float(pbr.hs_at_break_m),
    }


def _transect_result_to_dict(tr: TransectResult) -> dict[str, Any]:
    """Light TransectResult encode — see module docstring for what's dropped and why."""
    return {
        "transect_index": tr.transect_index,
        "is_structure_affected": tr.is_structure_affected,
        "per_partition": [
            _partition_break_result_to_dict(pbr) for pbr in tr.per_partition
        ],
        "best_face_height_m": float(tr.best_face_height_m),
        "handoff_depth_m": float(tr.handoff_depth_m),
        "handoff_source_level": tr.handoff_source_level,
    }


def _partition_break_info_to_dict(pbi: PartitionBreakInfo) -> dict[str, Any]:
    return {
        "partition_index": pbi.partition_index,
        "period_s": float(pbi.period_s),
        "direction_deg": float(pbi.direction_deg),
        "height_m": float(pbi.height_m),
        "classification": pbi.classification,
        "mean_break_distance_m": (
            float(pbi.mean_break_distance_m)
            if pbi.mean_break_distance_m is not None
            else None
        ),
        "mean_face_height_m": (
            float(pbi.mean_face_height_m) if pbi.mean_face_height_m is not None else None
        ),
        "peak_face_height_m": (
            float(pbi.peak_face_height_m) if pbi.peak_face_height_m is not None else None
        ),
        "mean_break_depth_m": (
            float(pbi.mean_break_depth_m) if pbi.mean_break_depth_m is not None else None
        ),
        "dominant_breaker_type": pbi.dominant_breaker_type,
    }


def serialize_pipeline_result_for_cache(result: PipelineResult) -> dict[str, Any]:
    """Encode a PipelineResult for the precomputed swelltrack cache.

    Includes every top-level PipelineResult field (best_peak_face_height_m,
    spot_average_face_height_m, peel_angle_deg, peel_classification,
    peel_direction, transect_count, open_transect_count, degraded,
    per_partition_breaks — confirmed as a real beach_profile.py/surf.py
    consumer, not just the fields named in the original task brief) plus a
    light per_transect (see ``_transect_result_to_dict`` for what's
    omitted).
    """
    return {
        "best_peak_face_height_m": float(result.best_peak_face_height_m),
        "spot_average_face_height_m": float(result.spot_average_face_height_m),
        "peel_angle_deg": (
            float(result.peel_angle_deg) if result.peel_angle_deg is not None else None
        ),
        "peel_classification": result.peel_classification,
        "peel_direction": result.peel_direction,
        "transect_count": result.transect_count,
        "open_transect_count": result.open_transect_count,
        "degraded": result.degraded,
        "per_partition_breaks": [
            _partition_break_info_to_dict(pbi) for pbi in result.per_partition_breaks
        ],
        "per_transect": [_transect_result_to_dict(tr) for tr in result.per_transect],
    }


# ---------------------------------------------------------------------------
# Decode (cache payload -> surf.py cache-hit)
# ---------------------------------------------------------------------------


def _break_point_from_dict(d: dict[str, Any]) -> BreakPoint:
    return BreakPoint(
        distance_m=float(d["distance_m"]),
        depth_m=float(d["depth_m"]),
        hs_m=float(d["hs_m"]),
        breaker_type=str(d["breaker_type"]),
        iribarren=float(d["iribarren"]),
    )


def _partition_break_result_from_dict(d: dict[str, Any] | None) -> PartitionBreakResult | None:
    if d is None:
        return None
    return PartitionBreakResult(
        partition_index=int(d["partition_index"]),
        break_points=[_break_point_from_dict(bp) for bp in d["break_points"]],
        face_height_m=float(d["face_height_m"]),
        hs_at_break_m=float(d["hs_at_break_m"]),
    )


def _transect_result_from_dict(d: dict[str, Any]) -> TransectResult:
    """Reconstructs a TransectResult with EMPTY hs_total_profile/distances/depths.

    Those three arrays are never present in the trimmed cache (see module
    docstring). Any caller that reads them off a cache-hit result gets empty
    arrays back, not an error — surf.py does not read them (only checks
    per_transect truthiness), so this is safe for its use. Do not route
    beach_profile.py through this decoder; it needs the real arrays.
    """
    empty = np.array([], dtype=float)
    return TransectResult(
        transect_index=int(d["transect_index"]),
        is_structure_affected=bool(d["is_structure_affected"]),
        hs_total_profile=empty,
        distances=empty,
        depths=empty,
        per_partition=[
            _partition_break_result_from_dict(pbr) for pbr in d["per_partition"]
        ],
        best_face_height_m=float(d["best_face_height_m"]),
        handoff_depth_m=float(d["handoff_depth_m"]),
        handoff_source_level=str(d["handoff_source_level"]),
    )


def _partition_break_info_from_dict(d: dict[str, Any]) -> PartitionBreakInfo:
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
            float(d["mean_face_height_m"]) if d.get("mean_face_height_m") is not None else None
        ),
        peak_face_height_m=(
            float(d["peak_face_height_m"]) if d.get("peak_face_height_m") is not None else None
        ),
        mean_break_depth_m=(
            float(d["mean_break_depth_m"]) if d.get("mean_break_depth_m") is not None else None
        ),
        dominant_breaker_type=d.get("dominant_breaker_type"),
    )


def deserialize_pipeline_result_from_cache(data: dict[str, Any]) -> PipelineResult:
    """Decode a cached swelltrack entry back into a PipelineResult.

    Raises KeyError/TypeError/ValueError on a malformed entry — callers
    (surf.py) must catch these and treat them the same as a cache miss
    (fall back to the on-demand pipeline call), never let a bad cache entry
    take down the request.
    """
    return PipelineResult(
        best_peak_face_height_m=float(data["best_peak_face_height_m"]),
        spot_average_face_height_m=float(data["spot_average_face_height_m"]),
        peel_angle_deg=(
            float(data["peel_angle_deg"]) if data.get("peel_angle_deg") is not None else None
        ),
        peel_classification=data.get("peel_classification"),
        peel_direction=data.get("peel_direction"),
        transect_count=int(data["transect_count"]),
        open_transect_count=int(data["open_transect_count"]),
        per_transect=[_transect_result_from_dict(tr) for tr in data["per_transect"]],
        per_partition_breaks=[
            _partition_break_info_from_dict(pbi) for pbi in data["per_partition_breaks"]
        ],
        degraded=bool(data["degraded"]),
    )
