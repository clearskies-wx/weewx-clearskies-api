"""Shared per-timestep SwellTrack pipeline input assembly + invocation.

Pure extraction (no behavior change) of logic that used to live inline in
``endpoints/surf.py``'s per-timestep loop (T3.4/T7.2 QB-based reference-point
selection, T4A.9/T4A.6 handoff selection, and the T3.2/T4.4 remote/in-process
pipeline invocation cascade).

Why this is now a shared module: the 1D (SwellTrack) pipeline is expensive
enough that it is precomputed once per SWAN cycle for every forecast
timestep in ``providers/nearshore/swan.py`` and cached, instead of being
re-run on every ``GET /surf/{location_id}`` request (67-72 timesteps ×
every request -- see PROVIDER-MANUAL §14.15 caching note). The precompute
step in swan.py must build the EXACT same ``run_pipeline()``/
``remote_swelltrack()`` call that the on-demand path in surf.py would have
made for that timestep, or the cached result silently diverges from what
on-demand would have produced. Rather than maintain two independent
implementations that can drift, both call sites share this module.

Nothing here changes what is computed -- only where the code that computes
it lives. See ``endpoints/surf.py``'s per-timestep loop for the (now much
shorter) call site, and ``providers/nearshore/swan.py``'s
``_precompute_swelltrack_for_spot()`` for the precompute call site.
"""

from __future__ import annotations

import logging
from typing import Any

from weewx_clearskies_api.services.compute_client import (
    ComputeServiceError,
    remote_swelltrack as _remote_swelltrack,
)
from weewx_clearskies_api.services.surf_1d_pipeline import (
    PipelineResult,
    run_pipeline as _run_surf_pipeline,
)
from weewx_clearskies_api.services.transect_handoff import (
    HandoffSelection,
    select_hourly_handoff as _select_hourly_handoff,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# T3.4+T7.2: QB-based reference point + break point selection
# ---------------------------------------------------------------------------


def select_reference_point(pts_at_time: list[dict]) -> tuple[dict, list[dict]]:
    """Scan for QB peaks (break points) and select the reference point.

    Pure move from ``endpoints/surf.py`` (previously inline in the
    per-timestep loop). Sorts transect points deepest-to-shallowest
    (offshore -> onshore); a QB peak is a local maximum >= 0.25 (threshold
    for meaningful breaking). Multiple peaks identify outer bar + inner bar
    break zones. If breaks are detected, the reference point is the transect
    point just offshore of the biggest break (highest waveHeight ×
    breakingFraction product); otherwise the reference point is whichever
    transect point is closest to 10 m depth (flat/no-break conditions).

    Returns:
        ``(ref_point, break_points_raw)`` where ``ref_point`` is the raw
        transect-point dict (SWAN TABLE units: meters/seconds/degrees,
        unconverted) and ``break_points_raw`` is a list of
        ``{"distance": ..., "depth": ..., "waveHeight": ...}`` dicts in the
        same raw units. Unit conversion to the operator's display unit is a
        per-request concern and happens in the caller (this function has no
        unit-config context) -- unchanged from the pre-extraction behavior,
        where the conversion call sat immediately after this logic in
        surf.py's loop.
    """
    break_points_raw: list[dict] = []
    _break_src_pts: list[dict] = []  # parallel: full transect pt at each break
    _break_offshore_pts: list[dict | None] = []  # parallel: just-offshore pt per break

    if len(pts_at_time) > 1:
        sorted_pts = sorted(
            pts_at_time,
            key=lambda p: p.get("distanceFromShore") or 0,
            reverse=True,
        )
        for _bp_idx, _bp_pt in enumerate(sorted_pts):
            _qb = _bp_pt.get("breakingFraction")
            if _qb is None or _qb < 0.25:
                continue
            _prev_qb = (
                (sorted_pts[_bp_idx - 1].get("breakingFraction") or 0.0)
                if _bp_idx > 0
                else 0.0
            )
            _next_qb = (
                (sorted_pts[_bp_idx + 1].get("breakingFraction") or 0.0)
                if _bp_idx < len(sorted_pts) - 1
                else 0.0
            )
            if _qb >= _prev_qb and _qb >= _next_qb:
                break_points_raw.append({
                    # T4A.1: model vocabulary (distance/depth) — matches
                    # beach_profile.py's transect/breakPoints field names.
                    # "waveHeight" stays raw (meters) — caller converts.
                    "distance": _bp_pt.get("distanceFromShore"),
                    "depth": _bp_pt.get("depth"),
                    "waveHeight": _bp_pt.get("waveHeight"),
                })
                _break_src_pts.append(_bp_pt)
                _break_offshore_pts.append(
                    sorted_pts[_bp_idx - 1] if _bp_idx > 0 else None
                )

    def _depth_of(pt: dict) -> float:
        d = pt.get("depth")
        return float(d) if d is not None else 9999.0

    ref_point = pts_at_time[0]
    if break_points_raw:
        _biggest_idx = max(
            range(len(break_points_raw)),
            key=lambda i: (_break_src_pts[i].get("waveHeight") or 0)
                          * (_break_src_pts[i].get("breakingFraction") or 0),
        )
        _offshore_ref = _break_offshore_pts[_biggest_idx]
        if _offshore_ref is not None:
            ref_point = _offshore_ref
        elif len(pts_at_time) > 1:
            ref_point = min(pts_at_time, key=lambda pt: abs(_depth_of(pt) - 10.0))
    elif len(pts_at_time) > 1:
        ref_point = min(pts_at_time, key=lambda pt: abs(_depth_of(pt) - 10.0))

    return ref_point, break_points_raw


# ---------------------------------------------------------------------------
# T4A.9/T4A.6 item g: per-hour handoff selection
# ---------------------------------------------------------------------------


def resolve_handoff_by_transect(
    ts_handoff_specout: dict[str, Any],
    spot_transects: list,
    raw_hsig: float | None,
) -> dict[int, tuple[float, str]]:
    """This hour's handoff depth/source, per transect.

    Pure move from ``endpoints/surf.py`` (previously inline in the
    per-timestep loop). The real per-hour selection happens upstream in
    ``swan_runner.py`` (``_select_l3_handoff_spectra()``, which has direct
    access to the L3 CURVE's per-station depths/QB) and is published on the
    SPECOUT entry as ``handoff_depth_m``/``handoff_source_level``. This
    function only reads that published choice — it does not recompute it,
    and it never touches grid geometry. A spot with no L3 grid (or a
    timestep with no published per-hour value) falls back to
    ``select_hourly_handoff()`` with no station data, landing on the same
    L2 reference depth every source in this pipeline agrees on.
    """
    if "handoff_depth_m" in ts_handoff_specout:
        handoff_selection = HandoffSelection(
            handoff_depth_m=ts_handoff_specout["handoff_depth_m"],
            source_level=ts_handoff_specout.get("handoff_source_level", "L3"),
            station_index=None,
            station_depth_m=ts_handoff_specout["handoff_depth_m"],
            clamped=False,
        )
    else:
        handoff_selection = _select_hourly_handoff(
            hs_m=float(raw_hsig) if raw_hsig is not None else 0.0,
            station_depths_m=None,
        )
    return (
        {
            idx: (handoff_selection.handoff_depth_m, handoff_selection.source_level)
            for idx in range(len(spot_transects))
        }
        if spot_transects
        else {}
    )


# ---------------------------------------------------------------------------
# T3.2/T4.4: remote-with-in-process-fallback pipeline invocation
# ---------------------------------------------------------------------------


def compute_pipeline_for_timestep(
    *,
    ts_handoff_specout: dict[str, Any],
    spot_transects: list,
    beach_facing_degrees: float,
    friction_coefficient: float,
    raw_hsig: float | None,
    wave_period_pt: float,
    wave_direction_pt: float,
    canonical_partitions: list[dict] | None,
    handoff_by_transect: dict[int, tuple[float, str]],
    spot_id: str,
    valid_time: str,
    compute_host: str | None,
    compute_secret: str,
    compute_verify_tls: bool,
    log_prefix: str = "surf endpoint",
) -> PipelineResult | None:
    """Run the per-partition 1D pipeline for one timestep, once.

    Pure move from ``endpoints/surf.py`` (previously inline in the
    per-timestep loop): remote compute-offload attempt (when
    ``compute_host`` is configured) with in-process fallback on
    ``ComputeServiceError``, or in-process directly when no compute host is
    configured. Tide level is hardcoded ``0.0`` — unchanged from the
    original: CO-OPS tide is fetched and applied elsewhere in the caller,
    not threaded into this call.

    ``log_prefix`` only changes the leading words of the WARNING log lines
    (e.g. "surf endpoint" vs. "SWAN precompute") so both call sites keep
    an accurate log source — it does not change any computed value.

    Returns ``None`` when ``spot_transects`` is empty, or when every
    attempt (remote, and in-process fallback) fails/raises — the caller's
    existing "unavailable" handling is unchanged either way.
    """
    if not spot_transects:
        return None

    pipeline_result: PipelineResult | None = None
    try:
        if compute_host:
            try:
                pipeline_result = _remote_swelltrack(
                    compute_host,
                    compute_secret,
                    compute_verify_tls,
                    spot_id=spot_id,
                    specout_data=ts_handoff_specout,
                    transects=spot_transects,
                    tide_level=0.0,
                    beach_facing=beach_facing_degrees,
                    gamma=0.73,
                    cfjon=friction_coefficient,
                    # T4.5: bulk fallback when SPECOUT freqs/dirs/energy absent.
                    bulk_hs=float(raw_hsig) if raw_hsig is not None else None,
                    bulk_tp=wave_period_pt if wave_period_pt else None,
                    bulk_dir=wave_direction_pt if wave_direction_pt else None,
                    # T4.5b: canonical partitions from deep-water SPECOUT
                    # so per_partition_breaks uses indices the swell card knows.
                    canonical_partitions=canonical_partitions,
                    # T4A.9/T4A.10 fix (2026-07-25, reopened): thread this
                    # hour's per-hour handoff onto the wire — compute
                    # offloading is the live production path, so without
                    # this the remote service computes with a stale
                    # placeholder instead of the real per-hour selection.
                    handoff_by_transect=handoff_by_transect,
                )
            except ComputeServiceError:
                logger.warning(
                    "%s: compute service unavailable for SwellTrack"
                    " (%s @ %s) — falling back to in-process",
                    log_prefix,
                    spot_id,
                    valid_time,
                    exc_info=True,
                )
                pipeline_result = _run_surf_pipeline(
                    specout_data=ts_handoff_specout,
                    transects=spot_transects,
                    tide_level=0.0,
                    beach_facing=beach_facing_degrees,
                    cfjon=friction_coefficient,
                    bulk_hs=float(raw_hsig) if raw_hsig is not None else None,
                    bulk_tp=wave_period_pt if wave_period_pt else None,
                    bulk_dir=wave_direction_pt if wave_direction_pt else None,
                    canonical_partitions=canonical_partitions,
                    handoff_by_transect=handoff_by_transect,
                )
        else:
            pipeline_result = _run_surf_pipeline(
                specout_data=ts_handoff_specout,
                transects=spot_transects,
                tide_level=0.0,
                beach_facing=beach_facing_degrees,
                cfjon=friction_coefficient,
                bulk_hs=float(raw_hsig) if raw_hsig is not None else None,
                bulk_tp=wave_period_pt if wave_period_pt else None,
                bulk_dir=wave_direction_pt if wave_direction_pt else None,
                canonical_partitions=canonical_partitions,
                handoff_by_transect=handoff_by_transect,
            )
    except Exception:
        logger.warning(
            "%s: 1D pipeline raised for %s @ %s — modelStatus will be "
            "'unavailable' for this timestep",
            log_prefix,
            spot_id,
            valid_time,
            exc_info=True,
        )

    return pipeline_result
