"""SWAN 3-level nested grid domain sizing and spot clustering.

Phase 14a/14d of SWAN-FIXES-PLAN. Computes physics-based grid domains for the
3-level nesting architecture:

  Level 1 (1 km): Continental shelf approach — all spots + GSFM shelf distance
  Level 2 (100 m): Nearshore refraction — all spots to 30m depth
  Level 3 (10 m): Surf zone — per cluster, shore to 15m depth

Spot clustering (14d): adjacent spots <500m apart share one Level 3 grid.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field

from weewx_clearskies_api.services.shelf_boundary import find_shelf_distance

logger = logging.getLogger(__name__)

_EARTH_RADIUS_KM = 6371.0


@dataclass
class GridDomain:
    """Computed bounding box for one SWAN grid level."""

    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    resolution_m: float
    level: int

    @property
    def ni(self) -> int:
        """Number of grid points in the longitude (x) direction."""
        width_m = _haversine_km(
            self.lat_min, self.lon_min, self.lat_min, self.lon_max
        ) * 1000
        return max(2, round(width_m / self.resolution_m) + 1)

    @property
    def nj(self) -> int:
        """Number of grid points in the latitude (y) direction."""
        height_m = _haversine_km(
            self.lat_min, self.lon_min, self.lat_max, self.lon_min
        ) * 1000
        return max(2, round(height_m / self.resolution_m) + 1)

    @property
    def cell_count(self) -> int:
        return self.ni * self.nj

    def estimated_runtime_s(self, cores: int = 6) -> float:
        """Estimated full 72h nonstationary run time in seconds."""
        return self.cell_count * (0.05 / (cores / 6))


@dataclass
class SpotCluster:
    """A group of adjacent surf spots sharing one Level 3 grid."""

    spot_ids: list[str]
    lats: list[float]
    lons: list[float]
    grid: GridDomain | None = None


@dataclass
class DomainSizing:
    """Complete 3-level domain sizing result."""

    level1: GridDomain
    level2: GridDomain
    level3_clusters: list[SpotCluster]

    @property
    def total_cells(self) -> int:
        cells = self.level1.cell_count + self.level2.cell_count
        for cluster in self.level3_clusters:
            if cluster.grid:
                cells += cluster.grid.cell_count
        return cells

    def estimated_runtime_s(self, cores: int = 6) -> float:
        """Total estimated runtime for a full 3-level run."""
        cost_per_cell = 0.05 / (cores / 6)
        return self.total_cells * cost_per_cell


# ---------------------------------------------------------------------------
# T4A.11 — widened L3 trigger + viability test (ADR-093 Amendment 2 §3, §4).
#
# L3 enables on a discovered manmade structure OR the operator's
# point_break/headland/bay_break topographic classification (Amendment 2 §3;
# structure-only was Amendment 1's trigger and could never enable L3 at a
# point break with no structure). The trigger is necessary but not
# sufficient: once sized, the grid is tested against the feature it exists
# for, and disabled (``grid=None``) with a mandatory INFO log when it does
# not reach that feature (Amendment 2 §4 / C4 — a too-shallow grid announces
# itself at runtime as breaking inside L3, but a too-seaward one is silently
# indistinguishable from "nothing here to model" without this log).
# ---------------------------------------------------------------------------

#: topographic_feature values that trigger L3 on their own (marine_config.py
#: _VALID_TOPOGRAPHIC_FEATURES minus "straight_beach" — a straight beach has
#: no 2D refraction feature for L3 to add over L2).
_TOPOGRAPHIC_L3_TRIGGERS: frozenset[str] = frozenset(
    {"point_break", "headland", "bay_break"}
)


def _cluster_structures(
    structures: list[dict] | None, cluster_spot_ids: list[str]
) -> list[dict]:
    """Filter *structures* to the ones belonging to this cluster.

    A structure dict may carry an optional ``spot_id`` key identifying which
    surf spot it belongs to (set by the apply-time chain in
    ``endpoints/setup.py``, T4A.3). When at least one structure in the list
    carries ``spot_id``, filtering is exact — a structure at one spot cannot
    spuriously trigger or size L3 for an unrelated cluster elsewhere on the
    coast. When NO structure carries ``spot_id`` (older caller shape, e.g. a
    test fixture predating T4A.11), every structure is returned unfiltered —
    this preserves prior behaviour but is imprecise: without spot
    attribution there is no way to tell which cluster a structure actually
    belongs to, and every cluster in the domain will see every structure.
    """
    if not structures:
        return []
    if not any("spot_id" in s for s in structures):
        return list(structures)
    return [s for s in structures if s.get("spot_id") in cluster_spot_ids]


def _l3_trigger_reason(
    cluster_structures: list[dict],
    l3_flags: list[str],
    classifications: list[str],
) -> tuple[bool, str]:
    """Decide whether L3 is triggered for one cluster, and why (for logging).

    ``"on"`` on any spot in the cluster forces enable regardless of
    structures/classification. ``"off"`` on every spot forces disable
    regardless of structures/classification — the operator override wins
    both ways, matching the pre-T4A.11 ``l3_enabled`` semantics. Otherwise
    ("auto" for at least one spot, not all off): enabled when the cluster
    has a structure OR any spot's classification is in
    ``_TOPOGRAPHIC_L3_TRIGGERS``.
    """
    if any(f == "on" for f in l3_flags):
        return True, "l3_enabled=on"
    if l3_flags and all(f == "off" for f in l3_flags):
        return False, "l3_enabled=off for every spot in cluster"

    has_structures = bool(cluster_structures)
    matching_features = sorted(
        {c for c in classifications if c in _TOPOGRAPHIC_L3_TRIGGERS}
    )
    if has_structures and matching_features:
        return True, f"structure(s) present + classification={matching_features}"
    if has_structures:
        return True, "structure(s) present"
    if matching_features:
        return True, f"classification={matching_features}"
    return (
        False,
        "no manmade structure and no point_break/headland/bay_break classification",
    )


def _l3_feature_points(
    cluster_structures: list[dict],
    cluster: SpotCluster,
) -> tuple[list[tuple[float, float]], str]:
    """The point(s) L3's grid must reach for the viability test.

    Structures: every structure coordinate belonging to this cluster (the
    same points ``smart_size_l3_grid()`` sizes around) — ``(lat, lon)``.

    Classification-only clusters (no structures): the cluster's own spot
    pin(s). There is no structure geometry to check reachability against,
    and inferring one (e.g. from shoreline curvature at a headland) is out
    of scope per ADR-093 Amendment 2 §3's scope boundary — contour shape
    analysis is explicitly not built. A classification-only grid is always
    sized to include the pin today, so this is a forward-looking safety net
    (it starts to matter once L3's shoreward edge can be tightened past the
    pin), not a live constraint.
    """
    points: list[tuple[float, float]] = []
    for struct in cluster_structures:
        for coord in struct.get("coordinates", []):
            if len(coord) >= 2:
                points.append((float(coord[1]), float(coord[0])))  # (lat, lon)
    if points:
        return points, "structure"
    return list(zip(cluster.lats, cluster.lons, strict=False)), "classified feature (pin)"


def _l3_viability_check(
    domain: GridDomain,
    feature_points: list[tuple[float, float]],
    feature_label: str,
    cluster_spot_ids: list[str],
) -> bool:
    """ADR-093 Amendment 2 §4 / T4A.11 Do step 4: verify the sized grid
    reaches the feature it was created for.

    Returns ``True`` (viable) when every point in *feature_points* falls
    within *domain*'s bounding box. On failure, logs INFO naming the
    feature and the approximate shortfall distance — this log is the fix
    for the "too far seaward" failure mode being silently indistinguishable
    from "nothing here to model" (C4).
    """
    unreached: list[tuple[float, float, float]] = []
    for lat, lon in feature_points:
        if domain.lat_min <= lat <= domain.lat_max and domain.lon_min <= lon <= domain.lon_max:
            continue
        clamped_lat = min(max(lat, domain.lat_min), domain.lat_max)
        clamped_lon = min(max(lon, domain.lon_min), domain.lon_max)
        shortfall_m = _haversine_km(lat, lon, clamped_lat, clamped_lon) * 1000.0
        unreached.append((lat, lon, shortfall_m))

    if not unreached:
        return True

    worst = max(unreached, key=lambda t: t[2])
    logger.info(
        "L3 viability test FAILED for cluster %s: %s unreachable by ~%.0f m "
        "(grid bbox [%.6f,%.6f - %.6f,%.6f]). L3 disabled for this cluster; "
        "handoff falls back to L2 at ~15m, same as an open beach.",
        cluster_spot_ids, feature_label, worst[2],
        domain.lat_min, domain.lon_min, domain.lat_max, domain.lon_max,
    )
    return False


def _l3_shoreward_edge_reach_m(max_struct_length_m: float | None) -> float | None:
    """⚠ UNAPPROVED — COORDINATOR CALL, NOT AN OPERATOR RULING. AWAITING
    OPERATOR ADJUDICATION (raised 2026-07-25 by the Phase 4A adversarial
    audit, finding F1).

    **Attribution correction.** An earlier version of this docstring said
    "operator ruling 2026-07-25." **That was false.** The operator never
    approved this reading. It was a coordinator decision, presented to the
    implementing agent inside a "Settled decisions ALL agents must apply"
    block alongside genuine operator decisions, which is how it acquired an
    attribution it never earned. `CLAUDE.md` names this exact failure:
    "Documents describing a superseded design are exactly how a wrong
    architectural change acquires a paper trail that looks legitimate."

    **This function's behaviour is therefore provisional and must not be
    treated as settled architecture.** Do not build on it, and do not cite
    it as precedent, until the operator has ruled.

    **The disputed call: the grid's shoreward edge is set by what the
    feature L3 exists for requires; the breaking-depth expression is a
    runtime CHECK, not a setup-time driver.** ADR-093 Amendment 2 §2 §4,
    read in order, say the opposite — size FROM the breaking-depth
    expression, THEN test the result against feature reach. Inverting that
    changes which criterion sets a model's grid boundary, which is trigger
    3 on `CLAUDE.md`'s list, and the coordinator was not entitled to decide
    it.

    Why, on the record: ADR-093 Amendment 2 §2, read literally, sizes L3's
    shoreward reach FROM the breaking-depth expression
    ``1.3 * Hs(hour) / gamma`` — specifically "the smallest value it ever
    produces for this spot's conditions," i.e. the spot's shallowest,
    smallest-swell breaking depth. **That expression is not computable.**
    It requires a minimum Hs, and none exists anywhere in this codebase —
    confirmed independently by both the coordinator and this agent via
    grep — and the operator has explicitly ruled out adding one (nobody
    should be asked to supply a minimum wave height). Only ``max_hs_m``,
    the LARGEST swell, is configured, and it is the wrong end for this
    purpose. So the literal reading has no implementable form, and the
    implementable reading — feature coverage drives the edge — is the one
    whose inputs (structure geometry, bathymetry) already exist at setup.

    **What still carries the breaking-depth check, so this is not a
    fudge:** T4A.9's per-hour handoff selection
    (``services/transect_handoff.py``) clamps a computed handoff to the
    nearest interior grid station and logs a WARNING when the true
    handoff would fall outside the grid. An hour whose breaking depth
    sits shallower than the grid reaches is therefore not silently
    mis-sampled — it is clamped and it announces itself. The
    breaking-depth expression still governs, but as a per-hour RUNTIME
    check with observability, rather than as a setup-time input that
    cannot be evaluated. If those warnings fire routinely at a spot, that
    is real evidence the grid should reach shallower, and it arrives as
    data rather than as a guessed constant.

    Args:
        max_struct_length_m: The longest structure in this cluster, in
            metres, or ``None`` when the cluster has no structures
            (classification-only trigger — point_break/headland/bay_break
            with no manmade structure).

    Returns:
        The additional shoreward margin (metres) the grid must reach past
        the feature's own shoreward-most point, or ``None`` when there is
        no feature geometry to size from.

        **Structure-triggered** (``max_struct_length_m`` given): returns
        ``3 * max_struct_length_m`` — the shadow-zone extent (ADR-093
        Amendment 1 §2 / PROVIDER-MANUAL §14.15 Amendment:
        ``shadow_length = structure_length + 2 * structure_length =
        3 * max_length``). This already reaches past the structure to
        capture the diffraction fill-in the shadow acquires with
        distance from the tip — the reason it needs no additional
        breaking-depth input to be a correct "feature coverage" answer.
        This is the SAME formula ``smart_size_l3_grid()`` already applied
        before this round; this function is now its single authoritative
        source rather than an inline duplicate.

        **Classification-only** (``max_struct_length_m`` is ``None``):
        there is no structure geometry to extend from, and inferring a
        feature position from shoreline or contour shape is out of scope
        (ADR-093 Amendment 2 §3's scope boundary, LC-R2-7 — contour
        curvature and orientation analysis are explicitly not built).
        Returns ``None`` — the caller keeps the pre-existing near-shore
        pin margin (``landward_km = 0.1``, ~100 m, unchanged since Era 1).
        This is NOT a new constant: LC-R1-4 forecloses inventing a
        minimum-wave-height config key or a new shallow-depth constant,
        and this function introduces neither — it reuses the margin that
        was already there for exactly the case where no feature geometry
        exists to replace it with.
    """
    if max_struct_length_m is None:
        return None
    return 3.0 * max_struct_length_m


def _size_l3_cluster(
    cluster: SpotCluster,
    avg_bearing: float,
    *,
    resolution_m: float,
    lateral_m: float,
    offshore_depth_m: float,
    offshore_distance_m: float | None,
    structures: list[dict] | None,
    spot_l3_configs: dict[str, str] | None,
    spot_topographic_features: dict[str, str] | None,
) -> GridDomain | None:
    """Trigger check → size → viability check for one L3 cluster (T4A.11).

    Shared by ``compute_domains()`` and ``compute_level3_domains()`` so the
    two public entry points cannot diverge on trigger/viability logic
    (``rules/coding.md`` DRY).

    Returns the sized ``GridDomain``, or ``None`` when the widened trigger
    (T4A.11 Do step 1 — structure OR operator classification, subject to the
    ``l3_enabled`` override) is not satisfied for this cluster, or the sized
    grid fails the viability test (ADR-093 Amendment 2 §4). Both paths log
    INFO with the reason. Callers (and ``swan_runner.py``'s L3 execution
    loop, P4A Round 2 Blocker 1) must treat ``grid=None`` as the single
    source of truth for "this cluster does not run L3" — do not re-derive
    the trigger decision downstream.
    """
    l3_flags = (
        [spot_l3_configs.get(sid, "auto") for sid in cluster.spot_ids]
        if spot_l3_configs is not None
        else []
    )
    cluster_structs = _cluster_structures(structures, cluster.spot_ids)
    classifications = (
        [spot_topographic_features.get(sid, "") for sid in cluster.spot_ids]
        if spot_topographic_features is not None
        else []
    )

    triggered, reason = _l3_trigger_reason(cluster_structs, l3_flags, classifications)
    if not triggered:
        logger.info("L3 skipped for cluster %s: %s", cluster.spot_ids, reason)
        return None

    # _compute_level3_grid() (and smart_size_l3_grid() beneath it, for the
    # structure-triggered path) already applies the resolved shoreward-edge
    # criterion internally — see _l3_shoreward_edge_reach_m()'s docstring
    # for the Blocker 2 ruling (feature coverage drives; breaking depth is
    # a runtime check carried by T4A.9's clamp-and-WARNING).
    grid = _compute_level3_grid(
        cluster,
        avg_bearing,
        resolution_m=resolution_m,
        lateral_m=lateral_m,
        offshore_depth_m=offshore_depth_m,
        offshore_distance_m=offshore_distance_m,
        structures=cluster_structs or None,
    )

    feature_points, feature_label = _l3_feature_points(cluster_structs, cluster)
    if not _l3_viability_check(grid, feature_points, feature_label, cluster.spot_ids):
        return None

    logger.info("L3 enabled for cluster %s: %s", cluster.spot_ids, reason)
    return grid


def compute_domains(
    spot_locations: list[dict],
    *,
    level1_resolution_m: float = 1000.0,
    level2_resolution_m: float = 100.0,
    level3_resolution_m: float = 10.0,
    level1_margin_km: float = 5.0,
    level2_margin_km: float = 2.0,
    level3_lateral_m: float = 250.0,
    level2_offshore_depth_m: float = 30.0,
    level3_offshore_depth_m: float = 15.0,
    cluster_distance_m: float = 500.0,
    structures: list[dict] | None = None,
    spot_l3_configs: dict[str, str] | None = None,
    spot_topographic_features: dict[str, str] | None = None,
) -> DomainSizing:
    """Compute 3-level nested grid domains from spot locations.

    Args:
        spot_locations: List of dicts with keys: id, lat, lon, beach_facing_degrees.
        level1_resolution_m: Level 1 grid spacing (default 1000m = 1km).
        level2_resolution_m: Level 2 grid spacing (default 100m).
        level3_resolution_m: Level 3 grid spacing (default 10m).
        level1_margin_km: Extra margin beyond shelf edge for Level 1 (default 5km).
        level2_margin_km: Lateral margin for Level 2 (default 2km).
        level3_lateral_m: Alongshore extent each side of pin for Level 3 (default 250m).
        level2_offshore_depth_m: Offshore boundary depth for Level 2 (default 30m).
        level3_offshore_depth_m: Offshore boundary depth for Level 3 (default 15m).
        cluster_distance_m: Max pin-to-pin distance for spot clustering (default 500m).
        structures: Optional list of coastal structure dicts (from Overpass/wizard
            discovery).  Each dict may carry an optional ``spot_id`` key (T4A.11) so
            a structure at one spot is not applied to an unrelated cluster.  When
            present for a cluster, L3 uses structure-based smart sizing (T3.2)
            instead of pin-cluster sizing.
        spot_l3_configs: Optional mapping of spot_id → l3_enabled value ("auto",
            "on", "off").  ``"on"`` forces L3 on; ``"off"`` for every spot in a
            cluster forces it off.  Otherwise ("auto"), T4A.11's widened trigger
            applies — see ``spot_topographic_features``.
        spot_topographic_features: Optional mapping of spot_id → operator
            classification (``"point_break"``, ``"headland"``, ``"bay_break"``,
            ``"straight_beach"``). T4A.11 (ADR-093 Amendment 2 §3): a cluster with
            no structures but a point_break/headland/bay_break classification on
            any of its spots still triggers L3 — structure-only was the pre-T4A.11
            trigger and could never enable L3 at a point break.

    Returns:
        DomainSizing with all three grid levels computed. Each cluster's
        ``grid`` is ``None`` when T4A.11's trigger is not satisfied, or when
        the sized grid fails the viability test (ADR-093 Amendment 2 §4) —
        see ``_size_l3_cluster()``.
    """
    if not spot_locations:
        raise ValueError("compute_domains: no spot locations provided")

    lats = [s["lat"] for s in spot_locations]
    lons = [s["lon"] for s in spot_locations]
    bearings = [float(s.get("beach_facing_degrees", 270.0)) for s in spot_locations]

    # --- Level 1: Coarse grid (1 km) ---
    level1 = _compute_level1(
        lats, lons,
        resolution_m=level1_resolution_m,
        margin_km=level1_margin_km,
        spot_locations=spot_locations,
    )

    # --- Level 2: Nearshore grid (100 m) ---
    # T4A.3: size from the actual 30m depth contour when the caller supplies
    # per-spot contour distances (real apply-time chain via marine_setup.py).
    # Falls back to an ESTIMATE-only distance (clearly logged as such) when
    # no contour data is available — the only legitimate caller of that path
    # is the pre-apply /marine/compute-estimate preview, before any CUDEM
    # download has happened.
    spot_contour_30m: dict[str, float] = {}
    for s in spot_locations:
        d = s.get("contour_30m_distance_m")
        if d is not None and d > 0:
            spot_contour_30m[s["id"]] = float(d)
    contour_30m_max = max(spot_contour_30m.values()) if spot_contour_30m else None

    level2 = _compute_level2(
        lats, lons, bearings,
        resolution_m=level2_resolution_m,
        margin_km=level2_margin_km,
        offshore_depth_m=level2_offshore_depth_m,
        contour_distance_m=contour_30m_max,
    )

    # --- Level 3: Surf zone grids (10 m) — per cluster ---
    clusters = _cluster_spots(spot_locations, cluster_distance_m)
    avg_bearing = sum(bearings) / len(bearings)

    # Build a lookup of per-spot offshore distances (from profile data).
    # Each spot may have "offshore_distance_m" = distance from shore to the
    # 15m depth contour, extracted from the cached bidirectional CUDEM profile.
    spot_offshore: dict[str, float] = {}
    for s in spot_locations:
        d = s.get("offshore_distance_m")
        if d is not None and d > 0:
            spot_offshore[s["id"]] = float(d)

    for cluster in clusters:
        # Use the maximum offshore distance from any spot in this cluster.
        # This ensures the grid reaches the 15m contour for all spots.
        cluster_distances = [
            spot_offshore[sid] for sid in cluster.spot_ids if sid in spot_offshore
        ]
        cluster_offshore_m = max(cluster_distances) if cluster_distances else None

        # T4A.11 — trigger (structure OR classification, subject to
        # l3_enabled) → size → viability check, all in one place.
        cluster.grid = _size_l3_cluster(
            cluster,
            avg_bearing,
            resolution_m=level3_resolution_m,
            lateral_m=level3_lateral_m,
            offshore_depth_m=level3_offshore_depth_m,
            offshore_distance_m=cluster_offshore_m,
            structures=structures,
            spot_l3_configs=spot_l3_configs,
            spot_topographic_features=spot_topographic_features,
        )

    return DomainSizing(level1=level1, level2=level2, level3_clusters=clusters)


# ---------------------------------------------------------------------------
# Public staged entry points (Phase 4A, T4A.3; lead call LC-3/LC-11).
#
# ``compute_domains()`` above sizes all three levels in one call from
# per-spot data the CALLER must already have (``offshore_distance_m`` for
# L3, ``contour_30m_distance_m`` for L2). That single-call shape is correct
# for its one remaining caller (the pre-apply ``/marine/compute-estimate``
# preview, which has no CUDEM data at all and accepts ESTIMATE fallbacks).
#
# The REAL apply-time chain (``services/marine_setup.py``) cannot supply
# those distances up front — it must size L1 first (no CUDEM needed), THEN
# download COARSE bathymetry for L1's bbox, THEN search it for the 30m
# contour, THEN size L2, THEN download MEDIUM bathymetry for L2's bbox,
# THEN search it for the 15m contour, THEN cluster + size L3. These public
# functions expose each stage independently so marine_setup.py can
# interleave sizing and downloading in the correct order — WITHOUT ever
# resizing a grid after it's been computed (LC-11: all inputs that affect
# sizing are passed in at computation time, nothing is applied after the
# fact).
# ---------------------------------------------------------------------------


def compute_level1_domain(
    spot_locations: list[dict],
    *,
    resolution_m: float = 1000.0,
    margin_km: float = 5.0,
) -> GridDomain:
    """Size the L1 grid alone, from spot locations + GSFM shelf data only.

    No CUDEM download is needed for this stage (Do step 1-2) — L1's bbox
    IS the COARSE download's target area, so this must run before any
    bathymetry download happens.

    Args:
        spot_locations: List of dicts with keys: id, lat, lon,
            beach_facing_degrees.
    """
    if not spot_locations:
        raise ValueError("compute_level1_domain: no spot locations provided")
    lats = [s["lat"] for s in spot_locations]
    lons = [s["lon"] for s in spot_locations]
    return _compute_level1(
        lats, lons,
        resolution_m=resolution_m,
        margin_km=margin_km,
        spot_locations=spot_locations,
    )


def compute_level2_domain(
    spot_locations: list[dict],
    *,
    resolution_m: float = 100.0,
    margin_km: float = 2.0,
    offshore_depth_m: float = 30.0,
    contour_30m_distance_m: float | None = None,
) -> GridDomain:
    """Size the L2 grid from the actual 30m depth contour (Do step 4).

    ``contour_30m_distance_m`` must be the MAX per-spot 30m-contour distance
    found by searching the COARSE-downloaded CUDEM grid along each spot's
    own bearing (LC-10) — computed by the caller (marine_setup.py) via
    ``enrichment.bathymetry.find_depth_contour_distance()`` BEFORE calling
    this function. Passing ``None`` produces an ESTIMATE-only grid (see
    ``_compute_level2`` docstring) — never valid for a real SWAN run.

    Args:
        spot_locations: List of dicts with keys: id, lat, lon,
            beach_facing_degrees.
    """
    if not spot_locations:
        raise ValueError("compute_level2_domain: no spot locations provided")
    lats = [s["lat"] for s in spot_locations]
    lons = [s["lon"] for s in spot_locations]
    bearings = [float(s.get("beach_facing_degrees", 270.0)) for s in spot_locations]
    return _compute_level2(
        lats, lons, bearings,
        resolution_m=resolution_m,
        margin_km=margin_km,
        offshore_depth_m=offshore_depth_m,
        contour_distance_m=contour_30m_distance_m,
    )


def compute_level3_domains(
    spot_locations: list[dict],
    *,
    resolution_m: float = 10.0,
    lateral_m: float = 250.0,
    offshore_depth_m: float = 15.0,
    cluster_distance_m: float = 500.0,
    structures: list[dict] | None = None,
    spot_l3_configs: dict[str, str] | None = None,
    contour_15m_by_spot: dict[str, float] | None = None,
    spot_topographic_features: dict[str, str] | None = None,
) -> list[SpotCluster]:
    """Cluster spots and size each L3 grid from the actual 15m depth contour
    — Do steps 6-7, gated by T4A.11's widened trigger and viability test.

    ``contour_15m_by_spot`` maps spot id -> 15m-contour distance found by
    searching the MEDIUM-downloaded CUDEM grid along that spot's own bearing
    (LC-10) — computed by the caller (marine_setup.py) BEFORE calling this
    function. Each cluster uses the MAX distance across its member spots.
    Spots absent from the mapping do not contribute to the max (but are
    still part of the cluster/grid extent via pin position) — the caller is
    responsible for logging a WARNING when a spot's contour search failed
    (per "Silent skipping of configured inputs is a bug pattern").

    Args:
        spot_locations: List of dicts with keys: id, lat, lon,
            beach_facing_degrees.
        structures: Optional list of structure dicts. Each may carry an
            optional ``spot_id`` key (T4A.11) so a structure at one spot is
            not applied to an unrelated cluster.
        spot_l3_configs: Optional mapping of spot_id → l3_enabled
            ("auto"/"on"/"off"). See ``compute_domains()`` for semantics.
        spot_topographic_features: Optional mapping of spot_id → operator
            classification. T4A.11's widened trigger — see
            ``compute_domains()`` for the full explanation.

    Returns:
        List of ``SpotCluster``. Each cluster's ``grid`` is ``None`` when
        T4A.11's trigger is not satisfied, or when the sized grid fails the
        viability test (ADR-093 Amendment 2 §4) — see ``_size_l3_cluster()``.
    """
    if not spot_locations:
        raise ValueError("compute_level3_domains: no spot locations provided")

    clusters = _cluster_spots(spot_locations, cluster_distance_m)
    bearings = [float(s.get("beach_facing_degrees", 270.0)) for s in spot_locations]
    avg_bearing = sum(bearings) / len(bearings)
    contour_by_spot = contour_15m_by_spot or {}

    for cluster in clusters:
        cluster_distances = [
            contour_by_spot[sid] for sid in cluster.spot_ids if sid in contour_by_spot
        ]
        cluster_offshore_m = max(cluster_distances) if cluster_distances else None

        cluster.grid = _size_l3_cluster(
            cluster,
            avg_bearing,
            resolution_m=resolution_m,
            lateral_m=lateral_m,
            offshore_depth_m=offshore_depth_m,
            offshore_distance_m=cluster_offshore_m,
            structures=structures,
            spot_l3_configs=spot_l3_configs,
            spot_topographic_features=spot_topographic_features,
        )

    return clusters


def _compute_level1(
    lats: list[float],
    lons: list[float],
    *,
    resolution_m: float,
    margin_km: float,
    spot_locations: list[dict] | None = None,
) -> GridDomain:
    """Level 1: all spots extent + margin + GSFM shelf distance offshore."""
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # Get shelf distance for domain sizing
    shelf_dist_km = find_shelf_distance(center_lat, center_lon)
    if shelf_dist_km is None:
        shelf_dist_km = 30.0  # Fallback: assume 30 km shelf width
        logger.warning(
            "GSFM data unavailable; using fallback shelf distance of %.0f km",
            shelf_dist_km,
        )

    # Offshore extent: shelf distance + 10 km past shelf edge (per research brief §5)
    offshore_km = shelf_dist_km + 10.0

    # Lateral extent: all spots + margin each side
    lat_spread_km = (max(lats) - min(lats)) * 111.0
    lon_spread_km = (max(lons) - min(lons)) * 111.0 * math.cos(math.radians(center_lat))
    lateral_km = max(lat_spread_km, lon_spread_km) / 2 + margin_km

    # Convert to degrees
    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(center_lat))

    lat_margin_deg = lateral_km / km_per_deg_lat
    lon_margin_deg = lateral_km / km_per_deg_lon
    offshore_deg = offshore_km / km_per_deg_lon

    # Extend offshore in the beach-facing direction, lateral perpendicular
    avg_bearing = sum(
        float(s.get("beach_facing_degrees", 270.0)) for s in spot_locations
    ) / len(spot_locations) if spot_locations else 270.0
    bearing_rad = math.radians(avg_bearing)

    offshore_dlat = offshore_km * math.cos(bearing_rad) / km_per_deg_lat
    offshore_dlon = offshore_km * math.sin(bearing_rad) / km_per_deg_lon
    landward_km = 1.0
    landward_dlat = landward_km * math.cos(bearing_rad + math.pi) / km_per_deg_lat
    landward_dlon = landward_km * math.sin(bearing_rad + math.pi) / km_per_deg_lon

    offshore_lat = center_lat + offshore_dlat
    offshore_lon = center_lon + offshore_dlon
    landward_lat = center_lat + landward_dlat
    landward_lon = center_lon + landward_dlon

    all_lats = lats + [offshore_lat, landward_lat]
    all_lons = lons + [offshore_lon, landward_lon]

    lat_min = min(all_lats) - lat_margin_deg
    lat_max = max(all_lats) + lat_margin_deg
    lon_min = min(all_lons) - lon_margin_deg
    lon_max = max(all_lons) + lon_margin_deg

    return GridDomain(
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        resolution_m=resolution_m, level=1,
    )


def _compute_level2(
    lats: list[float],
    lons: list[float],
    beach_facing_degrees: list[float],
    *,
    resolution_m: float,
    margin_km: float,
    offshore_depth_m: float,
    contour_distance_m: float | None = None,
) -> GridDomain:
    """Level 2: extends OFFSHORE from spots to the actual 30m depth contour.

    Uses beach_facing_degrees to determine which direction is offshore.

    T4A.3 fix: previously hardcoded ``offshore_km = 6.0`` regardless of
    ``offshore_depth_m`` — wrong by a wide margin on both steep Pacific
    shelves (~2km to 30m) and gentle Gulf shelves (~30km to 30m). Now sized
    from ``contour_distance_m`` — the MAX real 30m-contour distance across
    all configured spots (per-spot bearing search on downloaded CUDEM data,
    computed by ``services/marine_setup.py`` before calling this function;
    see ``rules/clearskies-process.md`` "Grid sizing must come from actual
    data, not illustrative estimates").

    When ``contour_distance_m`` is ``None`` (no CUDEM download has happened
    yet — the only legitimate caller is the pre-apply
    ``/marine/compute-estimate`` preview), falls back to a clearly-logged
    ESTIMATE distance. This estimate must NEVER be used to size a real SWAN
    grid — the apply-time chain (T4A.3) always supplies real contour data.
    """
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # Average beach-facing bearing (offshore direction)
    avg_bearing = sum(beach_facing_degrees) / len(beach_facing_degrees)
    bearing_rad = math.radians(avg_bearing)

    if contour_distance_m is not None and contour_distance_m > 0:
        offshore_km = contour_distance_m / 1000.0 + 0.5  # +500m margin past the 30m contour
    else:
        offshore_km = 6.0  # ESTIMATE fallback only — see docstring
        logger.warning(
            "Level 2 grid: no actual %.0fm depth contour distance available — "
            "using ESTIMATE fallback %.1f km offshore. This is only valid for "
            "the pre-apply compute-estimate preview; the T4A.3 apply-time "
            "chain always supplies contour_distance_m for real SWAN runs.",
            offshore_depth_m, offshore_km,
        )
    landward_km = 0.5

    # Lateral: spots spread + margin each side
    lateral_km = margin_km

    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(center_lat))

    # Compute offshore and landward offsets in lat/lon using bearing
    offshore_dlat = offshore_km * math.cos(bearing_rad) / km_per_deg_lat
    offshore_dlon = offshore_km * math.sin(bearing_rad) / km_per_deg_lon
    landward_dlat = landward_km * math.cos(bearing_rad + math.pi) / km_per_deg_lat
    landward_dlon = landward_km * math.sin(bearing_rad + math.pi) / km_per_deg_lon

    # Lateral offsets (perpendicular to bearing)
    perp_rad = bearing_rad + math.pi / 2
    lateral_dlat = lateral_km * abs(math.cos(perp_rad)) / km_per_deg_lat
    lateral_dlon = lateral_km * abs(math.sin(perp_rad)) / km_per_deg_lon

    # Build bbox from spots + directional offsets
    offshore_lat = center_lat + offshore_dlat
    offshore_lon = center_lon + offshore_dlon
    landward_lat = center_lat + landward_dlat
    landward_lon = center_lon + landward_dlon

    all_lats = lats + [offshore_lat, landward_lat]
    all_lons = lons + [offshore_lon, landward_lon]

    lat_min = min(all_lats) - lateral_dlat
    lat_max = max(all_lats) + lateral_dlat
    lon_min = min(all_lons) - lateral_dlon
    lon_max = max(all_lons) + lateral_dlon

    return GridDomain(
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        resolution_m=resolution_m, level=2,
    )


def _cluster_spots(
    spots: list[dict], max_distance_m: float
) -> list[SpotCluster]:
    """Group adjacent spots into clusters for shared Level 3 grids.

    Algorithm:
    1. Sort spots along the coast (by longitude for primarily N-S coasts,
       by latitude for E-W coasts — use the dominant spread axis).
    2. Walk sorted list; consecutive spots within max_distance_m form a cluster.
    """
    if len(spots) <= 1:
        return [
            SpotCluster(
                spot_ids=[s["id"] for s in spots],
                lats=[s["lat"] for s in spots],
                lons=[s["lon"] for s in spots],
            )
        ]

    # Determine dominant axis (sort by whichever has more spread)
    lat_spread = max(s["lat"] for s in spots) - min(s["lat"] for s in spots)
    lon_spread = max(s["lon"] for s in spots) - min(s["lon"] for s in spots)

    if lat_spread >= lon_spread:
        sorted_spots = sorted(spots, key=lambda s: s["lat"])
    else:
        sorted_spots = sorted(spots, key=lambda s: s["lon"])

    clusters: list[SpotCluster] = []
    current_cluster = SpotCluster(
        spot_ids=[sorted_spots[0]["id"]],
        lats=[sorted_spots[0]["lat"]],
        lons=[sorted_spots[0]["lon"]],
    )

    for i in range(1, len(sorted_spots)):
        prev = sorted_spots[i - 1]
        curr = sorted_spots[i]
        dist_m = _haversine_km(prev["lat"], prev["lon"], curr["lat"], curr["lon"]) * 1000

        if dist_m <= max_distance_m:
            current_cluster.spot_ids.append(curr["id"])
            current_cluster.lats.append(curr["lat"])
            current_cluster.lons.append(curr["lon"])
        else:
            clusters.append(current_cluster)
            current_cluster = SpotCluster(
                spot_ids=[curr["id"]],
                lats=[curr["lat"]],
                lons=[curr["lon"]],
            )

    clusters.append(current_cluster)
    return clusters


def smart_size_l3_grid(
    cluster: SpotCluster,
    beach_facing_degrees: float,
    structures: list[dict],
    *,
    resolution_m: float = 10.0,
    pad_m: float = 100.0,
) -> GridDomain | None:
    """Compute a structure-based Level 3 grid for a cluster.

    When coastal structures (jetty, pier, breakwater, etc.) are present near
    the cluster, sizes the L3 bbox from structure positions + shadow zone
    rather than the pin cluster centre.

    Shadow zone extent (PROVIDER-MANUAL §14.15 Amendment):
      shadow_length = structure_length + 2 × structure_length = 3 × max_length
      Direction: shoreward (opposite beach_facing_degrees) — waves propagate
      toward shore; the shadow extends behind the structure toward shore.

    Args:
        cluster: Spot cluster with lat/lon pin positions.
        beach_facing_degrees: Bearing from shore toward the open ocean (0=N,
            90=E, 180=S, 270=W).  The shoreward direction is bearing+180.
        structures: List of structure dicts with optional ``"coordinates"``
            key (list of [lon, lat] pairs) from Overpass/wizard discovery.
        resolution_m: L3 grid resolution (default 10 m).
        pad_m: Padding added to all sides of the computed bbox (default 100 m).

    Returns:
        GridDomain sized to the structure shadow zone, or None if no structure
        has usable coordinate data (caller should fall back to pin-cluster sizing).
    """
    # Collect all structure coordinate points (lon, lat)
    all_lons: list[float] = []
    all_lats: list[float] = []
    for struct in structures:
        for coord in struct.get("coordinates", []):
            if len(coord) >= 2:
                all_lons.append(float(coord[0]))
                all_lats.append(float(coord[1]))

    if not all_lons:
        return None  # no coordinate data — caller falls back to pin sizing

    center_lat = sum(cluster.lats) / len(cluster.lats)
    center_lon = sum(cluster.lons) / len(cluster.lons)

    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(center_lat))

    # Bearing toward ocean
    bearing_rad = math.radians(beach_facing_degrees)
    # Bearing toward shore (shoreward direction)
    shoreward_rad = math.radians(beach_facing_degrees + 180.0)

    # Project each structure coordinate onto the beach_facing_degrees axis to
    # find the offshore (max) and shoreward (min) extents.
    def _proj_offshore(lat: float, lon: float) -> float:
        """Signed projection onto beach-facing (offshore) axis, in km."""
        return (
            lat * km_per_deg_lat * math.cos(bearing_rad)
            + lon * km_per_deg_lon * math.sin(bearing_rad)
        )

    projections = [_proj_offshore(lat, lon) for lat, lon in zip(all_lats, all_lons)]
    proj_max = max(projections)  # most offshore point
    proj_min = min(projections)  # most shoreward point

    # Project onto perpendicular axis for lateral extent
    perp_rad = bearing_rad + math.pi / 2

    def _proj_lateral(lat: float, lon: float) -> float:
        return (
            lat * km_per_deg_lat * math.cos(perp_rad)
            + lon * km_per_deg_lon * math.sin(perp_rad)
        )

    lat_projs = [_proj_lateral(lat, lon) for lat, lon in zip(all_lats, all_lons)]
    lat_proj_min = min(lat_projs)
    lat_proj_max = max(lat_projs)

    # Estimate maximum structure length: haversine between most-separated coords.
    # Fast approximation — use bbox diagonal in km.
    struct_lat_span_km = (max(all_lats) - min(all_lats)) * km_per_deg_lat
    struct_lon_span_km = (max(all_lons) - min(all_lons)) * km_per_deg_lon
    max_struct_length_km = math.sqrt(struct_lat_span_km ** 2 + struct_lon_span_km ** 2)
    if max_struct_length_km < 0.010:
        # Structure too small to estimate from bbox — use minimum 20m
        max_struct_length_km = 0.020

    # Shadow zone shoreward of the structure (Blocker 2, RESOLVED — see
    # _l3_shoreward_edge_reach_m()'s docstring for the full ruling and
    # reasoning). This IS the feature-coverage answer for a
    # structure-triggered cluster. Always non-None here — this branch only
    # runs when structures is non-empty, so a length is always supplied.
    shadow_km = (_l3_shoreward_edge_reach_m(max_struct_length_km * 1000.0) or 0.0) / 1000.0
    pad_km = pad_m / 1000.0

    # Reference point for converting projections back to lat/lon.
    # Pick a point on the offshore axis at proj_max + pad.
    offshore_total_km = (proj_max - _proj_offshore(center_lat, center_lon)) + pad_km
    shoreward_total_km = (
        _proj_offshore(center_lat, center_lon) - proj_min + shadow_km + pad_km
    )
    lateral_plus_km = (lat_proj_max - _proj_lateral(center_lat, center_lon)) + pad_km
    lateral_minus_km = (
        _proj_lateral(center_lat, center_lon) - lat_proj_min + pad_km
    )

    # Build the four corner offsets from the cluster centre
    offshore_dlat = offshore_total_km * math.cos(bearing_rad) / km_per_deg_lat
    offshore_dlon = offshore_total_km * math.sin(bearing_rad) / km_per_deg_lon
    shoreward_dlat = shoreward_total_km * math.cos(shoreward_rad) / km_per_deg_lat
    shoreward_dlon = shoreward_total_km * math.sin(shoreward_rad) / km_per_deg_lon
    lateral_p_dlat = lateral_plus_km * math.cos(perp_rad) / km_per_deg_lat
    lateral_p_dlon = lateral_plus_km * math.sin(perp_rad) / km_per_deg_lon
    lateral_m_dlat = lateral_minus_km * math.cos(perp_rad + math.pi) / km_per_deg_lat
    lateral_m_dlon = lateral_minus_km * math.sin(perp_rad + math.pi) / km_per_deg_lon

    corner_lats = [
        center_lat + offshore_dlat + lateral_p_dlat,
        center_lat + offshore_dlat + lateral_m_dlat,
        center_lat + shoreward_dlat + lateral_p_dlat,
        center_lat + shoreward_dlat + lateral_m_dlat,
    ]
    corner_lons = [
        center_lon + offshore_dlon + lateral_p_dlon,
        center_lon + offshore_dlon + lateral_m_dlon,
        center_lon + shoreward_dlon + lateral_p_dlon,
        center_lon + shoreward_dlon + lateral_m_dlon,
    ]

    logger.info(
        "L3 smart sizing for cluster %s: struct %.0f m, shadow %.0f m → "
        "bbox [%.5f,%.5f – %.5f,%.5f]",
        cluster.spot_ids,
        max_struct_length_km * 1000,
        shadow_km * 1000,
        min(corner_lats), min(corner_lons),
        max(corner_lats), max(corner_lons),
    )

    return GridDomain(
        lat_min=min(corner_lats),
        lat_max=max(corner_lats),
        lon_min=min(corner_lons),
        lon_max=max(corner_lons),
        resolution_m=resolution_m,
        level=3,
    )


def _assert_grid_contains_points(
    domain: GridDomain,
    points: list[tuple[float, float]],
    cluster_spot_ids: list[str],
) -> None:
    """Log an ERROR (never raise — this is a post-construction invariant check,
    not a request-path failure) for any ``(lat, lon)`` in *points* that falls
    outside *domain*'s bounding box.

    T4A.3 (coordinator finding, 2026-07-25 librewxr diagnostic): a live SWAN
    run showed ``compute_spot_transect`` clipping a spot's deep transect
    endpoint from 2440m to 950m because the L3 grid didn't reach far enough
    — a silent 60% data loss logged only at WARNING inside a different file
    (``swan_formats.py``, not owned by this task). Per
    ``rules/clearskies-process.md`` "Silent skipping of configured inputs is
    a bug pattern", grid geometry is fixed at setup time (LC-11), so THIS is
    where containment must be verified — not detected later at runtime.
    Callers (``_compute_level3_grid``/``smart_size_l3_grid``) already fold
    every supplied deep-transect point into the bbox corner set, so this
    should never fire; it exists as defense-in-depth against a future
    regression that omits a point from that corner set.
    """
    for spot_id, (lat, lon) in zip(cluster_spot_ids, points, strict=False):
        if not (domain.lat_min <= lat <= domain.lat_max and domain.lon_min <= lon <= domain.lon_max):
            logger.error(
                "L3 grid for cluster %s does NOT contain spot %r's deep "
                "transect endpoint (%.6f, %.6f) — grid bbox is "
                "[%.6f,%.6f – %.6f,%.6f]. This means compute_spot_transect() "
                "will silently clip this spot's transect at runtime (data "
                "loss). Grid geometry is fixed at setup time — this is a "
                "sizing bug, not a runtime condition to tolerate.",
                cluster_spot_ids, spot_id, lat, lon,
                domain.lat_min, domain.lon_min, domain.lat_max, domain.lon_max,
            )


def _compute_level3_grid(
    cluster: SpotCluster,
    beach_facing_degrees: float,
    *,
    resolution_m: float,
    lateral_m: float,
    offshore_depth_m: float,
    offshore_distance_m: float | None = None,
    structures: list[dict] | None = None,
    spot_deep_points: list[tuple[float, float]] | None = None,
) -> GridDomain:
    """Compute one Level 3 grid for a spot cluster.

    The grid extends from the coastline (100m landward of the pin) to the
    15m depth contour offshore, using the actual distance from the cached
    bathymetric profile.  If no profile data is available, falls back to
    2.5 km (conservative estimate for typical continental shelves).

    The research brief (§5, Level 3) specifies "shore to 15m depth" — NOT
    a fixed distance.  The ~1 km estimate in the brief was illustrative;
    actual 15m depth distance varies (e.g., 2.35 km at HB Pier).

    Uses beach_facing_degrees to orient the grid offshore.
    Lateral: 250m each side along coast.

    Args:
        spot_deep_points: T4A.3 — each spot's OWN transect deep endpoint
            ``(lat, lon)``, computed from that spot's actual coastline along
            its own bearing at its own real 15m-contour distance (+ margin).
            When supplied, these points are folded directly into the bbox's
            corner-point set so the grid is GUARANTEED to contain every
            spot's transect — not just the cluster-centroid-derived offshore
            point, which can miss individual spots when the coastline is
            offset from the pin or multiple spots have different bearings.
            A containment assertion (ERROR-logged, not raised) runs
            afterward as defense-in-depth.
    """
    # T3.2 — when structures with coordinates are provided, delegate to
    # structure-based smart sizing (shadow zone extent), then union in the
    # per-spot deep transect points so structure-based sizing can't clip a
    # transect either.
    if structures:
        smart = smart_size_l3_grid(
            cluster,
            beach_facing_degrees,
            structures,
            resolution_m=resolution_m,
            pad_m=100.0,
        )
        if smart is not None:
            if spot_deep_points:
                deep_lats = [p[0] for p in spot_deep_points]
                deep_lons = [p[1] for p in spot_deep_points]
                smart = GridDomain(
                    lat_min=min(smart.lat_min, *deep_lats),
                    lat_max=max(smart.lat_max, *deep_lats),
                    lon_min=min(smart.lon_min, *deep_lons),
                    lon_max=max(smart.lon_max, *deep_lons),
                    resolution_m=smart.resolution_m,
                    level=smart.level,
                )
                _assert_grid_contains_points(smart, spot_deep_points, cluster.spot_ids)
            return smart

    center_lat = sum(cluster.lats) / len(cluster.lats)
    center_lon = sum(cluster.lons) / len(cluster.lons)

    bearing_rad = math.radians(beach_facing_degrees)

    if offshore_distance_m is not None and offshore_distance_m > 0:
        offshore_km = offshore_distance_m / 1000.0 + 0.1  # +100m margin past 15m contour
    else:
        offshore_km = 2.5  # ESTIMATE fallback only — see _compute_level2 docstring
        logger.warning(
            "Level 3 grid for cluster %s: no actual 15m depth contour distance "
            "available — using ESTIMATE fallback %.1f km offshore. This is only "
            "valid for the pre-apply compute-estimate preview; the T4A.3 "
            "apply-time chain always supplies offshore_distance_m for real "
            "SWAN runs.",
            cluster.spot_ids, offshore_km,
        )
    # Blocker 2, RESOLVED — see _l3_shoreward_edge_reach_m()'s docstring.
    # This pin-based path only runs for a classification-only trigger (no
    # structures — see the `if structures:` branch above, which returns
    # early via smart_size_l3_grid() otherwise), so there is no feature
    # geometry to size a reach from; the function returns None and this
    # keeps the pre-existing near-shore margin, not a new constant.
    _reach_m = _l3_shoreward_edge_reach_m(None)
    landward_km = (_reach_m / 1000.0) if _reach_m is not None else 0.1

    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(center_lat))

    # Offshore and landward offsets using bearing
    offshore_dlat = offshore_km * math.cos(bearing_rad) / km_per_deg_lat
    offshore_dlon = offshore_km * math.sin(bearing_rad) / km_per_deg_lon
    landward_dlat = landward_km * math.cos(bearing_rad + math.pi) / km_per_deg_lat
    landward_dlon = landward_km * math.sin(bearing_rad + math.pi) / km_per_deg_lon

    # Lateral offsets (perpendicular to bearing)
    lateral_km = lateral_m / 1000.0
    perp_rad = bearing_rad + math.pi / 2
    lateral_dlat = lateral_km * abs(math.cos(perp_rad)) / km_per_deg_lat
    lateral_dlon = lateral_km * abs(math.sin(perp_rad)) / km_per_deg_lon

    offshore_lat = center_lat + offshore_dlat
    offshore_lon = center_lon + offshore_dlon
    landward_lat = center_lat + landward_dlat
    landward_lon = center_lon + landward_dlon

    all_lats = cluster.lats + [offshore_lat, landward_lat]
    all_lons = cluster.lons + [offshore_lon, landward_lon]

    # T4A.3 (coordinator finding, 2026-07-25): fold each spot's OWN actual
    # deep transect endpoint into the corner-point set. The single
    # cluster-centroid offshore point above is not guaranteed to be as far
    # (in every individual spot's own bearing direction) as that spot's own
    # coastline-anchored transect needs — this is what was silently clipping
    # compute_spot_transect() at runtime.
    if spot_deep_points:
        all_lats = all_lats + [p[0] for p in spot_deep_points]
        all_lons = all_lons + [p[1] for p in spot_deep_points]

    lat_min = min(all_lats) - lateral_dlat
    lat_max = max(all_lats) + lateral_dlat
    lon_min = min(all_lons) - lateral_dlon
    lon_max = max(all_lons) + lateral_dlon

    domain = GridDomain(
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        resolution_m=resolution_m, level=3,
    )

    if spot_deep_points:
        _assert_grid_contains_points(domain, spot_deep_points, cluster.spot_ids)

    return domain


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in km."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# T4A.3 Do step 8 — "Grid boundary metadata" persistence.
#
# The apply-time chain (endpoints/setup.py) computes DomainSizing ONCE, at
# apply time, and must persist it so the SWAN runtime (providers/nearshore/
# swan.py, Do step 9) never calls compute_domains()/compute_level3_domains()
# fresh — no CUDEM download, no grid sizing, at runtime. These two functions
# are the lossless (de)serialization for that cache file.
# ---------------------------------------------------------------------------


def domain_sizing_to_dict(sizing: DomainSizing) -> dict:
    """Serialize a ``DomainSizing`` to a JSON-safe dict."""
    return {
        "level1": dataclasses.asdict(sizing.level1),
        "level2": dataclasses.asdict(sizing.level2),
        "level3_clusters": [
            {
                "spot_ids": cluster.spot_ids,
                "lats": cluster.lats,
                "lons": cluster.lons,
                "grid": dataclasses.asdict(cluster.grid) if cluster.grid else None,
            }
            for cluster in sizing.level3_clusters
        ],
    }


def domain_sizing_from_dict(d: dict) -> DomainSizing:
    """Reconstruct a ``DomainSizing`` from ``domain_sizing_to_dict()``'s output.

    Raises ``KeyError``/``TypeError`` on a malformed cache — callers must
    treat that as a cache-read failure (ERROR + skip run per T4A.3 Do step
    9), not a reason to fall back to fresh computation at runtime.
    """
    return DomainSizing(
        level1=GridDomain(**d["level1"]),
        level2=GridDomain(**d["level2"]),
        level3_clusters=[
            SpotCluster(
                spot_ids=c["spot_ids"],
                lats=c["lats"],
                lons=c["lons"],
                grid=GridDomain(**c["grid"]) if c.get("grid") else None,
            )
            for c in d["level3_clusters"]
        ],
    )
