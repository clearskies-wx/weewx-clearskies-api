"""Pre-model handoff selection algorithm (Phase 2 T2.3; rewritten Phase 4A T4A.9/T4A.10).

Two independent jobs live in this module, split per ADR-093 Amendment 2 / the
2026-07-25 L3-1D boundary decisions (`docs/planning/briefs/
L3-1D-BOUNDARY-DECISIONS-BRIEF.md` D2/D2b) and coordinator constraint C1 for
this round:

1. **Geometric shadow classification** (``compute_transect_shadows()``,
   Steps 1-2 of the original algorithm) — unchanged in substance. Determines
   whether a transect crosses an OBSTACLE structure's geometric shadow. This
   feeds ``swan_formats.TransectInfo.is_structure_affected`` /
   ``shadowing_structures``, which drive the multi-transect obstacle filter
   (SURF-ZONE-MODEL-BRIEF §2.2.3): structure-affected transects are excluded
   from headline metrics (best peak, spot average) but still rendered on the
   heat map. This computation must survive independent of how the handoff
   depth is determined — removing it would drop a responsibility (CLAUDE.md
   trigger 2), which is why it is kept as its own function rather than folded
   away.

2. **Per-forecast-hour handoff selection** (T4A.9, ``select_hourly_handoff()``)
   and **runtime QB assertion** (T4A.10, ``refine_handoff_with_qb()``) — new
   in this round. ADR-093 Amendment 2 replaced the old fixed-at-setup handoff
   depth (open=10 m, shadowed=structure depth - buffer, floor 5 m, ceiling
   15 m) with a per-hour computation:

       handoff depth (this hour) = 1.3 * Hs(hour) / gamma      (gamma=0.73)

   applied uniformly to every transect — structure geometry no longer moves
   the handoff (L3-1D-BOUNDARY-DECISIONS-BRIEF settled decision #7; it can
   only deepen SwellTrack's fine zone, a separate computation in
   ``enrichment/bathymetry.py``). The **grid is frozen at setup**
   (`rules/clearskies-process.md` "All SWAN grid geometry is fixed at setup
   time") — only the *station on the grid* that is read for the handoff
   spectrum moves, hour to hour. ``select_hourly_handoff()`` is a lookup
   against station depths that already exist; it never resizes or
   re-requests any geometry.

   The 5 m floor (``_MIN_HANDOFF_DEPTH_M``) is REMOVED per the operator's
   2026-07-25 approval, on the explicit condition that its removal is
   watched in testing — see the shallow-end tests in
   ``tests/test_transect_handoff.py`` and the closeout finding filed
   alongside this change.

References
----------
ADR-093 Amendment 2 §2 — the per-hour handoff formula and grid-freeze rule.
ADR-095 Amendment 2 — "No SPECOUT may be extracted at an L3 boundary cell."
SURF-ZONE-MODEL-BRIEF §2.3.2 — structure influence is geometric, not
    proportional (shadow classification only).
L3-1D-BOUNDARY-DECISIONS-BRIEF.md D2/D2b — handoff-as-lookup rationale.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from weewx_clearskies_api.config.marine_config import StructureConfig
from weewx_clearskies_api.metrics import HANDOFF_QB_VIOLATIONS_TOTAL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Reference handoff depth used when a transect has no L3 grid to sample from
#: (open beach, or the cluster failed the L3 viability test). Per
#: L3-1D-BOUNDARY-DECISIONS-BRIEF D3: 15 m from L2 — the same point already
#: used as the deep-water reference SPECOUT for the swell display card
#: (ADR-095 Amendment 2), so no separate extraction is needed for this case.
#: Public (no leading underscore) — ``swan_formats.py`` reuses this exact
#: value for the placeholder ``TransectInfo.handoff_depth_m`` it sets at
#: setup time, before any per-hour selection exists (DRY: one number, not a
#: duplicated magic constant across modules).
L2_REFERENCE_DEPTH_M: float = 15.0

#: The shoaling margin above the depth-induced breaking-onset depth
#: (`Hs/gamma`) used to place the per-hour handoff seaward of where SWAN's
#: Battjes-Janssen dissipation has corrupted the spectrum (ADR-093
#: Amendment 2 §2). Not a physics constant being re-derived here — this is
#: the same 1.3 factor already accepted for the (now-superseded) fixed-depth
#: rule, applied per hour instead of frozen at the year's largest swell.
_BREAKING_MARGIN_FACTOR: float = 1.3

#: Depth-limited breaking parameter, matching SWAN's own Battjes-Janssen
#: dissipation and SwellTrack's default (``surf_1d_pipeline.run_pipeline``).
_GAMMA_BREAKING: float = 0.73

#: Three wave approach angles used for shadow testing.  The middle value is
#: the nominal beach_facing_degrees; ±30° cover realistic swell direction
#: variability.  A transect is shadowed if ANY of the three angles places it
#: in a structure's geometric shadow.
_SHADOW_ANGLE_OFFSETS_DEG: tuple[float, float, float] = (-30.0, 0.0, 30.0)

#: Metres per degree of latitude (approximately, for the coastal US).
_M_PER_DEG_LAT: float = 111_320.0

#: Default QB (SWAN breaking fraction) threshold above which a sampled
#: station is considered contaminated by active breaking (T4A.10).
#: SURF-ZONE-MODEL-BRIEF §2.3.3: QB ~ 0 is typical seaward of the surf zone;
#: 0.05 is a conservative near-zero cutoff.
_DEFAULT_QB_THRESHOLD: float = 0.05

#: Greppable log-message prefix for T4A.10's runtime breaking-zone assertion.
#: Do not change this string without updating any operator alerting that
#: greps for it (T4A.10 Do step 2).
_QB_VIOLATION_LOG_PREFIX: str = "SWAN handoff"


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


@dataclass
class TransectShadow:
    """Geometric shadow classification for one transect.

    Split out of the old ``TransectHandoff`` (T4A.9): handoff depth is no
    longer a fixed setup-time value, so it no longer belongs on this
    dataclass. This is consumed by ``swan_formats.TransectInfo
    .is_structure_affected`` / ``.shadowing_structures`` — the multi-transect
    obstacle filter (SURF-ZONE-MODEL-BRIEF §2.2.3).

    Attributes
    ----------
    is_shadowed:
        True when one or more structures cast a geometric shadow on this
        transect for at least one of the three tested approach angles.
    shadowing_structures:
        Human-readable labels (``"{type}({length_m:.0f}m)"``) of every
        structure that shadows this transect.  Empty when ``is_shadowed``
        is False.
    """

    is_shadowed: bool
    shadowing_structures: list[str] = field(default_factory=list)


@dataclass
class HandoffSelection:
    """Result of selecting the per-forecast-hour handoff sample for one
    transect (T4A.9).

    Attributes
    ----------
    handoff_depth_m:
        The TARGET breaking-margin depth for this hour:
        ``1.3 * Hs(hour) / gamma``, or the fixed L2 reference depth (15 m)
        when this transect has no L3 grid to sample from. This is the value
        surfaced to callers as ``handoffDepthM`` (T4A.6 item g contract).
    source_level:
        ``"L3"`` when sampled from the L3 grid's cross-shore CURVE stations,
        ``"L2"`` when using the fixed deep-water reference (no L3, or the
        cluster failed the viability test). Surfaced as
        ``handoffSourceLevel``.
    station_index:
        Index into the transect's L3 CURVE station list that was sampled.
        ``None`` when ``source_level == "L2"`` — the L2 reference is a
        single fixed point, not a station on a curve.
    station_depth_m:
        The ACTUAL depth of the sampled station or reference point. May
        differ from ``handoff_depth_m`` because station selection is a
        discrete lookup against existing grid geometry, not an exact match.
    clamped:
        True when the target depth landed on or outside the L3 grid's
        boundary stations and the selection was moved to the nearest
        interior station (T4A.9 Do step 4 / ADR-095 Amendment 2 — no SPECOUT
        may be extracted at an L3 boundary cell).
    """

    handoff_depth_m: float
    source_level: str
    station_index: int | None
    station_depth_m: float
    clamped: bool


class HandoffBreakingError(Exception):
    """Raised by ``refine_handoff_with_qb()`` when SWAN's own QB output shows
    active breaking at every station reachable within the deepening cap
    (T4A.10). The caller MUST fail the hour for this transect rather than
    serve a sample from a breaking cell — the exact failure mode the
    2026-07-23 incident (0.01 m Hs served during a 6-8 ft swell, HTTP 200)
    exemplifies for a different frozen-depth defect.

    Attributes mirror the ERROR log fields so callers/tests can assert on
    structured state instead of parsing the message string
    (`rules/coding.md` "Dispatch on exception state via attributes").
    """

    def __init__(
        self,
        message: str,
        *,
        transect_index: int,
        hour: int | None,
        computed_depth_m: float,
        actual_depth_m: float,
        qb: float,
    ) -> None:
        super().__init__(message)
        self.transect_index = transect_index
        self.hour = hour
        self.computed_depth_m = computed_depth_m
        self.actual_depth_m = actual_depth_m
        self.qb = qb


# ---------------------------------------------------------------------------
# Local 2-D coordinate helpers (shadow classification only)
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
# Internal helpers — shadow classification
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
# Public API — geometric shadow classification (Steps 1-2 of the original
# algorithm; unchanged in substance, split per C1).
# ---------------------------------------------------------------------------


def compute_transect_shadows(
    transect_origins: list[tuple[float, float]],
    transect_bearings: list[float],
    structures: list[StructureConfig],
    beach_facing_degrees: float,
    bathymetry_profile_fn: Callable[[float, float], float | None],
) -> list[TransectShadow]:
    """Determine the geometric structure shadow for every transect.

    Implements SURF-ZONE-MODEL-BRIEF §2.3.4 Steps 1-2 only. Handoff depth
    assignment (the original Step 3) is no longer a setup-time computation —
    see ``select_hourly_handoff()``.

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
    list[TransectShadow]
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
    approach_angles = [
        (beach_facing_degrees + offset) % 360.0
        for offset in _SHADOW_ANGLE_OFFSETS_DEG
    ]
    unit_vecs = [_wave_travel_unit_vector(a) for a in approach_angles]

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
    # Step 2 (classification): which transects fall in whose shadow.       #
    # ------------------------------------------------------------------ #
    results: list[TransectShadow] = []

    for t_idx, (t_lat, t_lon) in enumerate(transect_origins):
        t_x, t_y = _to_local_xy(t_lat, t_lon, ref_lat, ref_lon)

        shadowing_structs: list[str] = []

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
                    shadowing_structs.append(_structure_label(s))
                    break  # confirmed for this structure; check next structure

        is_shadowed = bool(shadowing_structs)
        shadowing_names = list(dict.fromkeys(shadowing_structs))  # dedupe, preserve order

        logger.info(
            "Transect %d (%.5f, %.5f): shadowed=%s, structures=%s",
            t_idx,
            t_lat,
            t_lon,
            is_shadowed,
            shadowing_names if is_shadowed else "[]",
        )
        results.append(
            TransectShadow(
                is_shadowed=is_shadowed,
                shadowing_structures=shadowing_names,
            )
        )

    n_shadowed = sum(1 for r in results if r.is_shadowed)
    logger.info(
        "compute_transect_shadows complete: %d transects total, "
        "%d shadowed, %d open. beach_facing=%.1f°",
        len(results),
        n_shadowed,
        len(results) - n_shadowed,
        beach_facing_degrees,
    )
    return results


# ---------------------------------------------------------------------------
# Public API — T4B.1: standalone breaking-margin depth formula
# ---------------------------------------------------------------------------


def breaking_margin_depth_m(
    hs_m: float,
    *,
    gamma: float = _GAMMA_BREAKING,
    margin: float = _BREAKING_MARGIN_FACTOR,
) -> float:
    """Return the ADR-093 Amendment 2 §2 breaking-margin target depth for a
    given Hs: ``margin * hs_m / gamma``.

    This is the exact formula ``select_hourly_handoff()`` computes internally
    as ``target_depth_m``, exposed standalone so setup-time per-transect band
    sizing (T4B.1) can compute the same target depth that
    ``select_hourly_handoff()`` will later select a station against, without
    duplicating the formula (`rules/coding.md` DRY — "search before writing a
    new helper"). Not a new formula and not a change to the existing one —
    only how precisely/efficiently it is reused.
    """
    return margin * hs_m / gamma


# ---------------------------------------------------------------------------
# Public API — T4A.9: per-forecast-hour handoff selection
# ---------------------------------------------------------------------------


def select_hourly_handoff(
    hs_m: float,
    station_depths_m: Sequence[float] | None,
    *,
    gamma: float = _GAMMA_BREAKING,
    margin: float = _BREAKING_MARGIN_FACTOR,
    l2_reference_depth_m: float = L2_REFERENCE_DEPTH_M,
) -> HandoffSelection:
    """Select this forecast hour's handoff sample for one transect (T4A.9).

    The grid is frozen at setup (C2 — never resized here). This function only
    performs a lookup against station depths that already exist on the
    transect's L3 CURVE (or falls back to the fixed L2 reference when there
    is no L3 grid for this transect).

    Parameters
    ----------
    hs_m:
        Significant wave height (m) at this transect for this forecast hour
        — the input to the breaking-onset depth formula.
    station_depths_m:
        Depths (m, positive = below MSL) of every station on this
        transect's L3 CURVE, ordered offshore → nearshore (index 0 = deep/
        seaward grid boundary, index -1 = shallow/shoreward grid boundary —
        matching ``compute_spot_transect()``'s documented point order).
        ``None`` or empty ⇒ this transect has no L3 grid (open beach, or the
        cluster failed the viability test) ⇒ falls back to the L2 reference
        depth, ``source_level="L2"``.
    gamma:
        Depth-limited breaking parameter (default 0.73, matching SWAN and
        SwellTrack).
    margin:
        Shoaling margin factor (default 1.3, ADR-093 Amendment 2 §2).
    l2_reference_depth_m:
        Fallback depth used when no L3 station data is available (default
        15 m, L3-1D-BOUNDARY-DECISIONS-BRIEF D3).

    Returns
    -------
    HandoffSelection
    """
    target_depth_m = breaking_margin_depth_m(hs_m, gamma=gamma, margin=margin)

    if not station_depths_m:
        return HandoffSelection(
            handoff_depth_m=l2_reference_depth_m,
            source_level="L2",
            station_index=None,
            station_depth_m=l2_reference_depth_m,
            clamped=False,
        )

    n = len(station_depths_m)

    if n < 3:
        # No interior station exists — honouring the no-boundary-cell rule
        # (ADR-095 Amendment 2) leaves nothing to select from this grid.
        logger.warning(
            "select_hourly_handoff: L3 curve has only %d station(s) — no "
            "interior station available; falling back to L2 reference "
            "depth (%.1f m).",
            n,
            l2_reference_depth_m,
        )
        return HandoffSelection(
            handoff_depth_m=l2_reference_depth_m,
            source_level="L2",
            station_index=None,
            station_depth_m=l2_reference_depth_m,
            clamped=False,
        )

    # Boundary stations (index 0 and index n-1) may never be sampled
    # (ADR-095 Amendment 2: "No SPECOUT may be extracted at an L3 boundary
    # cell"). Search only the interior.
    global_best_idx = min(
        range(n), key=lambda i: abs(station_depths_m[i] - target_depth_m)
    )
    interior_best_idx = min(
        range(1, n - 1), key=lambda i: abs(station_depths_m[i] - target_depth_m)
    )

    clamped = global_best_idx in (0, n - 1)
    if clamped:
        logger.warning(
            "L3 handoff selection: target depth %.2f m (Hs=%.2f m) landed on "
            "or outside the L3 grid boundary (nearest station %d/%d, "
            "depth=%.2f m) — clamped to nearest interior station %d "
            "(depth=%.2f m).",
            target_depth_m,
            hs_m,
            global_best_idx,
            n - 1,
            station_depths_m[global_best_idx],
            interior_best_idx,
            station_depths_m[interior_best_idx],
        )

    return HandoffSelection(
        handoff_depth_m=target_depth_m,
        source_level="L3",
        station_index=interior_best_idx,
        station_depth_m=station_depths_m[interior_best_idx],
        clamped=clamped,
    )


# ---------------------------------------------------------------------------
# Public API — T4A.10: runtime breaking-zone assertion, extending the
# original Step 4 QB scan lineage (LC-R2-5 — not a second parallel scanner).
# ---------------------------------------------------------------------------


def refine_handoff_with_qb(
    selection: HandoffSelection,
    station_qb: Sequence[float] | None,
    station_depths_m: Sequence[float] | None,
    *,
    transect_index: int = -1,
    hour: int | None = None,
    qb_threshold: float = _DEFAULT_QB_THRESHOLD,
    max_deepening_stations: int = 5,
) -> HandoffSelection:
    """Verify and, if necessary, refine one hour's handoff selection using
    SWAN's own QB (breaking fraction) output (T4A.10).

    Extends ``select_hourly_handoff()``'s lineage rather than re-scanning
    independently (LC-R2-5 / `rules/coding.md` DRY): it only ever adjusts a
    selection this module already produced.

    Asserts SWAN's QB at the selected station is ~0 (ADR-095 Amendment 2:
    "L3 stops before the surf zone by construction, so QB should be ~0 at
    *every* L3 point. A non-zero QB anywhere in L3 now indicates the handoff
    is too shallow and is a failure signal"). On violation, moves the sample
    SEAWARD (toward index 0 — deeper water, away from the shoreward grid
    boundary) until QB is clean or the search exhausts the interior stations
    / the deepening cap, in which case it logs ERROR with the greppable
    pattern ``"SWAN handoff"``, increments the ``/metrics`` counter, and
    raises ``HandoffBreakingError`` — the caller MUST fail the hour rather
    than serve a sample from a breaking cell (Do step 4: "Never fail
    silently and never serve a sample from a breaking cell").

    Parameters
    ----------
    selection:
        The output of ``select_hourly_handoff()`` for this transect/hour.
    station_qb:
        SWAN QB (breaking fraction, 0-1) at every station on the same L3
        CURVE ``selection`` was computed against, same order as
        ``station_depths_m``. ``None`` or empty ⇒ QB data unavailable this
        cycle — the selection is returned unchanged (matches the original
        Step 4 behaviour: transects without QB data are left alone).
    station_depths_m:
        Depths (m) at each station, same order as ``station_qb`` — needed to
        report ``actual_depth_m`` on a raised ``HandoffBreakingError`` and to
        keep ``HandoffSelection.station_depth_m`` consistent after a move.
    transect_index, hour:
        Identifying context for the ERROR log and the raised exception only.
    qb_threshold:
        QB value above which a station is considered contaminated by active
        breaking (default 0.05 — SURF-ZONE-MODEL-BRIEF §2.3.3).
    max_deepening_stations:
        Maximum number of stations to move seaward while searching for clean
        QB before giving up and raising (default 5).

    Returns
    -------
    HandoffSelection
        Unchanged when ``selection.source_level == "L2"`` (no L3 station to
        assert QB against), when QB data is unavailable, or when QB at the
        selected station is already clean. Otherwise a new selection at the
        deeper station found.

    Raises
    ------
    HandoffBreakingError
        No clean station was found within ``max_deepening_stations`` moves,
        or the search ran out of interior stations first.
    """
    if selection.source_level != "L3" or selection.station_index is None:
        # L2 reference is a fixed point outside any L3 grid — nothing to
        # assert QB against.
        return selection

    if not station_qb:
        # No QB data available this cycle — leave the selection unchanged,
        # matching the original Step 4 contract.
        return selection

    idx = selection.station_index
    if idx >= len(station_qb):
        logger.debug(
            "refine_handoff_with_qb: station_index %d out of range for "
            "station_qb (len=%d) — leaving selection unchanged.",
            idx,
            len(station_qb),
        )
        return selection

    qb_here = station_qb[idx]
    if qb_here <= qb_threshold:
        return selection

    # QB > threshold at the selected station — scan seaward (decreasing
    # index) for a clean interior station, never touching index 0 (the
    # offshore grid boundary — also off-limits per ADR-095 Amendment 2).
    moves = 0
    new_idx = idx
    while new_idx > 1 and moves < max_deepening_stations:
        new_idx -= 1
        moves += 1
        if station_qb[new_idx] <= qb_threshold:
            new_depth = (
                station_depths_m[new_idx]
                if station_depths_m and new_idx < len(station_depths_m)
                else selection.station_depth_m
            )
            logger.warning(
                "%s: transect %d hour=%s QB=%.3f > %.3f at station %d "
                "(depth=%.2f m) — moved seaward to station %d "
                "(depth=%.2f m, QB=%.3f).",
                _QB_VIOLATION_LOG_PREFIX,
                transect_index,
                hour,
                qb_here,
                qb_threshold,
                idx,
                selection.station_depth_m,
                new_idx,
                new_depth,
                station_qb[new_idx],
            )
            return HandoffSelection(
                handoff_depth_m=selection.handoff_depth_m,
                source_level="L3",
                station_index=new_idx,
                station_depth_m=new_depth,
                clamped=True,
            )

    # Exhausted the search without finding clean QB — fail the hour rather
    # than serve a sample from a breaking cell.
    actual_depth_m = (
        station_depths_m[idx]
        if station_depths_m and idx < len(station_depths_m)
        else selection.station_depth_m
    )
    final_qb = station_qb[new_idx] if new_idx < len(station_qb) else qb_here
    logger.error(
        "%s: transect %d hour=%s could not find a clean QB station within "
        "%d seaward move(s) from station %d (computed depth=%.2f m, "
        "actual depth=%.2f m, QB=%.3f > threshold %.3f). Failing this hour "
        "for this transect — never serving a sample from a breaking cell.",
        _QB_VIOLATION_LOG_PREFIX,
        transect_index,
        hour,
        max_deepening_stations,
        idx,
        selection.handoff_depth_m,
        actual_depth_m,
        final_qb,
        qb_threshold,
    )
    HANDOFF_QB_VIOLATIONS_TOTAL.labels(
        transect_index=str(transect_index),
    ).inc()
    raise HandoffBreakingError(
        f"{_QB_VIOLATION_LOG_PREFIX}: transect {transect_index} hour={hour} "
        f"no clean QB station found (QB={final_qb:.3f} > {qb_threshold:.3f})",
        transect_index=transect_index,
        hour=hour,
        computed_depth_m=selection.handoff_depth_m,
        actual_depth_m=actual_depth_m,
        qb=final_qb,
    )


# ---------------------------------------------------------------------------
# Self-test: HB Pier worked example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Exercise both halves of this module against a synthetic HB Pier setup.

    Expected result:
      - Shadow classification: some transects shadowed by the pier, some
        open (geometry-dependent count — see note in the original T2.3
        self-test this replaces).
      - Per-hour selection: a 1 m Hs hour resolves to ~1.8 m depth; a 4 m Hs
        hour resolves to ~7.1 m depth (T4A.9 Accept criteria).
      - QB assertion: an injected breaking-zone violation moves the sample
        seaward and logs the greppable "SWAN handoff" pattern; a violation
        with no clean station within the cap raises HandoffBreakingError.
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # --- HB Pier geometry ---------------------------------------------------
    PIER_SHORE_LAT = 33.6592
    PIER_SHORE_LON = -118.0027
    PIER_SEA_LAT = 33.6571
    PIER_SEA_LON = -118.0048
    PIER_DEPTH_M = 7.0

    def mock_bathy(lat: float, lon: float) -> float:
        if abs(lat - PIER_SEA_LAT) < 1e-4 and abs(lon - PIER_SEA_LON) < 1e-4:
            return PIER_DEPTH_M
        if abs(lat - PIER_SHORE_LAT) < 1e-4 and abs(lon - PIER_SHORE_LON) < 1e-4:
            return 0.5
        dist_lat = (lat - PIER_SHORE_LAT) * _M_PER_DEG_LAT
        dist_lon = (
            (lon - PIER_SHORE_LON)
            * _M_PER_DEG_LAT
            * math.cos(math.radians(PIER_SHORE_LAT))
        )
        dist_m = math.sqrt(dist_lat ** 2 + dist_lon ** 2)
        return min(15.0, max(0.0, dist_m * 0.02))

    pier = StructureConfig(
        {
            "type": "pier",
            "material": "permeable",
            "length_m": 300.0,
            "bearing_degrees": 225.0,
            "distance_m": 0.0,
            "coordinates": [
                [PIER_SHORE_LON, PIER_SHORE_LAT],
                [PIER_SEA_LON, PIER_SEA_LAT],
            ],
        }
    )

    BEACH_FACING_DEG = 210.0
    SHORE_BEARING_DEG = 300.0
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

    print("\n=== HB Pier Worked Example (shadow classification) ===\n")
    shadows = compute_transect_shadows(
        transect_origins=transect_origins,
        transect_bearings=transect_bearings,
        structures=[pier],
        beach_facing_degrees=BEACH_FACING_DEG,
        bathymetry_profile_fn=mock_bathy,
    )
    n_shadowed = sum(1 for r in shadows if r.is_shadowed)
    n_open = len(shadows) - n_shadowed
    print(f"Results: {len(shadows)} transects, {n_shadowed} shadowed, {n_open} open")

    ok = True
    if not (1 <= n_shadowed <= len(shadows) - 1):
        print(
            f"FAIL: expected 1-{len(shadows) - 1} shadowed transects, got "
            f"{n_shadowed}"
        )
        ok = False
    else:
        print(f"OK: {n_shadowed} shadowed, {n_open} open (geometry-dependent count)")

    print("\n=== Per-hour handoff selection (T4A.9) ===\n")
    # Synthetic L3 curve: 40 stations from 15 m (offshore) to 1.5 m (shoreward).
    synthetic_depths = [15.0 - i * (13.5 / 39.0) for i in range(40)]

    sel_1m = select_hourly_handoff(1.0, synthetic_depths)
    sel_4m = select_hourly_handoff(4.0, synthetic_depths)
    print(
        f"Hs=1.0 m -> target={1.3 * 1.0 / 0.73:.2f} m, "
        f"station_depth={sel_1m.station_depth_m:.2f} m, source={sel_1m.source_level}"
    )
    print(
        f"Hs=4.0 m -> target={1.3 * 4.0 / 0.73:.2f} m, "
        f"station_depth={sel_4m.station_depth_m:.2f} m, source={sel_4m.source_level}"
    )
    if not (1.5 <= sel_1m.station_depth_m <= 2.2):
        print(f"FAIL: 1 m swell should resolve near 1.8 m, got {sel_1m.station_depth_m:.2f} m")
        ok = False
    if not (6.5 <= sel_4m.station_depth_m <= 7.6):
        print(f"FAIL: 4 m swell should resolve near 7.1 m, got {sel_4m.station_depth_m:.2f} m")
        ok = False

    print("\n=== QB runtime assertion (T4A.10) ===\n")
    qb_profile = [0.0] * 40
    qb_profile[sel_1m.station_index] = 0.15  # inject a breaking-zone violation
    qb_profile[sel_1m.station_index - 1] = 0.01  # clean one station seaward
    refined = refine_handoff_with_qb(
        sel_1m, qb_profile, synthetic_depths, transect_index=0, hour=6
    )
    if refined.station_index != sel_1m.station_index - 1:
        print(
            f"FAIL: expected QB refinement to move seaward to station "
            f"{sel_1m.station_index - 1}, got {refined.station_index}"
        )
        ok = False
    else:
        print(
            f"OK: QB refinement moved station {sel_1m.station_index} -> "
            f"{refined.station_index} (see ERROR/WARNING log above for the "
            "greppable pattern)."
        )

    # Unrecoverable violation: contaminate every interior station.
    qb_all_breaking = [0.5] * 40
    try:
        refine_handoff_with_qb(
            sel_1m, qb_all_breaking, synthetic_depths, transect_index=0, hour=6
        )
        print("FAIL: expected HandoffBreakingError when no station is clean")
        ok = False
    except HandoffBreakingError as exc:
        print(f"OK: HandoffBreakingError raised as expected ({exc})")

    if ok:
        print("\nSelf-test PASSED")
    else:
        print("\nSelf-test FAILED — see details above")
        sys.exit(1)
