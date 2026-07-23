"""GET /surf/{location_id}/profile — 1D model beach profile (API-MANUAL §17, ADR-097, T5.2).

Returns the per-partition 1D model's full cross-shore output for the current
forecast timestep (closest to now), replacing the legacy SWAN CURVE response.

Data flow:
  1. Fetch SWAN data for the location (spectral + point forecast).
  2. Find the forecast timestep closest to now.
  3. Compute surf spot transects from the shoreline segment config (same as
     the surf endpoint — both call compute_spot_transects() with the same
     SurfSpotConfig geometry).
  4. Run the per-partition 1D pipeline (run_pipeline()) for that timestep.
  5. Select the requested transect (by index, best-peak, or all transects).
  6. For the selected transect: extract the RSS-combined Hs envelope, break
     points with Iribarren/breaker-type/face-height, and per-partition break
     overlay from the pipeline result.
  7. Run run_1d_analytical() on the dominant partition to get wave shapes,
     surf zone classification (impact/foam/total/reform), and jacking factors.
  8. Apply unit conversion and return the response.

Query parameters:
  transect_index: str (default "best")
    - integer >= 0: zero-based transect index in the transect array
    - "best": open transect with the highest face height (default)
    - "all": array of all transect profiles

Auth level: public (no auth — same as the surf endpoint).

wire_beach_profile_config(settings) stores the parsed MarineConfig module-level.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import UTC, datetime

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.services.compute_client import (
    ComputeServiceError,
    remote_swelltrack as _remote_swelltrack,
)
from weewx_clearskies_api.enrichment.breaker_height import hsig_to_face_height
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock
from weewx_clearskies_api.services.surf_1d_analytical import run_1d_analytical
from weewx_clearskies_api.services.surfbeat_runner import get_cached_surfbeat_result
from weewx_clearskies_api.services.surf_1d_pipeline import (
    PartitionBreakInfo,
    PipelineResult,
    TransectResult,
    run_pipeline as _run_pipeline,
)
from weewx_clearskies_api.services.swan_formats import (
    compute_spot_transects as _compute_spot_transects,
)
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup — see module docstring)
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None


def wire_beach_profile_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by the beach profile endpoint.

    Accepted shapes mirror wire_surf_config / wire_beach_safety_config:
      - MarineConfig instance              -> stored directly.
      - object with a .marine_config attr that is a MarineConfig -> stored.
      - anything else (including None)    -> stored as None. Never raises.
    """
    global _marine_config  # noqa: PLW0603
    if isinstance(settings, MarineConfig):
        _marine_config = settings
    elif isinstance(getattr(settings, "marine_config", None), MarineConfig):
        _marine_config = settings.marine_config  # type: ignore[attr-defined]
    else:
        _marine_config = None


# ---------------------------------------------------------------------------
# Location lookup helpers
# ---------------------------------------------------------------------------


def _find_location(location_id: str) -> MarineLocation | None:
    if _marine_config is None:
        return None
    for loc in _marine_config.locations:
        if loc.id == location_id:
            return loc
    return None


# ---------------------------------------------------------------------------
# Marine unit resolution (API-MANUAL §16 "Marine unit groups")
# ---------------------------------------------------------------------------

_DISPLAY_LABEL: dict[str, str] = {
    "foot": "ft",
    "meter": "m",
}


def _wave_height_unit() -> tuple[str, str]:
    """Return (internal_unit_name, display_symbol) for wave height."""
    height_default = "foot" if get_target_unit() == "US" else "meter"
    unit = get_group_unit("group_wave_height", height_default)
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _distance_unit() -> tuple[str, str]:
    """Return (internal_unit_name, display_symbol) for cross-shore distances and depths."""
    dist_default = "foot" if get_target_unit() == "US" else "meter"
    unit = get_group_unit("group_wave_height", dist_default)
    return (unit, _DISPLAY_LABEL.get(unit, unit))


# ---------------------------------------------------------------------------
# Response serialization helpers
# ---------------------------------------------------------------------------


def _to_camel(snake: str) -> str:
    """Convert snake_case to camelCase (e.g. start_distance → startDistance)."""
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _serialize_surf_zones(surf_zones: object, d_unit: str) -> dict | None:
    """Convert a SurfZones dataclass to a camelCase serializable dict.

    All distance and depth values are converted to the operator's display unit.
    Returns None when surf_zones is None or has no populated fields.
    """
    if surf_zones is None:
        return None

    def _convert_zone(zone: dict | None) -> dict | None:
        if zone is None:
            return None
        result: dict = {}
        for k, v in zone.items():
            camel_k = _to_camel(k)
            if v is None:
                result[camel_k] = None
            elif k in (
                "start_distance",
                "end_distance",
                "width_m",
                "start_depth",
                "end_depth",
            ):
                result[camel_k] = _convert_unit(float(v), "meter", d_unit)
            else:
                result[camel_k] = v
        return result

    out = {
        "impactZone": _convert_zone(surf_zones.impact_zone),
        "foamZone": _convert_zone(surf_zones.foam_zone),
        "totalSurfZone": _convert_zone(surf_zones.total_surf_zone),
        "reformTrough": _convert_zone(surf_zones.reform_trough),
    }
    # Return None rather than an all-null dict on fully unclassified profiles.
    if all(v is None for v in out.values()):
        return None
    return out


def _serialize_partition_breaks(
    per_partition_breaks: list[PartitionBreakInfo],
    h_unit: str,
    d_unit: str,
) -> list[dict]:
    """Serialize PartitionBreakInfo list to camelCase dicts with unit conversion.

    Heights (height_m, mean/peak face heights) are converted to h_unit.
    Distances and depths are converted to d_unit.
    """
    result: list[dict] = []
    for pbi in per_partition_breaks:
        result.append({
            "partitionIndex": pbi.partition_index,
            "periodS": pbi.period_s,
            "directionDeg": pbi.direction_deg,
            "heightM": (
                _convert_unit(pbi.height_m, "meter", h_unit) if pbi.height_m else 0.0
            ),
            "classification": pbi.classification,
            "meanBreakDistanceM": (
                _convert_unit(pbi.mean_break_distance_m, "meter", d_unit)
                if pbi.mean_break_distance_m is not None
                else None
            ),
            "meanFaceHeightM": (
                _convert_unit(pbi.mean_face_height_m, "meter", h_unit)
                if pbi.mean_face_height_m is not None
                else None
            ),
            "peakFaceHeightM": (
                _convert_unit(pbi.peak_face_height_m, "meter", h_unit)
                if pbi.peak_face_height_m is not None
                else None
            ),
            "meanBreakDepthM": (
                _convert_unit(pbi.mean_break_depth_m, "meter", d_unit)
                if pbi.mean_break_depth_m is not None
                else None
            ),
            "dominantBreakerType": pbi.dominant_breaker_type,
        })
    return result


def _blend_hs_profiles(
    swelltrack_profile: list[dict],
    surfbeat_profile: list[dict],
    break_point_distance_m: float,
) -> list[float]:
    """Return blended Hs (m) at each SwellTrack point using SurfBeat in the approach zone.

    Applies a 50 m linear taper centred on the primary break point (T2.3):

    - Offshore of (break_point + 25 m):     100 % SurfBeat Hs
    - Taper zone (break_point ± 25 m):      linear ramp from SurfBeat → SwellTrack
    - Shoreward of (break_point − 25 m):    100 % SwellTrack Hs

    SurfBeat strip stations (≤ 25 m spacing) may be at different locations from the
    SwellTrack CUDEM grid, so ``numpy.interp`` aligns SurfBeat Hs to SwellTrack
    distance points before blending.  Points outside the SurfBeat profile range are
    clamped to the nearest edge value.

    The 50 m taper guarantees a smooth profile with no step discontinuities: at both
    taper edges the weight is 0 or 1, so the blended value matches the pure-source
    value exactly — maximum step is 0 % of local Hs.

    Args:
        swelltrack_profile: SwellTrack RSS-combined Hs at CUDEM grid points.
            Each element is ``{"distance_m": float, "hs_m": float}``.
            May be in any order; function sorts internally.
        surfbeat_profile: SurfBeat TABLE Hs at strip stations
            (``SurfBeatResult.hs_sw_profile``).
            Each element is ``{"distance_m": float, "hs_m": float}``.
        break_point_distance_m: Distance from shore (m) of the primary break
            point on this transect, from the SwellTrack pipeline.

    Returns:
        List of blended Hs (m) in the **same order** as *swelltrack_profile*.
        All values are non-negative (clipped at 0).
    """
    sw_raw_dist = np.array([p["distance_m"] for p in swelltrack_profile], dtype=float)
    sw_raw_hs = np.array([p["hs_m"] for p in swelltrack_profile], dtype=float)

    sb_raw_dist = np.array([p["distance_m"] for p in surfbeat_profile], dtype=float)
    sb_raw_hs = np.array([p["hs_m"] for p in surfbeat_profile], dtype=float)

    # Sort both ascending (shoreward → offshore) for numpy.interp (requires monotone xp).
    sw_order = np.argsort(sw_raw_dist)
    sb_order = np.argsort(sb_raw_dist)

    sw_dist = sw_raw_dist[sw_order]
    sw_hs = sw_raw_hs[sw_order]
    sb_dist = sb_raw_dist[sb_order]
    sb_hs = sb_raw_hs[sb_order]

    # Interpolate SurfBeat Hs to SwellTrack point locations.
    sb_hs_at_sw = np.interp(sw_dist, sb_dist, sb_hs)

    # alpha = weight of SwellTrack (1.0 shoreward, 0.0 offshore).
    # At outer_edge (bp+25m): alpha=0 → 100 % SurfBeat.
    # At inner_edge (bp−25m): alpha=1 → 100 % SwellTrack.
    taper_half_m = 25.0
    outer_edge = float(break_point_distance_m) + taper_half_m
    alpha = np.clip((outer_edge - sw_dist) / (2.0 * taper_half_m), 0.0, 1.0)

    blended_sorted = alpha * sw_hs + (1.0 - alpha) * sb_hs_at_sw
    blended_sorted = np.maximum(blended_sorted, 0.0)

    # Restore original SwellTrack order before returning.
    result = np.empty(len(swelltrack_profile), dtype=float)
    result[sw_order] = blended_sorted
    return result.tolist()


def _build_transect_profile(
    tr: TransectResult,
    pipeline_result: PipelineResult,
    spot_transects: list,
    beach_facing_degrees: float,
    h_unit: str,
    d_unit: str,
    pbi_by_partition: dict[int, PartitionBreakInfo],
    cfjon: float = 0.038,
    surfbeat_hs_profile: list[dict] | None = None,
) -> dict:
    """Build the full 1D model profile response dict for one TransectResult.

    Args:
        tr: Pipeline result for a single transect.
        pipeline_result: Full pipeline output (used for per-partition breaks).
        spot_transects: List of TransectInfo objects in original array order.
            Used to retrieve the bathy profile for the dominant-partition
            run_1d_analytical() call.
        beach_facing_degrees: Shore-normal direction (degrees) for the
            Snell's law refraction call inside run_1d_analytical().
        h_unit: Internal wave height unit name (e.g. "meter", "foot").
        d_unit: Internal distance/depth unit name (e.g. "meter", "foot").
        pbi_by_partition: Mapping from partition_index → PartitionBreakInfo.
            NOTE: partition_index matches the HANDOFF partition order when
            canonical_partitions is not supplied to run_pipeline() (which is
            the case here — beach_profile.py does not pass canonical_partitions).
            If T4.5b canonical mapping is enabled in future, a different lookup
            strategy is needed.
        surfbeat_hs_profile: SurfBeat TABLE Hs at strip stations
            (``SurfBeatResult.hs_sw_profile``), or ``None`` when SurfBeat is
            unavailable.  When provided, approach-zone Hs is blended with a
            50 m linear taper centred on the dominant-partition primary break
            point (T2.3).  Actual wiring from the SurfBeat cache happens in
            Phase 6; this parameter exists so the blend is functional once data
            is provided.

    Returns:
        Serializable dict with camelCase keys including hsEnvelope, breakPoints,
        waveShapes, surfZones, jackingFactors, and transect metadata.
    """
    # --- Dominant partition (needed early for blend derivation and wave shapes) ---
    # Moved ahead of hs_envelope so that the break point distance can be derived
    # before building the profile (T2.3).
    dominant_pbi: PartitionBreakInfo | None = None
    if pipeline_result.per_partition_breaks:
        dominant_pbi = max(
            pipeline_result.per_partition_breaks,
            key=lambda p: p.height_m or 0.0,
        )

    # Derive the primary break point distance on THIS transect from the dominant
    # partition's PartitionBreakResult.  Falls back to None when the dominant
    # partition produced no break points on this transect (degraded run).
    _break_point_distance_m: float | None = None
    if dominant_pbi is not None:
        _dom_idx = dominant_pbi.partition_index
        if _dom_idx < len(tr.per_partition):
            _dom_pbr = tr.per_partition[_dom_idx]
            if _dom_pbr is not None and _dom_pbr.break_points:
                _break_point_distance_m = float(_dom_pbr.break_points[0].distance_m)

    # --- Hs envelope (RSS-combined from pipeline, optionally blended with SurfBeat) ---
    # T2.3: when SurfBeat data and a break point are both available, blend the
    # approach-zone Hs using a 50 m linear taper to correct the ~24 % SwellTrack
    # overestimate offshore of the break.  Falls back to 100 % SwellTrack otherwise.
    _raw_distances_m = [float(d) for d in tr.distances]
    _raw_hs_m = [float(h) for h in tr.hs_total_profile]

    if (
        surfbeat_hs_profile is not None
        and len(surfbeat_hs_profile) >= 2
        and _break_point_distance_m is not None
    ):
        _sw_profile = [
            {"distance_m": d, "hs_m": h}
            for d, h in zip(_raw_distances_m, _raw_hs_m)
        ]
        _blended_hs_m: list[float] = _blend_hs_profiles(
            _sw_profile, surfbeat_hs_profile, _break_point_distance_m
        )
        logger.debug(
            "_build_transect_profile: SurfBeat blend applied "
            "(transect=%d break_point=%.1f m surfbeat_stations=%d)",
            tr.transect_index,
            _break_point_distance_m,
            len(surfbeat_hs_profile),
        )
    else:
        _blended_hs_m = _raw_hs_m

    hs_envelope: list[dict] = [
        {
            "distanceFromShore": _convert_unit(d, "meter", d_unit),
            "depth": _convert_unit(float(z), "meter", d_unit),
            "waveHeight": _convert_unit(h, "meter", h_unit),
        }
        for d, z, h in zip(_raw_distances_m, tr.depths, _blended_hs_m)
    ]

    # --- Break points (from per-partition results on this transect) ---
    # Uses the pre-computed face_height_m from PartitionBreakResult for the
    # primary break point (already applies 1.27× H1/10 per T4.3).
    # Secondary break points (inner bars) compute face height on the fly.
    break_points: list[dict] = []
    for pbr in tr.per_partition:
        if pbr is None or not pbr.break_points:
            continue

        # Match handoff partition to PartitionBreakInfo for period/classification.
        pbi = pbi_by_partition.get(pbr.partition_index)
        partition_info: dict | None = None
        period_s = 10.0  # fallback period for secondary break point face height
        if pbi is not None:
            period_s = pbi.period_s or 10.0
            partition_info = {
                "partitionIndex": pbi.partition_index,
                "periodS": pbi.period_s,
                "directionDeg": pbi.direction_deg,
                "classification": pbi.classification,
                "heightM": (
                    _convert_unit(pbi.height_m, "meter", h_unit) if pbi.height_m else 0.0
                ),
            }

        # Primary break point — face height pre-computed by the pipeline.
        primary_bp = pbr.break_points[0]
        break_points.append({
            "distanceFromShore": _convert_unit(primary_bp.distance_m, "meter", d_unit),
            "depth": _convert_unit(primary_bp.depth_m, "meter", d_unit),
            "waveHeight": _convert_unit(primary_bp.hs_m, "meter", h_unit),
            "faceHeight": _convert_unit(pbr.face_height_m, "meter", h_unit),
            "breakerType": primary_bp.breaker_type,
            "iribarren": round(primary_bp.iribarren, 3),
            "partitionInfo": partition_info,
        })

        # Secondary break points (inner bars, if any).
        for inner_bp in pbr.break_points[1:]:
            inner_face_h = hsig_to_face_height(
                hsig_m=inner_bp.hs_m,
                period_s=period_s,
                source="break_point",
            )
            break_points.append({
                "distanceFromShore": _convert_unit(inner_bp.distance_m, "meter", d_unit),
                "depth": _convert_unit(inner_bp.depth_m, "meter", d_unit),
                "waveHeight": _convert_unit(inner_bp.hs_m, "meter", h_unit),
                "faceHeight": _convert_unit(inner_face_h, "meter", h_unit),
                "breakerType": inner_bp.breaker_type,
                "iribarren": round(inner_bp.iribarren, 3),
                "partitionInfo": partition_info,
            })

    # Sort offshore to shore (descending distance from shore).
    break_points.sort(key=lambda b: b["distanceFromShore"], reverse=True)

    # --- Wave shapes, surf zones, jacking from dominant partition ---
    # The pipeline's per_transect result contains only the RSS-combined Hs
    # profile — wave shapes, surf zone boundaries, and jacking factors are
    # computed by run_1d_analytical() per partition.  We run it here on the
    # dominant partition (highest Hs at handoff) to get these fields.
    # dominant_pbi was already computed at the top of this function (T2.3).
    wave_shapes: list[dict] = []
    surf_zones: dict | None = None
    jacking_factors: list[dict] = []

    if dominant_pbi is not None and dominant_pbi.height_m and dominant_pbi.height_m > 0.0:
        # Get bathy profile for this transect from the TransectInfo.
        t_info = (
            spot_transects[tr.transect_index]
            if tr.transect_index < len(spot_transects)
            else None
        )
        if t_info is not None and t_info.bathymetric_profile:
            try:
                bathy = np.array(
                    [
                        [p["distance_m"], p["depth_m"]]
                        for p in t_info.bathymetric_profile
                    ],
                    dtype=float,
                )
                if bathy.ndim == 2 and bathy.shape[1] >= 2 and bathy.shape[0] >= 2:
                    result_1d = run_1d_analytical(
                        hs=float(dominant_pbi.height_m),
                        tp=float(dominant_pbi.period_s or 10.0),
                        direction=float(dominant_pbi.direction_deg or 180.0),
                        bathy_profile=bathy,
                        tide_level=0.0,
                        gamma=0.73,
                        beach_facing=beach_facing_degrees,
                        cfjon=cfjon,
                    )

                    # Wave shapes — whatever the model provides at selected points.
                    # The dashboard interpolates visually between sample points.
                    for ws in result_1d.wave_shapes:
                        surface_data: list[list[float]] | None = None
                        if ws.surface is not None:
                            surface_data = [
                                [round(phase, 4), round(eta, 4)]
                                for phase, eta in ws.surface
                            ]
                        wave_shapes.append({
                            "distance": _convert_unit(ws.distance_m, "meter", d_unit),
                            "depth": _convert_unit(ws.depth_m, "meter", d_unit),
                            "regime": ws.regime,
                            "surface": surface_data,
                        })

                    # Surf zone classification.
                    surf_zones = _serialize_surf_zones(result_1d.surf_zones, d_unit)

                    # Jacking factors (bar crest Hs / approach Hs).
                    for jf in result_1d.jacking_factors:
                        jacking_factors.append({
                            "barIndex": jf.bar_index,
                            "distance": _convert_unit(jf.distance_m, "meter", d_unit),
                            "factor": round(jf.factor, 3),
                        })

            except Exception:
                logger.warning(
                    "beach_profile: run_1d_analytical failed for transect %d "
                    "(dominant partition Hs=%.2f m, Tp=%.1f s) — "
                    "wave shapes / surf zones / jacking unavailable",
                    tr.transect_index,
                    dominant_pbi.height_m or 0.0,
                    dominant_pbi.period_s or 0.0,
                    exc_info=True,
                )

    # --- Transect metadata ---
    t_info_meta = (
        spot_transects[tr.transect_index]
        if tr.transect_index < len(spot_transects)
        else None
    )

    return {
        "transectIndex": tr.transect_index,
        "isStructureAffected": tr.is_structure_affected,
        "transectBearingDeg": (
            t_info_meta.bearing_deg if t_info_meta is not None else None
        ),
        "transect": hs_envelope,
        "breakPoints": break_points,
        "waveShapes": wave_shapes,
        "surfZones": surf_zones,
        "jackingFactors": jacking_factors,
    }


# ---------------------------------------------------------------------------
# GET /surf/{location_id}/profile — 1D model beach profile
# ---------------------------------------------------------------------------


@router.get(
    "/surf/{location_id}/profile",
    summary="Cross-shore beach profile with 1D model output",
    tags=["Marine"],
)
def get_beach_profile(
    location_id: str,
    transect_index: str = Query(
        default="best",
        description=(
            "Transect selector: non-negative integer index, "
            "'best' (highest-face-height open transect, default), "
            "or 'all' (returns array of all transect profiles)"
        ),
    ),
) -> dict:
    """Return the 1D model's full cross-shore output for the current forecast timestep.

    Hs envelope at 3-5m resolution, H/d=gamma break points with breaker type and
    face height, wave shapes (Stokes / cnoidal / bore), surf zone widths
    (impact / foam / total / reform trough), jacking factors, and per-partition
    break overlay — all for the transect selected by transect_index.

    404 when:
    - Location does not exist or does not have "surf" enabled.
    - No surf spot configuration is present for the location.
    - No SWAN data has been cached for the location.
    - The 1D pipeline is degraded (no bathymetric profiles loaded yet).
    """
    # --- Validate transect_index ---
    _ti_str = str(transect_index).strip().lower()
    _ti_int: int | None = None
    _ti_mode: str  # "best" | "all" | "int"
    if _ti_str in ("best", "all"):
        _ti_mode = _ti_str
    else:
        try:
            _ti_int = int(_ti_str)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid transect_index {transect_index!r}: "
                    "must be a non-negative integer, 'best', or 'all'"
                ),
            )
        if _ti_int < 0:
            raise HTTPException(
                status_code=422,
                detail="transect_index must be >= 0",
            )
        _ti_mode = "int"

    # --- Location lookup ---
    location = _find_location(location_id)
    if location is None or "surf" not in location.activities:
        raise HTTPException(
            status_code=404,
            detail=f"Surf location {location_id!r} not found",
        )

    # --- Surf spot config (required for transect geometry and pipeline call) ---
    spot_config = _marine_config.surf_spots.get(location_id) if _marine_config else None
    if spot_config is None:
        raise HTTPException(
            status_code=404,
            detail=f"No surf spot configuration for location {location_id!r}",
        )

    # --- Unit resolution ---
    wave_height_internal, wave_height_symbol = _wave_height_unit()
    distance_internal, distance_symbol = _distance_unit()

    # --- Compute offloading config (mirrors surf.py) ---
    _compute_host: str | None = _marine_config.surf_compute_host if _marine_config else None
    _compute_secret: str = os.environ.get("SURF_COMPUTE_SECRET", "") if _compute_host else ""
    _compute_verify_tls: bool = (
        _marine_config.surf_compute_verify_tls if _marine_config else True
    )

    # --- SWAN: fetch cached nearshore data ---
    swan_result: dict | None = None
    try:
        from weewx_clearskies_api.providers.nearshore import swan  # noqa: PLC0415

        swan_result = swan.fetch(spot_id=location_id)
    except Exception:
        logger.warning(
            "beach_profile: SWAN fetch failed for %s", location_id, exc_info=True
        )

    if swan_result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SWAN data cached for location {location_id!r}",
        )

    # --- Build timestep→SPECOUT and timestep→points lookups ---
    swan_points: list[dict] = swan_result.get("forecast") or []
    swan_spectral: list[dict] = swan_result.get("spectral") or []

    _handoff_specout_by_time: dict[str, dict] = {}
    for _entry in swan_spectral:
        _t = _entry.get("time", "")
        if _t:
            _handoff_specout_by_time[_t] = _entry

    _points_by_time: dict[str, list[dict]] = defaultdict(list)
    for _p in swan_points:
        _pt_time = (
            _p.get("time") if isinstance(_p, dict) else getattr(_p, "time", "")
        ) or ""
        if _pt_time:
            _pt_dict = _p if isinstance(_p, dict) else _p.model_dump()
            _points_by_time[_pt_time].append(_pt_dict)

    # --- Find closest timestep to now ---
    now = datetime.now(UTC)
    closest_time: str | None = None
    closest_delta: float | None = None

    for time_iso in set(_handoff_specout_by_time) | set(_points_by_time):
        try:
            ts_dt = datetime.fromisoformat(time_iso.replace("Z", "+00:00"))
            delta = abs((ts_dt - now).total_seconds())
            if closest_delta is None or delta < closest_delta:
                closest_delta = delta
                closest_time = time_iso
        except (ValueError, TypeError):
            logger.debug(
                "beach_profile: skipping unparseable timestep key %r for %s",
                time_iso,
                location_id,
            )

    if closest_time is None:
        raise HTTPException(
            status_code=404,
            detail=f"No forecast timesteps found for location {location_id!r}",
        )

    # --- Bulk fallback parameters (from SWAN TABLE point nearest to now) ---
    # Used by run_pipeline() when SPECOUT is unavailable (T4.5 degraded mode).
    _ref_pts = _points_by_time.get(closest_time, [])
    _bulk_hs: float | None = None
    _bulk_tp: float | None = None
    _bulk_dir: float | None = None
    if _ref_pts:
        _rp = _ref_pts[0]
        _raw = _rp.get("waveHeight")
        if _raw is not None:
            _bulk_hs = float(_raw)
        _raw = _rp.get("wavePeriod")
        if _raw is not None:
            _bulk_tp = float(_raw)
        _raw = _rp.get("waveDirection")
        if _raw is not None:
            _bulk_dir = float(_raw)

    # --- Compute transects (same geometry as the surf endpoint) ---
    spot_transects: list = []
    try:
        spot_transects = _compute_spot_transects(
            segment_start_lat=spot_config.segment_start_lat,
            segment_start_lon=spot_config.segment_start_lon,
            segment_end_lat=spot_config.segment_end_lat,
            segment_end_lon=spot_config.segment_end_lon,
            transect_spacing_m=spot_config.transect_spacing_m,
            beach_facing_degrees=spot_config.beach_facing_degrees,
            structures=spot_config.structures or None,
        )
    except Exception:
        logger.warning(
            "beach_profile: compute_spot_transects failed for %s",
            location_id,
            exc_info=True,
        )

    if not spot_transects:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No transects available for location {location_id!r} "
                "— spot not configured or segment has zero length"
            ),
        )

    # --- Run the per-partition 1D pipeline ---
    _ts_specout = _handoff_specout_by_time.get(closest_time) or {}
    pipeline_result: PipelineResult | None = None
    try:
        if _compute_host:
            try:
                pipeline_result = _remote_swelltrack(
                    _compute_host,
                    _compute_secret,
                    _compute_verify_tls,
                    spot_id=location_id,
                    specout_data=_ts_specout,
                    transects=spot_transects,
                    tide_level=0.0,
                    beach_facing=spot_config.beach_facing_degrees,
                    gamma=0.73,
                    cfjon=spot_config.friction_coefficient,
                    bulk_hs=_bulk_hs,
                    bulk_tp=_bulk_tp,
                    bulk_dir=_bulk_dir,
                )
            except ComputeServiceError:
                logger.warning(
                    "beach_profile: compute service unavailable — falling back to in-process",
                    exc_info=True,
                )
                pipeline_result = _run_pipeline(
                    specout_data=_ts_specout,
                    transects=spot_transects,
                    tide_level=0.0,
                    beach_facing=spot_config.beach_facing_degrees,
                    cfjon=spot_config.friction_coefficient,
                    bulk_hs=_bulk_hs,
                    bulk_tp=_bulk_tp,
                    bulk_dir=_bulk_dir,
                )
        else:
            pipeline_result = _run_pipeline(
                specout_data=_ts_specout,
                transects=spot_transects,
                tide_level=0.0,
                beach_facing=spot_config.beach_facing_degrees,
                cfjon=spot_config.friction_coefficient,
                bulk_hs=_bulk_hs,
                bulk_tp=_bulk_tp,
                bulk_dir=_bulk_dir,
            )
    except Exception:
        logger.warning(
            "beach_profile: 1D pipeline raised for %s @ %s",
            location_id,
            closest_time,
            exc_info=True,
        )

    if pipeline_result is None or not pipeline_result.per_transect:
        raise HTTPException(
            status_code=404,
            detail=(
                f"1D model output unavailable for {location_id!r} — "
                "pipeline degraded or bathymetric profiles not yet loaded"
            ),
        )

    # --- Build lookup: partition_index → PartitionBreakInfo ---
    pbi_by_partition: dict[int, PartitionBreakInfo] = {
        pbi.partition_index: pbi
        for pbi in pipeline_result.per_partition_breaks
    }

    # --- Serialize per-partition break overlay (common to all responses) ---
    per_partition_breaks_out = _serialize_partition_breaks(
        pipeline_result.per_partition_breaks,
        wave_height_internal,
        distance_internal,
    )

    # --- Common metadata block ---
    metadata = {
        "axisUnits": {"x": distance_symbol, "y": distance_symbol},
        "verticalDatum": "NAVD88",
        "transectCount": pipeline_result.transect_count,
        "openTransectCount": pipeline_result.open_transect_count,
    }

    now_str = utc_isoformat(now)
    units_block = {
        "distance": distance_symbol,
        "depth": distance_symbol,
        "waveHeight": wave_height_symbol,
    }
    envelope = {
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("surf").model_dump(by_alias=True),
        "units": units_block,
        "generatedAt": now_str,
    }

    all_transect_results: list[TransectResult] = pipeline_result.per_transect

    # Look up cached SurfBeat result for approach-zone Hs blend (F1 fix).
    _sb_cached = get_cached_surfbeat_result(location_id)
    _sb_hs_profile = _sb_cached.hs_sw_profile if _sb_cached is not None else None

    # -----------------------------------------------------------------------
    # transect_index = "all" — return array of all transect profiles
    # -----------------------------------------------------------------------
    if _ti_mode == "all":
        profiles = [
            _build_transect_profile(
                tr,
                pipeline_result,
                spot_transects,
                spot_config.beach_facing_degrees,
                wave_height_internal,
                distance_internal,
                pbi_by_partition,
                cfjon=spot_config.friction_coefficient,
                surfbeat_hs_profile=_sb_hs_profile,
            )
            for tr in all_transect_results
        ]
        return {
            "data": {
                "locationId": location_id,
                "timestep": closest_time,
                "profiles": profiles,
                "perPartitionBreaks": per_partition_breaks_out,
                "metadata": metadata,
            },
            **envelope,
        }

    # -----------------------------------------------------------------------
    # Single-transect responses ("best" or integer index)
    # -----------------------------------------------------------------------
    if _ti_mode == "best":
        open_trs = [
            tr for tr in all_transect_results if not tr.is_structure_affected
        ]
        candidates = open_trs if open_trs else all_transect_results
        selected_tr = max(candidates, key=lambda tr: tr.best_face_height_m)
    else:
        # _ti_mode == "int"
        assert _ti_int is not None  # guaranteed by the parsing block above
        matching = [
            tr for tr in all_transect_results if tr.transect_index == _ti_int
        ]
        if not matching:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Transect index {_ti_int} not found "
                    f"(successful transects: 0–{len(spot_transects) - 1})"
                ),
            )
        selected_tr = matching[0]

    profile = _build_transect_profile(
        selected_tr,
        pipeline_result,
        spot_transects,
        spot_config.beach_facing_degrees,
        wave_height_internal,
        distance_internal,
        pbi_by_partition,
        cfjon=spot_config.friction_coefficient,
        surfbeat_hs_profile=_sb_hs_profile,
    )

    return {
        "data": {
            "locationId": location_id,
            "timestep": closest_time,
            **profile,
            "perPartitionBreaks": per_partition_breaks_out,
            "metadata": metadata,
        },
        **envelope,
    }
