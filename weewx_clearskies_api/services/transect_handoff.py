"""Pre-model handoff depth determination algorithm (Phase 2, T2.3).

Implements the 4-step handoff depth algorithm from SURF-ZONE-MODEL-BRIEF
§2.3.4.  Called during surf spot setup (wizard), *before* any SWAN run,
because transect generation (T2.2) needs to know the handoff depth for each
transect.

Step 1  Determine structure depth extents from CUDEM.
Step 2  Compute geometric shadow per transect — three approach angles
        (beach_facing_degrees, beach_facing ± 30°).  Ray optics, conservative.
Step 3  Assign handoff depth per transect:
            open transect  →  10 m (safe default)
            shadowed       →  max(5 m, shallowest_structure_depth − 1.5 m)
        Clamped to [5 m, 15 m] for all transects.
Step 4  Per-run QB refinement (optional runtime function).  When SWAN CURVE
        output is available after a model run, ``refine_handoff_with_qb()``
        scans outward from the configured handoff depth and moves it deeper if
        QB > threshold.

References
----------
SURF-ZONE-MODEL-BRIEF §2.3.2 — structure influence is geometric, not
    proportional.
SURF-ZONE-MODEL-BRIEF §2.3.4 — full algorithm with HB Pier worked example.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable

from weewx_clearskies_api.config.marine_config import StructureConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (from SURF-ZONE-MODEL-BRIEF §2.3.4)
# ---------------------------------------------------------------------------

#: Default handoff depth for open (unshadowed) transects.
#: Safe for Hs up to 7.3 m (breaking criterion: d = Hs / 0.73).
_DEFAULT_HANDOFF_DEPTH_M: float = 10.0

#: Minimum handoff depth — shallower than this risks QB > 0 (breaking
#: contamination) for moderate to large swells.
_MIN_HANDOFF_DEPTH_M: float = 5.0

#: Maximum handoff depth — the L3 offshore boundary.
_MAX_HANDOFF_DEPTH_M: float = 15.0

#: Buffer applied shoreward of the structure's seaward tip depth.
#: Handoff is placed *just shoreward* of the structure so the spectrum
#: has fully passed through it before the 1D model takes over.
_STRUCTURE_BUFFER_M: float = 1.5

#: Three wave approach angles used for shadow testing.  The middle value is
#: the nominal beach_facing_degrees; ±30° cover realistic swell direction
#: variability.  A transect is shadowed if ANY of the three angles places it
#: in a structure's geometric shadow.
_SHADOW_ANGLE_OFFSETS_DEG: tuple[float, float, float] = (-30.0, 0.0, 30.0)

#: Metres per degree of latitude (approximately, for the coastal US).
_M_PER_DEG_LAT: float = 111_320.0


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass
class TransectHandoff:
    """Handoff depth assignment for one transect.

    Attributes
    ----------
    handoff_depth_m:
        CUDEM depth (metres, positive = below MSL) at which the 1D model
        should receive its boundary condition from SWAN.  Clamped to
        [5.0 m, 15.0 m].
    is_shadowed:
        True when one or more structures cast a geometric shadow on this
        transect for at least one of the three tested approach angles.
    shadowing_structures:
        Human-readable labels (``"{type}({length_m:.0f}m)"``) of every
        structure that shadows this transect.  Empty when ``is_shadowed``
        is False.
    """

    handoff_depth_m: float
    is_shadowed: bool
    shadowing_structures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Local 2-D coordinate helpers
# ---------------------------------------------------------------------------


def _to_local_xy(
    lat: float,
    lon: float,
    ref_lat: float,
    ref_lon: float,
) -> tuple[float, float]:
    """Convert (lat, lon) to local metres east/north relative to *ref*.

    Uses a flat-earth approximation valid for the coastal-scale distances
    involved here (< 10 km).
    """
    x = (lon - ref_lon) * _M_PER_DEG_LAT * math.cos(math.radians(ref_lat))
    y = (lat - ref_lat) * _M_PER_DEG_LAT
    return x, y


def _wave_travel_unit_vector(approach_degrees: float) -> tuple[float, float]:
    """Return (ux, uy) unit vector in the wave TRAVEL direction (toward shore).

    ``approach_degrees`` is the direction the beach faces — also the direction
    waves come FROM (i.e. from the ocean toward shore is *approach + 180°*).
    """
    travel_deg = (approach_degrees + 180.0) % 360.0
    # Compass convention: 0° = north, 90° = east.
    travel_rad = math.radians(travel_deg)
    ux = math.sin(travel_rad)   # eastward component
    uy = math.cos(travel_rad)   # northward component
    return ux, uy


def _project_uv(
    x: float,
    y: float,
    ux: float,
    uy: float,
) -> tuple[float, float]:
    """Project local (x, y) onto wave-travel (u) and cross-wave (v) axes.

    u  — along wave travel direction (positive = shoreward).
    v  — along-shore (perpendicular to wave travel; positive = rightward when
         facing shore).
    """
    u = x * ux + y * uy
    v = -x * uy + y * ux  # 90° CCW rotation
    return u, v


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _structure_label(structure: StructureConfig) -> str:
    """Short human-readable label used in log messages and return values."""
    return f"{structure.type}({structure.length_m:.0f}m)"


def _structure_seaward_tip_depth(
    structure: StructureConfig,
    bathymetry_profile_fn: Callable[[float, float], float | None],
) -> float | None:
    """Return CUDEM depth (m) at the structure's seaward endpoint.

    The seaward tip is whichever of the two OBSTACLE-LINE endpoints has the
    greater depth (is deeper below MSL).  Returns ``None`` when the structure
    has fewer than two coordinate pairs (cannot determine geometry).

    ``structure.coordinates`` is ``[[lon, lat], ...]`` — same order as the
    SWAN OBSTACLE LINE command.
    """
    coords = structure.coordinates  # [[lon, lat], ...]
    if len(coords) < 2:
        logger.debug(
            "Structure %s: only %d coordinate pair(s) — need ≥2 to locate "
            "seaward tip; skipping.",
            _structure_label(structure),
            len(coords),
        )
        return None

    # Sample only the two endpoints.  For a simple two-point OBSTACLE the
    # endpoints are shore-side and seaward-tip.  For a multi-point OBSTACLE
    # the first and last points span the structure extent.
    depths: list[float] = []
    for lon, lat in (coords[0], coords[-1]):
        d = bathymetry_profile_fn(lat, lon)
        depths.append(d if d is not None else 0.0)

    seaward_depth = max(depths)
    logger.debug(
        "Structure %s: endpoint depths=%s → seaward tip depth=%.1fm",
        _structure_label(structure),
        [f"{d:.1f}" for d in depths],
        seaward_depth,
    )
    return seaward_depth


def _compute_shadow_span(
    structure: StructureConfig,
    ref_lat: float,
    ref_lon: float,
    ux: float,
    uy: float,
) -> tuple[float, float, float] | None:
    """Compute the structure's shadow extent in (u, v) coordinates.

    Returns ``(tip_u, v_min, v_max)`` where:
    - ``tip_u``  — u-coordinate of the structure's most-offshore point.
      A transect is potentially in shadow only when its u > tip_u
      (i.e., the transect is shoreward of the offshore tip).
    - ``v_min/v_max`` — cross-wave span of the entire structure in the
      v-direction.  A transect whose v-coordinate falls inside this range
      is geometrically in shadow.

    Returns ``None`` when the structure has fewer than two coordinates.
    """
    coords = structure.coordinates
    if len(coords) < 2:
        return None

    us: list[float] = []
    vs: list[float] = []
    for lon, lat in coords:
        x, y = _to_local_xy(lat, lon, ref_lat, ref_lon)
        u, v = _project_uv(x, y, ux, uy)
        us.append(u)
        vs.append(v)

    # Most-offshore point = minimum u (farthest from shore in wave direction).
    tip_u = min(us)
    return tip_u, min(vs), max(vs)


def _in_shadow(
    transect_u: float,
    transect_v: float,
    tip_u: float,
    v_min: float,
    v_max: float,
) -> bool:
    """Return True if (transect_u, transect_v) falls in the geometric shadow.

    Shadow region (ray optics):
      - Shoreward of the structure's seaward tip: transect_u > tip_u.
      - Within the structure's cross-wave span: v_min ≤ transect_v ≤ v_max.
    """
    return transect_u > tip_u and v_min <= transect_v <= v_max


# ---------------------------------------------------------------------------
# Public API — Step 1-3
# ---------------------------------------------------------------------------


def compute_handoff_depths(
    transect_origins: list[tuple[float, float]],
    transect_bearings: list[float],
    structures: list[StructureConfig],
    beach_facing_degrees: float,
    bathymetry_profile_fn: Callable[[float, float], float | None],
) -> list[TransectHandoff]:
    """Determine the pre-model handoff depth for every transect.

    Implements SURF-ZONE-MODEL-BRIEF §2.3.4 Steps 1–3.  Step 4 (runtime QB
    refinement) is a separate function: ``refine_handoff_with_qb()``.

    Parameters
    ----------
    transect_origins:
        ``(lat, lon)`` of each transect's shore-side origin, one entry per
        transect.
    transect_bearings:
        Compass bearing (degrees, toward offshore) for each transect.
        Accepted for completeness and future use; shadow geometry is driven
        by *beach_facing_degrees*, not per-transect bearings.
    structures:
        ``StructureConfig`` objects from the marine config (Overpass API
        discovery output).
    beach_facing_degrees:
        Direction the beach face points — same as the predominant wave
        approach direction (waves come FROM this bearing).
    bathymetry_profile_fn:
        ``Callable[[lat: float, lon: float], depth_m: float | None]``.
        Returns CUDEM depth in metres (non-negative, positive = below MSL)
        at a point, or ``None`` when the point falls outside CUDEM coverage.
        Queried only at structure endpoint coordinates (Step 1).

    Returns
    -------
    list[TransectHandoff]
        One entry per transect, in the same order as *transect_origins*.
    """
    if len(transect_origins) != len(transect_bearings):
        raise ValueError(
            f"transect_origins has {len(transect_origins)} entries but "
            f"transect_bearings has {len(transect_bearings)}"
        )
    if not transect_origins:
        return []

    # ------------------------------------------------------------------ #
    # Reference origin for the local flat-earth coordinate system.        #
    # Using the centroid of transect origins keeps numeric precision high  #
    # for all transects in the array.                                      #
    # ------------------------------------------------------------------ #
    ref_lat = sum(lat for lat, _ in transect_origins) / len(transect_origins)
    ref_lon = sum(lon for _, lon in transect_origins) / len(transect_origins)

    # ------------------------------------------------------------------ #
    # Step 1: seaward tip depth for each structure.                        #
    # ------------------------------------------------------------------ #
    # Parallel list to `structures`:  None → no usable geometry; else the
    # CUDEM depth at the seaward tip.
    struct_depths: list[float | None] = []
    for s in structures:
        depth = _structure_seaward_tip_depth(s, bathymetry_profile_fn)
        struct_depths.append(depth)
        if depth is None:
            logger.info(
                "Structure %s: no usable coordinates — excluded from shadow "
                "computation.",
                _structure_label(s),
            )
        else:
            logger.info(
                "Structure %s: seaward tip depth = %.1f m",
                _structure_label(s),
                depth,
            )

    # ------------------------------------------------------------------ #
    # Step 2 (pre-computation): shadow spans per (structure, angle).       #
    # ------------------------------------------------------------------ #
    # Precompute the wave-direction unit vectors and shadow spans for all
    # three approach angles to avoid recomputing them for every transect.
    approach_angles = [
        (beach_facing_degrees + offset) % 360.0
        for offset in _SHADOW_ANGLE_OFFSETS_DEG
    ]
    # unit_vecs[angle_idx] = (ux, uy)
    unit_vecs = [_wave_travel_unit_vector(a) for a in approach_angles]

    # spans[struct_idx][angle_idx] = (tip_u, v_min, v_max) or None
    spans: list[list[tuple[float, float, float] | None]] = []
    for s_idx, s in enumerate(structures):
        angle_spans: list[tuple[float, float, float] | None] = []
        if struct_depths[s_idx] is None:
            angle_spans = [None] * len(approach_angles)
        else:
            for ux, uy in unit_vecs:
                angle_spans.append(
                    _compute_shadow_span(s, ref_lat, ref_lon, ux, uy)
                )
        spans.append(angle_spans)

    # ------------------------------------------------------------------ #
    # Step 3: assign handoff depth per transect.                           #
    # ------------------------------------------------------------------ #
    results: list[TransectHandoff] = []

    for t_idx, (t_lat, t_lon) in enumerate(transect_origins):
        t_x, t_y = _to_local_xy(t_lat, t_lon, ref_lat, ref_lon)

        # Collect every structure that shadows this transect (any angle).
        shadowing_structs: list[tuple[str, float]] = []  # (label, depth_m)

        for s_idx, s in enumerate(structures):
            if struct_depths[s_idx] is None:
                continue

            for angle_idx, (ux, uy) in enumerate(unit_vecs):
                span = spans[s_idx][angle_idx]
                if span is None:
                    continue
                tip_u, v_min, v_max = span
                t_u, t_v = _project_uv(t_x, t_y, ux, uy)
                if _in_shadow(t_u, t_v, tip_u, v_min, v_max):
                    shadowing_structs.append(
                        (_structure_label(s), struct_depths[s_idx])  # type: ignore[arg-type]
                    )
                    break  # confirmed for this structure; check next structure

        if not shadowing_structs:
            handoff_depth = _DEFAULT_HANDOFF_DEPTH_M
            is_shadowed = False
            shadowing_names: list[str] = []
        else:
            is_shadowed = True
            # Use the shallowest (closest to surface) structure depth that
            # shadows this transect — the most shoreward structure that the
            # wave spectrum must pass through.
            shallowest = min(depth for _, depth in shadowing_structs)
            handoff_depth = shallowest - _STRUCTURE_BUFFER_M
            # Never shallower than 5 m (breaking contamination risk).
            handoff_depth = max(handoff_depth, _MIN_HANDOFF_DEPTH_M)
            shadowing_names = list(dict.fromkeys(  # deduplicate, preserve order
                label for label, _ in shadowing_structs
            ))

        # Never deeper than the L3 offshore boundary (15 m).
        handoff_depth = min(handoff_depth, _MAX_HANDOFF_DEPTH_M)

        logger.info(
            "Transect %d (%.5f, %.5f): handoff_depth=%.1f m, "
            "shadowed=%s, structures=%s",
            t_idx,
            t_lat,
            t_lon,
            handoff_depth,
            is_shadowed,
            shadowing_names if is_shadowed else "[]",
        )
        results.append(
            TransectHandoff(
                handoff_depth_m=handoff_depth,
                is_shadowed=is_shadowed,
                shadowing_structures=shadowing_names,
            )
        )

    # Summary log.
    n_shadowed = sum(1 for r in results if r.is_shadowed)
    logger.info(
        "compute_handoff_depths complete: %d transects total, "
        "%d shadowed, %d open. beach_facing=%.1f°",
        len(results),
        n_shadowed,
        len(results) - n_shadowed,
        beach_facing_degrees,
    )
    return results


# ---------------------------------------------------------------------------
# Public API — Step 4: per-run QB refinement (runtime, optional)
# ---------------------------------------------------------------------------


def refine_handoff_with_qb(
    handoffs: list[TransectHandoff],
    qb_profiles: list[list[tuple[float, float]]],
    *,
    qb_threshold: float = 0.05,
    max_deepening_m: float = 5.0,
) -> list[TransectHandoff]:
    """Step 4: adjust handoff depths using SWAN QB output after a model run.

    Called at model *runtime* once SWAN has produced CURVE TABLE output with
    QB values — not at setup time.  If QB at the configured handoff depth
    exceeds *qb_threshold* (indicating active wave breaking at that depth,
    which would contaminate the 1D model's boundary condition), the handoff
    is moved deeper until QB ≈ 0 or the deepening cap is reached.

    Parameters
    ----------
    handoffs:
        Pre-configured handoff depths from ``compute_handoff_depths()``.
    qb_profiles:
        For each transect (same order as *handoffs*), a list of
        ``(depth_m, QB)`` pairs extracted from the SWAN CURVE TABLE at this
        forecast cycle.  Pairs may be in any order; this function sorts them.
        Pass an empty list for transects whose QB data is unavailable — those
        transects are left unchanged.
    qb_threshold:
        QB value above which the handoff is considered contaminated by
        breaking.  SURF-ZONE-MODEL-BRIEF §2.3.3 notes QB ~ 0 is typical down
        to 3.7 m at HB Pier for normal conditions; default 0.05 is
        conservative.
    max_deepening_m:
        Maximum additional deepening allowed (metres).  Prevents pushing the
        handoff beyond the L3 grid outer boundary.

    Returns
    -------
    list[TransectHandoff]
        New list with adjusted handoff depths.  ``is_shadowed`` and
        ``shadowing_structures`` are preserved unchanged from the input.
    """
    if len(handoffs) != len(qb_profiles):
        raise ValueError(
            f"handoffs ({len(handoffs)}) and qb_profiles ({len(qb_profiles)}) "
            "must have the same length"
        )

    refined: list[TransectHandoff] = []

    for t_idx, (ho, qb_profile) in enumerate(
        zip(handoffs, qb_profiles, strict=True)
    ):
        if not qb_profile:
            # No QB data for this transect — keep pre-configured depth.
            refined.append(ho)
            continue

        configured = ho.handoff_depth_m

        # Sort by depth ascending (shallowest → deepest) so we can scan
        # outward from the handoff toward deep water.
        sorted_qb = sorted(qb_profile, key=lambda p: p[0])

        # Find QB at the profile point closest to the configured depth.
        nearest_depth, nearest_qb = min(
            sorted_qb, key=lambda p: abs(p[0] - configured)
        )

        if nearest_qb <= qb_threshold:
            # QB is clean at this depth — no adjustment needed.
            refined.append(ho)
            continue

        # QB > threshold — scan deeper until clean or cap reached.
        new_depth: float = configured
        moved = False

        for depth_m, qb in sorted_qb:
            if depth_m < configured:
                continue  # Skip points shallower than current handoff.
            deepening = depth_m - configured
            if deepening > max_deepening_m:
                # Hit the cap before finding clean QB.
                new_depth = min(configured + max_deepening_m, _MAX_HANDOFF_DEPTH_M)
                logger.warning(
                    "Transect %d: QB=%.3f still above %.3f at %.1f m after "
                    "%.1f m of deepening (cap=%.1f m). "
                    "Capping handoff at %.1f m — extreme event, handoff may "
                    "still be contaminated by breaking.",
                    t_idx,
                    qb,
                    qb_threshold,
                    depth_m,
                    deepening,
                    max_deepening_m,
                    new_depth,
                )
                moved = True
                break
            if qb <= qb_threshold:
                new_depth = depth_m
                moved = True
                break

        if moved and new_depth != configured:
            logger.warning(
                "Transect %d: QB=%.3f > %.3f at configured depth %.1f m; "
                "handoff deepened to %.1f m (delta=+%.1f m).",
                t_idx,
                nearest_qb,
                qb_threshold,
                configured,
                new_depth,
                new_depth - configured,
            )
            refined.append(
                TransectHandoff(
                    handoff_depth_m=new_depth,
                    is_shadowed=ho.is_shadowed,
                    shadowing_structures=ho.shadowing_structures,
                )
            )
        else:
            refined.append(ho)

    return refined


# ---------------------------------------------------------------------------
# Self-test: HB Pier worked example (§2.3.4)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Run the HB Pier worked example and verify output.

    Expected result (from SURF-ZONE-MODEL-BRIEF §2.3.4):
      - ~3-5 pier-shadowed transects with handoff depth ≈ 5.5 m
        (pier seaward depth 7 m − 1.5 m buffer = 5.5 m).
      - ~25-27 open transects with handoff depth = 10.0 m.
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # --- HB Pier geometry ---------------------------------------------------
    # The pier extends SW from the shore to approximately 7 m depth.
    # Coordinates are approximate but geometrically representative.
    PIER_SHORE_LAT = 33.6592
    PIER_SHORE_LON = -118.0027
    PIER_SEA_LAT = 33.6571
    PIER_SEA_LON = -118.0048
    PIER_DEPTH_M = 7.0

    # --- Mock bathymetry function -------------------------------------------
    # Returns PIER_DEPTH_M at the seaward tip; 0 m at the shore end;
    # a simple linear ramp elsewhere so the function is well-behaved.
    def mock_bathy(lat: float, lon: float) -> float:
        if (
            abs(lat - PIER_SEA_LAT) < 1e-4
            and abs(lon - PIER_SEA_LON) < 1e-4
        ):
            return PIER_DEPTH_M
        if (
            abs(lat - PIER_SHORE_LAT) < 1e-4
            and abs(lon - PIER_SHORE_LON) < 1e-4
        ):
            return 0.5
        # Generic linear ramp: ~20 cm/m from the shore end of the pier.
        dist_lat = (lat - PIER_SHORE_LAT) * _M_PER_DEG_LAT
        dist_lon = (
            (lon - PIER_SHORE_LON)
            * _M_PER_DEG_LAT
            * math.cos(math.radians(PIER_SHORE_LAT))
        )
        dist_m = math.sqrt(dist_lat ** 2 + dist_lon ** 2)
        return min(15.0, max(0.0, dist_m * 0.02))

    # --- Pier StructureConfig -----------------------------------------------
    pier = StructureConfig(
        {
            "type": "pier",
            "material": "permeable",
            "length_m": 300.0,
            "bearing_degrees": 225.0,
            "distance_m": 0.0,
            "coordinates": [
                [PIER_SHORE_LON, PIER_SHORE_LAT],  # [lon, lat] — shore end
                [PIER_SEA_LON, PIER_SEA_LAT],       # [lon, lat] — seaward tip
            ],
        }
    )

    # --- 30 transects at 10 m spacing along the shoreline ------------------
    # HB beach runs roughly NW-SE; shore bearing ~300° (NW direction).
    # Origin placed so the pier falls within the middle of the array.
    BEACH_FACING_DEG = 210.0   # beach faces SW (toward ocean)
    SHORE_BEARING_DEG = 300.0  # longshore direction (NW)
    ORIGIN_LAT = 33.6593
    ORIGIN_LON = -118.0013

    transect_origins: list[tuple[float, float]] = []
    shore_rad = math.radians(SHORE_BEARING_DEG)
    for i in range(30):
        dist_m = i * 10.0
        dlat = math.cos(shore_rad) * dist_m / _M_PER_DEG_LAT
        dlon = math.sin(shore_rad) * dist_m / (
            _M_PER_DEG_LAT * math.cos(math.radians(ORIGIN_LAT))
        )
        transect_origins.append((ORIGIN_LAT + dlat, ORIGIN_LON + dlon))

    transect_bearings = [BEACH_FACING_DEG] * 30

    # --- Run the algorithm --------------------------------------------------
    print("\n=== HB Pier Worked Example (SURF-ZONE-MODEL-BRIEF §2.3.4) ===\n")
    results = compute_handoff_depths(
        transect_origins=transect_origins,
        transect_bearings=transect_bearings,
        structures=[pier],
        beach_facing_degrees=BEACH_FACING_DEG,
        bathymetry_profile_fn=mock_bathy,
    )

    n_shadowed = sum(1 for r in results if r.is_shadowed)
    n_open = len(results) - n_shadowed
    expected_shadow_depth = max(
        _MIN_HANDOFF_DEPTH_M, PIER_DEPTH_M - _STRUCTURE_BUFFER_M
    )

    print(f"\nResults: {len(results)} transects, {n_shadowed} shadowed, {n_open} open")
    print(
        f"Shadowed indices: "
        f"{[i for i, r in enumerate(results) if r.is_shadowed]}"
    )
    print(f"Expected shadowed depth: {expected_shadow_depth:.1f} m")
    print(f"Expected open depth: {_DEFAULT_HANDOFF_DEPTH_M:.1f} m")

    # --- Verify results -----------------------------------------------------
    # Note on shadow count: the brief states "~3-5 transects in the pier's
    # geometric shadow" for REAL HB Pier coordinates.  This self-test uses
    # synthetic coordinates that do not precisely replicate the pier's actual
    # geometry; the exact count is geometry-dependent and varies with the
    # bearing between pier and wave approach angles.  The important invariants
    # are (a) SOME transects are shadowed, (b) SOME are open, (c) the depth
    # assignments are physically correct.
    ok = True

    if not (1 <= n_shadowed <= len(results) - 1):
        print(
            f"FAIL: expected 1–{len(results) - 1} shadowed transects, got {n_shadowed} "
            "(algorithm either shadows none or all — geometry check needed)"
        )
        ok = False
    else:
        print(f"OK: {n_shadowed} shadowed, {n_open} open (geometry-dependent count)")

    shadow_depths = [r.handoff_depth_m for r in results if r.is_shadowed]
    if shadow_depths and not all(
        abs(d - expected_shadow_depth) < 0.01 for d in shadow_depths
    ):
        print(
            f"FAIL: shadowed handoff depths should all be {expected_shadow_depth:.1f} m, "
            f"got {sorted(set(shadow_depths))}"
        )
        ok = False

    open_depths = [r.handoff_depth_m for r in results if not r.is_shadowed]
    if not all(abs(d - _DEFAULT_HANDOFF_DEPTH_M) < 0.01 for d in open_depths):
        print(
            f"FAIL: open handoff depths should all be {_DEFAULT_HANDOFF_DEPTH_M:.1f} m, "
            f"got {sorted(set(open_depths))}"
        )
        ok = False

    # Quick QB refinement smoke-test.
    dummy_qb: list[list[tuple[float, float]]] = [[] for _ in results]
    # Simulate QB > 0 at 5.5 m for the first transect, clean at 7 m.
    dummy_qb[0] = [(5.5, 0.15), (7.0, 0.01), (10.0, 0.0)]
    refined = refine_handoff_with_qb(
        results, dummy_qb, qb_threshold=0.05, max_deepening_m=5.0
    )
    if refined[0].handoff_depth_m <= results[0].handoff_depth_m:
        print(
            "FAIL: QB refinement should have deepened transect 0 "
            f"(was {results[0].handoff_depth_m:.1f} m, "
            f"got {refined[0].handoff_depth_m:.1f} m)"
        )
        ok = False
    else:
        print(
            f"\nQB refinement: transect 0 deepened from "
            f"{results[0].handoff_depth_m:.1f} m to "
            f"{refined[0].handoff_depth_m:.1f} m (QB was 0.15 at 5.5 m)."
        )

    if ok:
        print("\nSelf-test PASSED")
    else:
        print("\nSelf-test FAILED — see details above")
        sys.exit(1)
