"""Per-partition 1D swell transformation pipeline (Phase 4, T4.2).

Orchestrates the full per-partition, multi-transect wave transformation
pipeline described in SURF-ZONE-MODEL-BRIEF §7.  Replaces the legacy
single-reference-point approach with N_partitions × N_transects independent
1D model runs.

Pipeline (per SURF-ZONE-MODEL-BRIEF §7):
  SPECOUT at handoff
    → decompose_spectrum()         — N swell partitions
    → run_1d_analytical() ×(N×M)  — N partitions × M transects
    → RSS combination per transect — Hs_total = sqrt(sum(Hs_i²))
    → depth-limited saturation     — Hs_total ≤ γd at every point
    → H1/10 at each break point    — face_height = 1.27 × Hs_break
    → aggregate open transects     — best peak, spot average
    → peel angle                   — from break-point spatial variation

Key SURF-FIXIT-LIST resolutions this module enables:
  SURF-11: Per-partition runs — all decomposed components now transform
           independently, none are masked by the dominant peak.
  SURF-22: Face height (H1/10) applied at the 1D model's actual break point,
           not at an arbitrary ~10 m reference depth.
  SURF-23: The 'multiSwell' swell card feeds from the deep-water SPECOUT
           decomposition (done upstream); this module consumes the handoff
           SPECOUT partitions for 1D transformation.

Physics references:
  SURF-ZONE-MODEL-BRIEF §3 Option A — analytical 1D equations
  SURF-ZONE-MODEL-BRIEF §5.2 — K-G is NOT eliminated; better Hs input
  SURF-ZONE-MODEL-BRIEF §5.8 — peel angle from multi-transect break variation
  SURF-ZONE-MODEL-BRIEF §7 — complete data pipeline
  Plan T4.2, T4.3 — per-partition pipeline + K-G fix spec
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from weewx_clearskies_api.enrichment.breaker_height import hsig_to_face_height
from weewx_clearskies_api.services.surf_1d_analytical import (
    Analytical1DResult,
    BreakPoint,
    run_1d_analytical,
)
from weewx_clearskies_api.services.swan_formats import TransectInfo
from weewx_clearskies_api.services.swan_spectral import decompose_spectrum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Metres per degree latitude (flat-earth approximation for coastal scale).
_M_PER_DEG_LAT: float = 111_320.0

#: Peel angle classification thresholds (Walker 1974; SURF-ZONE-MODEL-BRIEF §5.8).
_PEEL_CLOSEOUT_MAX_DEG: float = 30.0
_PEEL_FAST_MAX_DEG: float = 45.0
_PEEL_GOOD_MAX_DEG: float = 66.0


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass
class PartitionBreakResult:
    """Break point info for one swell partition on one transect.

    Populated by the per-partition 1D model run.  May be absent (None in the
    containing list) when the 1D model produced no break points for this
    partition × transect combination.
    """

    partition_index: int
    """Zero-based index of the swell partition in the decomposition output."""

    break_points: list[BreakPoint]
    """All break points found by the 1D model (primary = index 0, outermost)."""

    face_height_m: float
    """Breaking face height (m) at the primary break point: 1.27 × Hs_break
    (Rayleigh H1/10 statistical conversion, source='break_point').
    Zero when no break points detected."""

    hs_at_break_m: float
    """Hs at the primary break point from the 1D model (m).
    Zero when no break points detected."""


@dataclass
class TransectResult:
    """Fully combined results for one cross-shore transect.

    Holds the per-partition Hs profiles (post-RSS-combination, depth-limited),
    per-partition break results, and the aggregated face height for the transect.
    """

    transect_index: int
    """Position of this transect in the transect array (matches TransectInfo.index)."""

    is_structure_affected: bool
    """True when this transect crosses an OBSTACLE.  Structure-affected transects
    are excluded from headline metrics (best peak, spot average, peel angle) but
    included in the quasi-2D heat map."""

    hs_total_profile: np.ndarray
    """RSS-combined, depth-limited Hs at every bathymetric profile point (m)."""

    distances: np.ndarray
    """Distance from shore at each profile point (m), offshore→shore order."""

    depths: np.ndarray
    """Tide-adjusted depth at each profile point (m, positive below MSL)."""

    per_partition: list[PartitionBreakResult | None]
    """One entry per swell partition (same order as the decomposition output).
    None when the 1D model failed or produced no break points for that partition."""

    best_face_height_m: float
    """Maximum face height across all partition break points at this transect.
    Represents the best surfable wave available here.  Zero if no partition broke."""


@dataclass
class PartitionBreakInfo:
    """Per-swell-partition break statistics aggregated across open transects.

    Provides the "what each swell component does at the beach" information —
    where it breaks, how high, what breaker type.  Only open (non-structure-
    affected) transects contribute to these statistics.
    """

    partition_index: int
    """Zero-based index in the decomposition output."""

    period_s: float
    """Energy-weighted peak period of this partition (s)."""

    direction_deg: float
    """Energy-weighted mean direction of this partition (degrees nautical)."""

    height_m: float
    """Partition Hs at the handoff point (m) — deep-water input value."""

    classification: str
    """Swell classification: 'groundswell', 'swell', or 'wind_swell'."""

    mean_break_distance_m: float | None
    """Mean distance from shore of the primary break point across open transects (m).
    None if no open transects produced a break point for this partition."""

    mean_face_height_m: float | None
    """Mean face height (m) across open transects that had a break point."""

    peak_face_height_m: float | None
    """Maximum face height (m) across open transects (best case for this partition)."""

    mean_break_depth_m: float | None
    """Mean depth at the primary break point across open transects (m)."""

    dominant_breaker_type: str | None
    """Most common breaker type ('spilling', 'plunging', 'surging') across open transects."""


@dataclass
class PipelineResult:
    """Top-level output of the per-partition swell transformation pipeline.

    Contains aggregated headline metrics (best peak, spot average, peel angle)
    plus the full per-transect and per-partition detailed output.
    """

    best_peak_face_height_m: float
    """Highest face height (m) across all OPEN transects.
    Represents the best wave at the spot for this forecast timestep."""

    spot_average_face_height_m: float
    """Mean face height (m) across all OPEN transects.
    Represents the typical wave a surfer will get."""

    peel_angle_deg: float | None
    """Peel angle in degrees, computed from the spatial variation of break points
    across adjacent open transects.  None when fewer than 2 open transects have
    break points for the dominant partition."""

    peel_classification: str | None
    """Peel quality with direction suffix.
    Quality component: 'closeout', 'fast', 'good', 'mellow' (Walker 1974).
    Direction suffix: '_right', '_left', '_a_frame' appended for non-closeout waves.
    Examples: 'fast_right', 'good_left', 'mellow_a_frame', 'closeout'.
    None when peel_angle_deg is None."""

    peel_direction: str | None
    """Peel direction from the surfer's perspective (facing the ocean).
    'right', 'left', 'a_frame' (symmetric), or None when direction cannot be
    determined (fewer than 2 open transects with break points, or degenerate
    geometry).  Always None for 'closeout' — closeouts do not peel."""

    transect_count: int
    """Total number of transects in the array (open + structure-affected)."""

    open_transect_count: int
    """Number of transects NOT crossing any OBSTACLE structure.  Only open
    transects contribute to headline metrics."""

    per_transect: list[TransectResult]
    """One entry per transect in the original array order.
    Includes both open and structure-affected transects for the heat map.
    Only present when the 1D model succeeded for the transect — failed
    transects are omitted (see degraded flag)."""

    per_partition_breaks: list[PartitionBreakInfo]
    """Per-swell-partition break statistics aggregated across open transects."""

    degraded: bool
    """True when the pipeline could not complete normally:
      - No transects had bathymetric profiles
      - All transects failed the 1D model
      - Spectrum decomposition returned no partitions
    In degraded mode, face heights and peel angle may be zero / None."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bathy_array(transect: TransectInfo) -> np.ndarray | None:
    """Convert TransectInfo.bathymetric_profile to the Nx2 numpy array expected by
    run_1d_analytical().

    Returns None when the profile is empty (no bathymetry loaded yet).
    """
    if not transect.bathymetric_profile:
        return None
    try:
        arr = np.array(
            [[p["distance_m"], p["depth_m"]] for p in transect.bathymetric_profile],
            dtype=float,
        )
        if arr.ndim != 2 or arr.shape[1] < 2 or arr.shape[0] < 2:
            return None
        return arr
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("_bathy_array: malformed bathymetric_profile — %s", exc)
        return None


def _along_shore_distance_m(t1: TransectInfo, t2: TransectInfo) -> float:
    """Flat-earth great-circle distance (m) between two transect origins.

    Used to determine the actual along-shore spacing between adjacent open
    transects for peel angle calculation.  Works for the coastal-scale
    distances involved (< 5 km).
    """
    dlat = (t2.origin_lat - t1.origin_lat) * _M_PER_DEG_LAT
    mid_lat = (t1.origin_lat + t2.origin_lat) / 2.0
    dlon = (t2.origin_lon - t1.origin_lon) * _M_PER_DEG_LAT * math.cos(math.radians(mid_lat))
    return math.sqrt(dlat ** 2 + dlon ** 2)


def _combine_partition_hs(
    partition_profiles: list[np.ndarray | None],
    depths: np.ndarray,
    gamma: float,
) -> tuple[np.ndarray, np.ndarray]:
    """RSS-combine per-partition Hs profiles and enforce depth-limited saturation.

    Implements the FABLE P2 combined saturation check:
    After RSS combination, enforce Hs_total ≤ γd at every point.  Individual
    partitions may each be below γd but their RSS sum may exceed it; this
    function reduces the total and redistributes the reduction proportionally.

    Args:
        partition_profiles: One Hs array per partition (None → all zeros).
        depths: Tide-adjusted depth at each grid point (m, positive = below MSL).
        gamma: Depth-limited breaking parameter (default 0.73).

    Returns:
        (hs_total, hs_stack) where:
          hs_total: RSS-combined, depth-limited Hs (m).
          hs_stack: (n_partitions × n_points) array of per-partition Hs after
                    saturation redistribution.
    """
    n_parts = len(partition_profiles)
    n_pts = len(depths)

    hs_stack = np.zeros((n_parts, n_pts), dtype=float)
    for p_idx, profile in enumerate(partition_profiles):
        if profile is not None and len(profile) == n_pts:
            hs_stack[p_idx] = profile

    hs_total = np.sqrt(np.sum(hs_stack ** 2, axis=0))

    # Depth-limited saturation: cap Hs_total at γd.
    gamma_d = gamma * np.maximum(depths, 0.01)
    exceeded = hs_total > gamma_d
    if np.any(exceeded):
        # Scale all partition Hs proportionally at each exceeded point.
        ratio = np.where(exceeded, gamma_d / np.maximum(hs_total, 1e-9), 1.0)
        hs_stack *= ratio[np.newaxis, :]  # broadcast (n_parts × n_pts)
        hs_total = np.minimum(hs_total, gamma_d)

    return hs_total, hs_stack


def _classify_peel(peel_angle_deg: float) -> str:
    """Classify peel angle per Walker (1974) / SURF-ZONE-MODEL-BRIEF §5.8."""
    if peel_angle_deg < _PEEL_CLOSEOUT_MAX_DEG:
        return "closeout"
    if peel_angle_deg < _PEEL_FAST_MAX_DEG:
        return "fast"
    if peel_angle_deg <= _PEEL_GOOD_MAX_DEG:
        return "good"
    return "mellow"


def _compute_peel_angle(
    open_transect_results: list[tuple[int, TransectResult]],
    all_transects: list[TransectInfo],
    partitions: list[dict[str, Any]],
    beach_facing: float,
) -> tuple[float | None, str | None, str | None]:
    """Compute peel angle and direction from break-point spatial variation across open transects.

    Method (SURF-ZONE-MODEL-BRIEF §5.8):
      1. Use the dominant partition (highest Hs) as the reference swell system.
      2. For each adjacent pair of open transects: compute the break-line angle
         relative to shore = arctan(|Δx| / Δy) where Δx is the change in
         primary break-point distance from shore and Δy is the along-shore
         spacing.
      3. Compute the wave crest angle from shore = angle between wave travel
         direction and shore-normal.
      4. peel_angle = |wave_crest_angle - mean_break_line_angle|.
      5. Peel direction from regression slope of break_distance vs signed
         along-shore position (positive = surfer's right, negative = left).
         |slope| < 0.05 → a_frame (break line nearly parallel to shore).

    Returns (peel_angle_deg, peel_classification_with_suffix, peel_direction)
    or (None, None, None).
    """
    if len(open_transect_results) < 2:
        return None, None, None

    # Dominant partition: first in the list (sorted by descending Hs).
    dominant_p_idx = 0
    dominant_dir = partitions[0]["direction"]

    # Wave crest angle from shore-parallel.
    # beach_facing: the direction the beach faces = direction waves arrive FROM.
    # If waves come exactly from beach_facing, they are shore-normal → crests
    # are shore-parallel → wave_crest_angle = 0°.
    # Obliqueness = angular difference between wave direction and beach_facing.
    obliqueness = abs((dominant_dir - beach_facing + 180.0) % 360.0 - 180.0)
    wave_crest_angle = min(obliqueness, 90.0)

    # Gather primary break-point distances from open transects.
    break_pts: list[tuple[int, float]] = []  # (transect_idx, break_distance_m)
    for t_idx, tr in open_transect_results:
        pbr = tr.per_partition[dominant_p_idx] if dominant_p_idx < len(tr.per_partition) else None
        if pbr is not None and pbr.break_points:
            break_pts.append((t_idx, pbr.break_points[0].distance_m))

    if len(break_pts) < 2:
        return None, None, None

    # Compute break-line angles from adjacent pairs.
    break_line_angles: list[float] = []
    for i in range(len(break_pts) - 1):
        t1_idx, x1 = break_pts[i]
        t2_idx, x2 = break_pts[i + 1]
        # Along-shore spacing (actual distance between transect origins).
        t1 = all_transects[t1_idx]
        t2 = all_transects[t2_idx]
        delta_y = _along_shore_distance_m(t1, t2)
        if delta_y < 0.1:
            continue  # degenerate — skip
        delta_x = abs(x2 - x1)
        break_line_angles.append(math.degrees(math.atan2(delta_x, delta_y)))

    if not break_line_angles:
        return None, None, None

    mean_break_line_angle = sum(break_line_angles) / len(break_line_angles)
    peel_angle = abs(wave_crest_angle - mean_break_line_angle)
    peel_angle = min(peel_angle, 90.0)  # physical upper bound

    base_class = _classify_peel(peel_angle)

    # ------------------------------------------------------------------
    # Peel direction: regression of break_distance vs signed along-shore
    # position in the surfer's "right" direction.
    #
    # "Right" direction from surfer facing the ocean (facing beach_facing):
    #   right_compass = (beach_facing + 90°) mod 360°.
    # Compass → cartesian: east = sin(θ), north = cos(θ).
    # Positive slope → break farther offshore to the right → right peel.
    # Negative slope → break farther offshore to the left → left peel.
    # |slope| < 0.05 → break line nearly parallel to shore → a_frame.
    # ------------------------------------------------------------------
    right_dir_rad = math.radians((beach_facing + 90.0) % 360.0)
    right_east = math.sin(right_dir_rad)
    right_north = math.cos(right_dir_rad)

    ref_t_idx = break_pts[0][0]
    ref_t = all_transects[ref_t_idx]
    signed_positions: list[float] = []
    for t_idx, _ in break_pts:
        t = all_transects[t_idx]
        dlat_m = (t.origin_lat - ref_t.origin_lat) * _M_PER_DEG_LAT
        mid_lat = (t.origin_lat + ref_t.origin_lat) / 2.0
        dlon_m = (
            (t.origin_lon - ref_t.origin_lon)
            * _M_PER_DEG_LAT
            * math.cos(math.radians(mid_lat))
        )
        signed_positions.append(dlon_m * right_east + dlat_m * right_north)

    break_distances = [bp[1] for bp in break_pts]
    n_pts = len(signed_positions)
    mean_pos = sum(signed_positions) / n_pts
    mean_bd = sum(break_distances) / n_pts
    num = sum(
        (p - mean_pos) * (d - mean_bd)
        for p, d in zip(signed_positions, break_distances)
    )
    den = sum((p - mean_pos) ** 2 for p in signed_positions)

    peel_direction: str | None = None
    if den > 1.0:  # require at least 1 m of positional spread
        slope = num / den
        if abs(slope) < 0.05:
            peel_direction = "a_frame"
        elif slope > 0:
            peel_direction = "right"
        else:
            peel_direction = "left"

    # Build classification string with direction suffix.
    # Closeout waves do not peel directionally — no suffix.
    if base_class == "closeout":
        classification = "closeout"
    elif peel_direction == "a_frame":
        classification = f"{base_class}_a_frame"
    elif peel_direction is not None:
        classification = f"{base_class}_{peel_direction}"
    else:
        classification = base_class

    logger.debug(
        "_compute_peel_angle: dominant partition dir=%.1f°, beach_facing=%.1f°, "
        "wave_crest_angle=%.1f°, mean_break_line=%.1f°, "
        "peel=%.1f° (%s, dir=%s), from %d adjacent pairs",
        dominant_dir,
        beach_facing,
        wave_crest_angle,
        mean_break_line_angle,
        peel_angle,
        classification,
        peel_direction or "n/a",
        len(break_line_angles),
    )
    return peel_angle, classification, peel_direction


def _match_partitions(
    canonical_partitions: list[dict[str, Any]],
    handoff_partitions: list[dict[str, Any]],
) -> list[int | None]:
    """Match handoff partitions to canonical partition indices (T4.5b).

    For each handoff partition, finds the best-matching canonical partition
    by nearest (period, direction).  Match criteria: period within ±2 s AND
    direction within ±20°.  The 0°/360° wraparound is handled correctly
    (e.g., 5° and 355° are 10° apart, not 350° apart).

    The canonical list comes from the deep-water SPECOUT decomposition (L2,
    ~15 m depth) and is the reference set used by the swell display card.
    The handoff list comes from the L3 SPECOUT (nearshore handoff point) used
    by the 1D model; structure effects or shoaling at L3 can produce a
    different partition structure.

    Args:
        canonical_partitions: Partitions from the deep-water SPECOUT.
            Each dict must have ``"period"`` (s) and ``"direction"`` (deg).
        handoff_partitions: Partitions from the handoff SPECOUT (L3).

    Returns:
        List of length ``len(handoff_partitions)``.  Each element is the
        index into *canonical_partitions* of the best match, or ``None``
        when no canonical partition satisfies both the ±2 s period AND ±20°
        direction criteria.
    """
    _PERIOD_TOL_S: float = 2.0
    _DIR_TOL_DEG: float = 20.0

    result: list[int | None] = []

    for h_part in handoff_partitions:
        h_period = float(h_part.get("period", 0.0))
        h_dir = float(h_part.get("direction", 0.0))

        best_idx: int | None = None
        best_score: float = float("inf")

        for c_idx, c_part in enumerate(canonical_partitions):
            c_period = float(c_part.get("period", 0.0))
            c_dir = float(c_part.get("direction", 0.0))

            dp = abs(h_period - c_period)
            # Smallest arc between two compass bearings (handles 0°/360° wrap).
            dd = abs((h_dir - c_dir + 180.0) % 360.0 - 180.0)

            if dp <= _PERIOD_TOL_S and dd <= _DIR_TOL_DEG:
                # Normalised combined score — both tolerances count equally.
                score = (dp / _PERIOD_TOL_S) + (dd / _DIR_TOL_DEG)
                if score < best_score:
                    best_score = score
                    best_idx = c_idx

        result.append(best_idx)

    logger.debug(
        "_match_partitions: %d handoff → %d canonical; matched=%d, unmatched=%d",
        len(handoff_partitions),
        len(canonical_partitions),
        sum(1 for m in result if m is not None),
        sum(1 for m in result if m is None),
    )
    return result


def _aggregate_partition_breaks(
    open_transect_results: list[tuple[int, TransectResult]],
    handoff_partitions: list[dict[str, Any]],
    canonical_mapping: list[int | None] | None = None,
    canonical_partitions: list[dict[str, Any]] | None = None,
) -> list[PartitionBreakInfo]:
    """Aggregate per-partition break statistics across open transects.

    For each swell partition, collects face heights, break distances, depths,
    and breaker types from all open transects that produced a break point for
    that partition.

    T4.5b: When *canonical_partitions* and *canonical_mapping* are supplied,
    results are indexed by **canonical** partition index so the swell display
    card can connect each partition to its breaking behaviour.  Handoff
    partitions that do not match any canonical partition are collected into an
    ``partition_index=-1`` "other" entry (appended at the end of the list).
    When no canonical data is available, handoff partition indices are used
    directly (backward-compatible behaviour).

    Args:
        open_transect_results: (transect_idx, TransectResult) pairs for open
            (non-structure-affected) transects that succeeded.
        handoff_partitions: Swell partitions from the handoff SPECOUT
            decomposition (same order as ``TransectResult.per_partition``).
        canonical_mapping: Output of ``_match_partitions()``.  Length must
            equal ``len(handoff_partitions)``.  ``None`` disables T4.5b logic.
        canonical_partitions: Partitions from the deep-water SPECOUT.
            ``None`` disables T4.5b logic.

    Returns:
        One :class:`PartitionBreakInfo` per output partition.
        - With canonical data: N_canonical entries + optional "other" entry.
        - Without canonical data: one entry per handoff partition.
    """
    # ---- Collect raw stats per handoff partition index ----
    def _collect(p_idx: int) -> tuple[list[float], list[float], list[float], list[str]]:
        dists: list[float] = []
        faces: list[float] = []
        bdepths: list[float] = []
        btypes: list[str] = []
        for _, tr in open_transect_results:
            pbr = tr.per_partition[p_idx] if p_idx < len(tr.per_partition) else None
            if pbr is None or not pbr.break_points:
                continue
            bp = pbr.break_points[0]
            dists.append(bp.distance_m)
            faces.append(pbr.face_height_m)
            bdepths.append(bp.depth_m)
            btypes.append(bp.breaker_type)
        return dists, faces, bdepths, btypes

    def _summarize(
        dists: list[float],
        faces: list[float],
        bdepths: list[float],
        btypes: list[str],
    ) -> tuple[float | None, float | None, float | None, float | None, str | None]:
        if not dists:
            return None, None, None, None, None
        return (
            sum(dists) / len(dists),
            sum(faces) / len(faces),
            max(faces),
            sum(bdepths) / len(bdepths),
            Counter(btypes).most_common(1)[0][0],
        )

    results: list[PartitionBreakInfo] = []

    if canonical_partitions is not None and canonical_mapping is not None:
        # ---- T4.5b: aggregate by canonical partition index ----
        # Accumulate stats from matched handoff partitions into their canonical bucket.
        # Unmatched handoff partitions land in bucket key=-1 ("other").
        accum: dict[int, tuple[list[float], list[float], list[float], list[str]]] = {}

        for h_idx in range(len(handoff_partitions)):
            c_idx = canonical_mapping[h_idx] if h_idx < len(canonical_mapping) else None
            key = c_idx if c_idx is not None else -1

            dists, faces, bdepths, btypes = _collect(h_idx)
            if not dists:
                # No break data for this handoff partition — nothing to accumulate.
                continue

            if key not in accum:
                accum[key] = ([], [], [], [])
            ad, af, ab, at_ = accum[key]
            ad.extend(dists)
            af.extend(faces)
            ab.extend(bdepths)
            at_.extend(btypes)

        # Build one PartitionBreakInfo per canonical partition.
        for c_idx, c_part in enumerate(canonical_partitions):
            ad, af, ab, at_ = accum.get(c_idx, ([], [], [], []))
            mean_dist, mean_face, peak_face, mean_depth, dom_type = _summarize(
                ad, af, ab, at_
            )
            results.append(PartitionBreakInfo(
                partition_index=c_idx,
                period_s=float(c_part.get("period", 0.0)),
                direction_deg=float(c_part.get("direction", 0.0)),
                height_m=float(c_part.get("height", 0.0)),
                classification=c_part.get("classification", "swell"),
                mean_break_distance_m=mean_dist,
                mean_face_height_m=mean_face,
                peak_face_height_m=peak_face,
                mean_break_depth_m=mean_depth,
                dominant_breaker_type=dom_type,
            ))

        # Append "other" entry for unmatched handoff partitions (if any had breaks).
        if -1 in accum:
            ad, af, ab, at_ = accum[-1]
            mean_dist, mean_face, peak_face, mean_depth, dom_type = _summarize(
                ad, af, ab, at_
            )
            if mean_dist is not None:
                # Use the first unmatched handoff partition for the metadata fields.
                first_unmatched = next(
                    (
                        handoff_partitions[h_idx]
                        for h_idx in range(len(handoff_partitions))
                        if h_idx < len(canonical_mapping)
                        and canonical_mapping[h_idx] is None
                    ),
                    handoff_partitions[0],
                )
                n_unmatched = sum(1 for m in canonical_mapping if m is None)
                logger.debug(
                    "_aggregate_partition_breaks: %d handoff partition(s) unmatched "
                    "to any canonical partition — aggregated as 'other' (index=-1)",
                    n_unmatched,
                )
                results.append(PartitionBreakInfo(
                    partition_index=-1,  # "other" — no canonical match
                    period_s=float(first_unmatched.get("period", 0.0)),
                    direction_deg=float(first_unmatched.get("direction", 0.0)),
                    height_m=float(first_unmatched.get("height", 0.0)),
                    classification=first_unmatched.get("classification", "swell"),
                    mean_break_distance_m=mean_dist,
                    mean_face_height_m=mean_face,
                    peak_face_height_m=peak_face,
                    mean_break_depth_m=mean_depth,
                    dominant_breaker_type=dom_type,
                ))

    else:
        # ---- Backward-compatible: use handoff partition indices directly ----
        for p_idx, partition in enumerate(handoff_partitions):
            dists, faces, bdepths, btypes = _collect(p_idx)
            mean_dist, mean_face, peak_face, mean_depth, dom_type = _summarize(
                dists, faces, bdepths, btypes
            )
            results.append(PartitionBreakInfo(
                partition_index=p_idx,
                period_s=float(partition.get("period", 0.0)),
                direction_deg=float(partition.get("direction", 0.0)),
                height_m=float(partition.get("height", 0.0)),
                classification=partition.get("classification", "swell"),
                mean_break_distance_m=mean_dist,
                mean_face_height_m=mean_face,
                peak_face_height_m=peak_face,
                mean_break_depth_m=mean_depth,
                dominant_breaker_type=dom_type,
            ))

    return results


def _degraded_result(
    transect_count: int,
    reason: str,
) -> PipelineResult:
    """Return a degraded PipelineResult with a log warning."""
    logger.warning("surf_1d_pipeline: degraded — %s", reason)
    return PipelineResult(
        best_peak_face_height_m=0.0,
        spot_average_face_height_m=0.0,
        peel_angle_deg=None,
        peel_classification=None,
        peel_direction=None,
        transect_count=transect_count,
        open_transect_count=0,
        per_transect=[],
        per_partition_breaks=[],
        degraded=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_pipeline(
    specout_data: dict[str, Any] | None,
    transects: list[TransectInfo],
    tide_level: float,
    beach_facing: float,
    gamma: float = 0.73,
    bulk_hs: float | None = None,
    bulk_tp: float | None = None,
    bulk_dir: float | None = None,
    canonical_partitions: list[dict[str, Any]] | None = None,
) -> PipelineResult:
    """Run the per-partition multi-transect 1D swell transformation pipeline.

    Implements SURF-ZONE-MODEL-BRIEF §7 end-to-end:
      1. Decomposes the SPECOUT spectrum into N swell partitions.
      2. Runs the analytical 1D model for each partition × each transect.
      3. RSS-combines per-partition Hs profiles at each transect point.
      4. Enforces depth-limited saturation (Hs_total ≤ γd) after combination.
      5. Computes face height (1.27 × Hs, H1/10 Rayleigh factor) at each
         partition's primary break point.
      6. Aggregates best peak and spot average from OPEN transects only.
      7. Computes peel angle from break-point spatial variation across open transects.

    Args:
        specout_data: Parsed SPECOUT spectrum for one forecast timestep —
            output of ``parse_specout_file()`` for a single timestep:
            {
                "time":       ISO-8601 UTC string,
                "freqs_hz":   list[float],
                "dirs_deg":   list[float],
                "energy":     list[list[float]],   # E[i_freq][i_dir]
            }
            This is the HANDOFF SPECOUT (from L3 when available, L2 otherwise).
            The DEEP-WATER SPECOUT for the swell display card is consumed
            separately upstream (T3.3 / T4.2 step a).
            ``None`` or an empty dict triggers T4.5 bulk-fallback behaviour.
        transects: List of TransectInfo objects from compute_spot_transects().
            The ``bathymetric_profile`` field must be populated (done at SWAN
            runtime per T3.3) — transects with empty profiles are skipped.
        tide_level: Tide offset in metres (added to every bathymetric depth).
            Must be in the same vertical datum as the bathymetric profiles.
        beach_facing: Shore-normal direction in degrees (compass convention,
            true north).  Waves arrive FROM this direction.  Used for Snell's
            law refraction and peel angle computation.
        gamma: Depth-limited breaking parameter (default 0.73, matching SWAN).
        bulk_hs: T4.5 — fallback significant wave height (m) from SWAN TABLE.
            Used only when *specout_data* is None/empty/missing required keys.
        bulk_tp: T4.5 — fallback peak period (s) from SWAN TABLE.
        bulk_dir: T4.5 — fallback wave direction (degrees) from SWAN TABLE.
        canonical_partitions: T4.5b — partitions from the deep-water (L2)
            SPECOUT decomposition that the swell display card uses.  When
            provided, ``per_partition_breaks`` in the result uses canonical
            partition indices so the card can connect each partition to its
            breaking behaviour.  ``None`` disables canonical-index logic and
            falls back to handoff partition indices.

    Returns:
        PipelineResult with headline metrics and full per-transect output.
        Returns a degraded result (degraded=True) when:
          - SPECOUT decomposition finds zero partitions AND no bulk fallback
            values are provided
          - All transects have empty bathymetric profiles
          - The 1D model fails on every transect
        Returns degraded=True (but with valid 1D face-height results) when:
          - SPECOUT unavailable and bulk Hs/Tp/Dir are used as single partition
    """
    n_transects = len(transects)

    # ------------------------------------------------------------------
    # Step 1: Decompose spectrum into swell partitions
    # T4.5: Handle None / empty / missing-key specout_data gracefully.
    # ------------------------------------------------------------------
    if specout_data is None:
        specout_data = {}

    partitions = decompose_spectrum(
        freqs_hz=specout_data.get("freqs_hz", []),
        dirs_deg=specout_data.get("dirs_deg", []),
        energy=specout_data.get("energy", []),
    )

    # T4.5: track whether we degraded due to missing SPECOUT.
    _specout_degraded: bool = False

    if not partitions:
        # T4.5: SPECOUT unavailable or un-parseable — fall back to a single
        # bulk partition synthesised from SWAN TABLE bulk parameters if available.
        if bulk_hs is not None and bulk_tp is not None and bulk_dir is not None:
            _bulk_class = (
                "groundswell" if bulk_tp >= 12.5
                else "swell" if bulk_tp >= 10.0
                else "wind_swell"
            )
            logger.warning(
                "run_pipeline: SPECOUT unavailable or empty — falling back to "
                "single bulk partition (Hs=%.2f m, Tp=%.1f s, Dir=%.0f°); "
                "result will be marked degraded=True",
                bulk_hs,
                bulk_tp,
                bulk_dir,
            )
            partitions = [{
                "height": bulk_hs,
                "period": bulk_tp,
                "direction": bulk_dir,
                "energy": 0.0,
                "frequencyRange": [0.0, 0.0],
                "classification": _bulk_class,
            }]
            _specout_degraded = True
        else:
            return _degraded_result(
                n_transects,
                "spectral decomposition returned no partitions",
            )

    n_partitions = len(partitions)
    logger.info(
        "run_pipeline: %d transects, %d spectral partitions, "
        "tide=%.2f m, beach_facing=%.1f°, gamma=%.2f%s",
        n_transects,
        n_partitions,
        tide_level,
        beach_facing,
        gamma,
        " [bulk-fallback]" if _specout_degraded else "",
    )

    # T4.5b: Match handoff partitions to canonical partition indices.
    # canonical_mapping[h_idx] = c_idx | None (None = no canonical match).
    canonical_mapping: list[int | None] | None = None
    if canonical_partitions is not None and partitions and not _specout_degraded:
        canonical_mapping = _match_partitions(canonical_partitions, partitions)

    # ------------------------------------------------------------------
    # Step 2: Run 1D model for each partition × each transect
    # ------------------------------------------------------------------
    # Shape: analytical_results[partition_idx][transect_idx] = Analytical1DResult | None
    analytical_results: list[list[Analytical1DResult | None]] = []

    for p_idx, partition in enumerate(partitions):
        hs_p = partition["height"]
        tp_p = partition["period"]
        dir_p = partition["direction"]

        transect_run_results: list[Analytical1DResult | None] = []
        for t_idx, transect in enumerate(transects):
            bathy = _bathy_array(transect)
            if bathy is None:
                transect_run_results.append(None)
                continue
            try:
                result = run_1d_analytical(
                    hs=hs_p,
                    tp=tp_p,
                    direction=dir_p,
                    bathy_profile=bathy,
                    tide_level=tide_level,
                    gamma=gamma,
                    beach_facing=beach_facing,
                )
                transect_run_results.append(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "run_pipeline: 1D model failed for partition %d (%.1fs %.0f°) "
                    "transect %d — %s",
                    p_idx,
                    tp_p,
                    dir_p,
                    t_idx,
                    exc,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                transect_run_results.append(None)

        analytical_results.append(transect_run_results)

    # ------------------------------------------------------------------
    # Step 3-5: Per-transect RSS combination + face heights
    # ------------------------------------------------------------------
    per_transect_results: list[TransectResult | None] = []
    failed_count = 0

    for t_idx, transect in enumerate(transects):
        # Collect partition 1D results for this transect.
        p_results_for_t: list[Analytical1DResult | None] = [
            analytical_results[p_idx][t_idx] for p_idx in range(n_partitions)
        ]

        valid_results = [r for r in p_results_for_t if r is not None]
        if not valid_results:
            failed_count += 1
            per_transect_results.append(None)
            continue

        # Use the reference grid (distances/depths) from the first valid result.
        # All partitions share the same bathymetric profile → same grid.
        ref_result = valid_results[0]
        depths = ref_result.depths    # tide-adjusted, offshore→shore
        distances = ref_result.distances  # distance from shore (decreasing)
        n_pts = len(depths)

        # Extract per-partition Hs profiles aligned to the reference grid.
        partition_profiles: list[np.ndarray | None] = []
        for p_result in p_results_for_t:
            if p_result is None:
                partition_profiles.append(None)
            elif len(p_result.hs_profile) == n_pts:
                partition_profiles.append(p_result.hs_profile)
            else:
                # Length mismatch (shouldn't happen — same bathy profile).
                logger.warning(
                    "run_pipeline: transect %d partition profile length mismatch "
                    "(%d vs %d) — treating as None",
                    t_idx,
                    len(p_result.hs_profile),
                    n_pts,
                )
                partition_profiles.append(None)

        # Step 3+4: RSS combination + depth-limited saturation (FABLE P2).
        hs_total, _ = _combine_partition_hs(partition_profiles, depths, gamma)

        # Step 5: Per-partition face height at each partition's primary break point.
        per_partition_break_results: list[PartitionBreakResult | None] = []
        for p_idx, p_result in enumerate(p_results_for_t):
            if p_result is None or not p_result.break_points:
                per_partition_break_results.append(None)
                continue

            primary_bp = p_result.break_points[0]  # outermost / primary break
            face_h = hsig_to_face_height(
                hsig_m=primary_bp.hs_m,
                period_s=partitions[p_idx]["period"],
                source="break_point",
            )
            per_partition_break_results.append(PartitionBreakResult(
                partition_index=p_idx,
                break_points=p_result.break_points,
                face_height_m=face_h,
                hs_at_break_m=primary_bp.hs_m,
            ))

        # Aggregate best face height for this transect.
        valid_face_heights = [
            r.face_height_m for r in per_partition_break_results if r is not None
        ]
        best_face = max(valid_face_heights) if valid_face_heights else 0.0

        per_transect_results.append(TransectResult(
            transect_index=t_idx,
            is_structure_affected=transect.is_structure_affected,
            hs_total_profile=hs_total,
            distances=distances,
            depths=depths,
            per_partition=per_partition_break_results,
            best_face_height_m=best_face,
        ))

    # Check for total failure.
    successful = [r for r in per_transect_results if r is not None]
    if not successful:
        return _degraded_result(n_transects, "1D model failed on all transects")

    if failed_count > 0:
        logger.warning(
            "run_pipeline: %d/%d transect(s) failed the 1D model — "
            "excluded from aggregation",
            failed_count,
            n_transects,
        )

    # ------------------------------------------------------------------
    # Step 6: Aggregate across OPEN transects only
    # ------------------------------------------------------------------
    # open_transect_results: list of (transect_idx, TransectResult) for
    # open (non-structure-affected) transects that succeeded.
    open_transect_results: list[tuple[int, TransectResult]] = [
        (t_idx, tr)
        for t_idx, (tr, transect) in enumerate(zip(per_transect_results, transects))
        if tr is not None and not transect.is_structure_affected
    ]

    open_transect_count = len(open_transect_results)

    if open_transect_count == 0:
        # All transects are structure-affected or failed.
        # Fall back to all successful transects for the headline.
        logger.warning(
            "run_pipeline: no open transects — using all %d successful transects "
            "for headline metrics (structure-affected spots)",
            len(successful),
        )
        open_transect_results = [
            (t_idx, tr)
            for t_idx, tr in enumerate(per_transect_results)
            if tr is not None
        ]

    open_face_heights = [tr.best_face_height_m for _, tr in open_transect_results]
    best_peak_face_height = max(open_face_heights) if open_face_heights else 0.0
    spot_avg_face_height = (
        sum(open_face_heights) / len(open_face_heights) if open_face_heights else 0.0
    )

    # ------------------------------------------------------------------
    # Step 7: Peel angle
    # ------------------------------------------------------------------
    peel_angle, peel_class, peel_dir = _compute_peel_angle(
        open_transect_results, transects, partitions, beach_facing
    )

    # ------------------------------------------------------------------
    # Per-partition aggregation across open transects
    # T4.5b: pass canonical mapping so results use canonical indices.
    # ------------------------------------------------------------------
    per_partition_breaks = _aggregate_partition_breaks(
        open_transect_results,
        partitions,
        canonical_mapping=canonical_mapping,
        canonical_partitions=canonical_partitions if canonical_mapping is not None else None,
    )

    # Log summary.
    logger.info(
        "run_pipeline complete: %d transects (%d open), "
        "best_peak=%.2f m, spot_avg=%.2f m, "
        "peel=%.1f° (%s), %d partition(s)",
        n_transects,
        open_transect_count,
        best_peak_face_height,
        spot_avg_face_height,
        peel_angle if peel_angle is not None else float("nan"),
        peel_class or "n/a",
        n_partitions,
    )

    return PipelineResult(
        best_peak_face_height_m=best_peak_face_height,
        spot_average_face_height_m=spot_avg_face_height,
        peel_angle_deg=peel_angle,
        peel_classification=peel_class,
        peel_direction=peel_dir,
        transect_count=n_transects,
        open_transect_count=open_transect_count,
        per_transect=[tr for tr in per_transect_results if tr is not None],
        per_partition_breaks=per_partition_breaks,
        # T4.5: degraded=True when SPECOUT was unavailable and bulk fallback was used.
        # Remaining pathways (partial transect failure, all-transect failure) are
        # handled by _degraded_result() and the failed_count warning above.
        degraded=_specout_degraded,
    )
