"""SWAN 3-level nested grid domain sizing and spot clustering.

Phase 14a/14d of SWAN-FIXES-PLAN. Computes physics-based grid domains for the
3-level nesting architecture:

  Level 1 (1 km): Continental shelf approach — all spots + GSFM shelf distance
  Level 2 (100 m): Nearshore refraction — all spots to 30m depth
  Level 3 (10 m): Surf zone — per cluster, shore to 15m depth

Spot clustering (14d): adjacent spots <500m apart share one Level 3 grid.
"""

from __future__ import annotations

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
            discovery).  When provided, L3 grids with structure coordinates use
            structure-based smart sizing (T3.2) instead of pin-cluster sizing.
        spot_l3_configs: Optional mapping of spot_id → l3_enabled value ("auto",
            "on", "off").  When provided, clusters where all spots have
            l3_enabled="off" receive ``grid=None`` (L3 skipped).

    Returns:
        DomainSizing with all three grid levels computed.
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
    level2 = _compute_level2(
        lats, lons, bearings,
        resolution_m=level2_resolution_m,
        margin_km=level2_margin_km,
        offshore_depth_m=level2_offshore_depth_m,
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
        # T3.1 — skip L3 if all spots in this cluster have l3_enabled="off"
        if spot_l3_configs is not None:
            flags = [spot_l3_configs.get(sid, "auto") for sid in cluster.spot_ids]
            if all(f == "off" for f in flags):
                cluster.grid = None
                logger.info(
                    "L3 skipped for cluster %s: all spots have l3_enabled=off",
                    cluster.spot_ids,
                )
                continue

        # Use the maximum offshore distance from any spot in this cluster.
        # This ensures the grid reaches the 15m contour for all spots.
        cluster_distances = [
            spot_offshore[sid] for sid in cluster.spot_ids if sid in spot_offshore
        ]
        cluster_offshore_m = max(cluster_distances) if cluster_distances else None

        cluster.grid = _compute_level3_grid(
            cluster,
            avg_bearing,
            resolution_m=level3_resolution_m,
            lateral_m=level3_lateral_m,
            offshore_depth_m=level3_offshore_depth_m,
            offshore_distance_m=cluster_offshore_m,
            structures=structures,
        )

    return DomainSizing(level1=level1, level2=level2, level3_clusters=clusters)


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
) -> GridDomain:
    """Level 2: extends OFFSHORE from spots to 30m depth, with small land margin.

    Uses beach_facing_degrees to determine which direction is offshore.
    The grid extends 4-6 km in the offshore direction and only 0.5 km
    on the land side.
    """
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # Average beach-facing bearing (offshore direction)
    avg_bearing = sum(beach_facing_degrees) / len(beach_facing_degrees)
    bearing_rad = math.radians(avg_bearing)

    # Cross-shore: 6 km offshore (to ~30m depth), 0.5 km landward
    offshore_km = 6.0
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

    # Shadow zone: 3× structure length in the shoreward direction
    shadow_km = 3.0 * max_struct_length_km
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

    # Ensure the grid contains all spot pin positions (with padding).
    # Smart-sizing from structures alone can produce a grid that excludes
    # the spots when structures are offset from the pins.
    spot_pad_deg = pad_m / (km_per_deg_lat * 1000.0)
    corner_lats.extend(lat - spot_pad_deg for lat in cluster.lats)
    corner_lats.extend(lat + spot_pad_deg for lat in cluster.lats)
    corner_lons.extend(lon - pad_m / (km_per_deg_lon * 1000.0) for lon in cluster.lons)
    corner_lons.extend(lon + pad_m / (km_per_deg_lon * 1000.0) for lon in cluster.lons)

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


def _compute_level3_grid(
    cluster: SpotCluster,
    beach_facing_degrees: float,
    *,
    resolution_m: float,
    lateral_m: float,
    offshore_depth_m: float,
    offshore_distance_m: float | None = None,
    structures: list[dict] | None = None,
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
    """
    # T3.2 — when structures with coordinates are provided, delegate to
    # structure-based smart sizing (shadow zone extent).
    if structures:
        smart = smart_size_l3_grid(
            cluster,
            beach_facing_degrees,
            structures,
            resolution_m=resolution_m,
            pad_m=100.0,
        )
        if smart is not None:
            return smart

    center_lat = sum(cluster.lats) / len(cluster.lats)
    center_lon = sum(cluster.lons) / len(cluster.lons)

    bearing_rad = math.radians(beach_facing_degrees)

    if offshore_distance_m is not None and offshore_distance_m > 0:
        offshore_km = offshore_distance_m / 1000.0 + 0.1  # +100m margin past 15m contour
    else:
        offshore_km = 2.5  # fallback when no profile data
        logger.warning(
            "Level 3 grid for cluster %s: no profile data, using fallback %.1f km offshore",
            cluster.spot_ids, offshore_km,
        )
    landward_km = 0.1

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

    lat_min = min(all_lats) - lateral_dlat
    lat_max = max(all_lats) + lateral_dlat
    lon_min = min(all_lons) - lateral_dlon
    lon_max = max(all_lons) + lateral_dlon

    return GridDomain(
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        resolution_m=resolution_m, level=3,
    )


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
