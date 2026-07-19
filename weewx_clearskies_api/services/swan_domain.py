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

    @property
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

    Returns:
        DomainSizing with all three grid levels computed.
    """
    if not spot_locations:
        raise ValueError("compute_domains: no spot locations provided")

    lats = [s["lat"] for s in spot_locations]
    lons = [s["lon"] for s in spot_locations]

    # --- Level 1: Coarse grid (1 km) ---
    level1 = _compute_level1(
        lats, lons,
        resolution_m=level1_resolution_m,
        margin_km=level1_margin_km,
    )

    # --- Level 2: Nearshore grid (100 m) ---
    level2 = _compute_level2(
        lats, lons,
        resolution_m=level2_resolution_m,
        margin_km=level2_margin_km,
        offshore_depth_m=level2_offshore_depth_m,
    )

    # --- Level 3: Surf zone grids (10 m) — per cluster ---
    clusters = _cluster_spots(spot_locations, cluster_distance_m)
    for cluster in clusters:
        cluster.grid = _compute_level3_grid(
            cluster,
            resolution_m=level3_resolution_m,
            lateral_m=level3_lateral_m,
            offshore_depth_m=level3_offshore_depth_m,
        )

    return DomainSizing(level1=level1, level2=level2, level3_clusters=clusters)


def _compute_level1(
    lats: list[float],
    lons: list[float],
    *,
    resolution_m: float,
    margin_km: float,
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

    # Offshore extent: shelf distance + margin into deep water
    offshore_km = shelf_dist_km + margin_km + 10.0  # +10km past shelf edge

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

    # The offshore direction is generally "away from coast" — approximate as
    # seaward from the spot centroid. For simplicity, extend in all directions
    # but bias offshore using the average beach-facing direction.
    lat_min = min(lats) - lat_margin_deg
    lat_max = max(lats) + lat_margin_deg
    lon_min = min(lons) - max(lon_margin_deg, offshore_deg)
    lon_max = max(lons) + max(lon_margin_deg, offshore_deg)

    return GridDomain(
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        resolution_m=resolution_m, level=1,
    )


def _compute_level2(
    lats: list[float],
    lons: list[float],
    *,
    resolution_m: float,
    margin_km: float,
    offshore_depth_m: float,
) -> GridDomain:
    """Level 2: all spots + margin, extending to 30m depth offshore.

    The 30m depth contour is approximately 3-4 km offshore for SoCal.
    Without pre-computed bathymetry data, we estimate cross-shore extent
    using a typical shelf gradient.
    """
    center_lat = sum(lats) / len(lats)

    # Estimate cross-shore extent to 30m depth
    # Typical nearshore gradient: ~1:100 to ~1:200 (SoCal average)
    # 30m depth at ~1:100 gradient = ~3 km offshore
    # Use 4 km as conservative estimate + margin
    cross_shore_km = 4.0 + margin_km

    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(center_lat))

    lat_margin_deg = margin_km / km_per_deg_lat
    lon_margin_deg = cross_shore_km / km_per_deg_lon

    lat_min = min(lats) - lat_margin_deg
    lat_max = max(lats) + lat_margin_deg
    lon_min = min(lons) - lon_margin_deg
    lon_max = max(lons) + lon_margin_deg

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


def _compute_level3_grid(
    cluster: SpotCluster,
    *,
    resolution_m: float,
    lateral_m: float,
    offshore_depth_m: float,
) -> GridDomain:
    """Compute one Level 3 grid for a spot cluster.

    Extends lateral_m before first pin and after last pin along coast.
    Cross-shore: ~1 km (shore to ~15m depth).
    """
    center_lat = sum(cluster.lats) / len(cluster.lats)

    # Cross-shore extent: shore to 15m depth ≈ 1 km
    cross_shore_km = 1.0

    # Lateral extent: 250m before first pin to 250m after last pin
    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(center_lat))

    lateral_deg_lat = lateral_m / 1000 / km_per_deg_lat
    lateral_deg_lon = lateral_m / 1000 / km_per_deg_lon
    cross_shore_deg = cross_shore_km / km_per_deg_lon

    lat_min = min(cluster.lats) - lateral_deg_lat
    lat_max = max(cluster.lats) + lateral_deg_lat
    lon_min = min(cluster.lons) - cross_shore_deg
    lon_max = max(cluster.lons) + cross_shore_deg

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
