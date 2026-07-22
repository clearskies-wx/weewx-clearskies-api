"""SwellTrack — cross-shore wave transformation model.

Transforms wave parameters from a nearshore handoff point to shore along
a bathymetric transect.  Implements linear wave theory shoaling, Snell's
law refraction, Battjes-Janssen (1978) breaking dissipation, the
Svendsen (1984) roller model for post-breaking energy transfer, and
JONSWAP bottom friction (Hasselmann et al. 1973).
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, field

import numpy as np
from scipy.special import ellipj, ellipk  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_NAME = "SwellTrack"

_G = 9.81  # gravitational acceleration (m/s²)
_RHO = 1025.0  # seawater density (kg/m³)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BreakPoint:
    distance_m: float
    depth_m: float
    hs_m: float
    breaker_type: str  # "spilling", "plunging", "surging"
    iribarren: float


@dataclass
class WaveShape:
    distance_m: float
    depth_m: float
    regime: str  # "stokes", "cnoidal", "bore"
    surface: list[tuple[float, float]] | None = None  # (phase, eta) pairs


@dataclass
class JackingFactor:
    bar_index: int
    distance_m: float
    factor: float  # Hs_crest / Hs_approach


@dataclass
class SurfZones:
    impact_zone: dict | None = None  # {start_distance, end_distance, start_depth, end_depth}
    foam_zone: dict | None = None
    total_surf_zone: dict | None = None
    reform_trough: dict | None = None


@dataclass
class Analytical1DResult:
    hs_profile: np.ndarray
    distances: np.ndarray
    depths: np.ndarray
    break_points: list[BreakPoint]
    wave_shapes: list[WaveShape]
    jacking_factors: list[JackingFactor]
    surf_zones: SurfZones
    runtime_ms: float = 0.0


# ---------------------------------------------------------------------------
# Core physics (vectorized with numpy)
# ---------------------------------------------------------------------------


def _dispersion(T: float, d: np.ndarray, n_iter: int = 7) -> np.ndarray:
    """Solve the dispersion relation for local wavelength L at each depth.

    L = gT²/(2π) × tanh(2πd/L)  — iterative Newton-like.
    """
    omega = 2.0 * np.pi / T
    L0 = _G * T * T / (2.0 * np.pi)
    L = L0 * np.ones_like(d)
    for _ in range(n_iter):
        k = 2.0 * np.pi / L
        L = L0 * np.tanh(k * d)
    return np.maximum(L, 0.01)


def _group_velocity(T: float, d: np.ndarray, L: np.ndarray) -> np.ndarray:
    """Group velocity Cg = n × C where n = 0.5(1 + 2kd/sinh(2kd))."""
    k = 2.0 * np.pi / L
    C = L / T
    kd = k * d
    kd_safe = np.clip(kd, 1e-6, 50.0)
    n = 0.5 * (1.0 + 2.0 * kd_safe / np.sinh(2.0 * kd_safe))
    return n * C


def _snell_refraction(
    theta0: float, L0: float, L: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Snell's law refraction. Returns (theta, Kr) arrays.

    theta0 in radians (angle relative to shore-normal at handoff).
    Kr = sqrt(cos(theta0) / cos(theta)).
    """
    sin_theta = np.sin(theta0) * L / L0
    sin_theta = np.clip(sin_theta, -1.0, 1.0)
    theta = np.arcsin(sin_theta)
    cos_ratio = np.cos(theta0) / np.cos(theta)
    cos_ratio = np.clip(cos_ratio, 0.0, 10.0)
    Kr = np.sqrt(cos_ratio)
    return theta, Kr


def _bottom_friction(
    Hs: np.ndarray,
    d: np.ndarray,
    dx: np.ndarray,
    Cg: np.ndarray,
    T: float,
    L: np.ndarray,
    cfjon: float,
) -> np.ndarray:
    """JONSWAP bottom friction dissipation (Hasselmann et al. 1973).

    Since D_bf is proportional to E, the energy balance is linear in E
    and the friction attenuation is exponential:
        Hs(x) = Hs_shoaled(x) × exp(-½ × ∫ α(x') dx')
    where α(x) = cfjon × ω² / (g × Cg(x) × sinh²(kd(x)))

    This integral accumulates along the propagation path.
    """
    k = 2.0 * np.pi / L
    kd = np.clip(k * d, 1e-6, 50.0)
    omega = 2.0 * np.pi / T

    alpha_fric = cfjon * omega**2 / (_G * np.maximum(Cg, 0.01) * np.sinh(kd) ** 2)
    cumulative = np.cumsum(alpha_fric * np.abs(dx))
    return Hs * np.exp(-0.5 * cumulative)


def _battjes_janssen(
    Hs: np.ndarray,
    d: np.ndarray,
    gamma: float,
    dx: np.ndarray,
    Cg: np.ndarray,
    T: float,
) -> np.ndarray:
    """Battjes-Janssen (1978) dissipation. Returns dissipated Hs array.

    Operates on Hs directly (Hrms = Hs/sqrt(2) internally).
    """
    Hmax = gamma * d
    Hrms = Hs / np.sqrt(2.0)

    ratio = np.clip(Hrms / np.maximum(Hmax / np.sqrt(2.0), 0.01), 0.0, 10.0)
    Qb = np.where(ratio >= 1.0, 1.0, 1.0 - np.exp(-ratio**2))

    alpha_bj = 1.0
    Dtot = alpha_bj * Qb * _RHO * _G * (Hmax / np.sqrt(2.0)) ** 2 / (4.0 * T)

    E = 0.125 * _RHO * _G * Hs**2
    E_new = E - Dtot * np.abs(dx) / np.maximum(Cg, 0.01)
    E_new = np.maximum(E_new, 0.0)
    return np.sqrt(8.0 * E_new / (_RHO * _G))


def _roller_model(
    Hs: np.ndarray,
    d: np.ndarray,
    gamma: float,
    dx: np.ndarray,
    C: np.ndarray,
    T: float,
    beta_roller: float = 0.1,
) -> np.ndarray:
    """Svendsen (1984) roller model. Delays energy transfer from organized
    wave to turbulence, producing more realistic post-breaking decay and
    reformation in bar/trough systems.

    Returns adjusted Hs array.
    """
    n = len(Hs)
    Hs_out = Hs.copy()
    Er = 0.0  # roller energy density (J/m²)

    for i in range(1, n):
        Hmax = gamma * d[i]
        if Hs_out[i] > Hmax and d[i] > 0.1:
            Dw = 0.25 * _RHO * _G * (Hs_out[i] ** 2 - Hmax**2) / T
            Dw = max(Dw, 0.0)
        else:
            Dw = 0.0

        Dr = 2.0 * _G * beta_roller * Er / (C[i] if C[i] > 0.01 else 0.01)
        dEr = (Dw - Dr) * abs(dx[i]) / max(C[i], 0.01)
        Er = max(Er + dEr, 0.0)

        E = 0.125 * _RHO * _G * Hs_out[i] ** 2
        E_adj = E - (Dw - Dr) * abs(dx[i]) / max(C[i], 0.01)
        E_adj = max(E_adj, 0.0)
        Hs_out[i] = math.sqrt(8.0 * E_adj / (_RHO * _G))

    return Hs_out


def _iribarren(slope: float, H0: float, L0: float) -> tuple[float, str]:
    """Deep-water Iribarren number xi_0 and breaker classification.

    Battjes (1974) thresholds for xi_0: <0.5 spilling, 0.5-3.3 plunging, >3.3 surging.
    """
    if H0 <= 0 or L0 <= 0:
        return 0.0, "spilling"
    xi = slope / math.sqrt(H0 / L0)
    if xi < 0.5:
        return xi, "spilling"
    elif xi <= 3.3:
        return xi, "plunging"
    else:
        return xi, "surging"


def _local_slope(depths: np.ndarray, dx_arr: np.ndarray) -> np.ndarray:
    """Compute local bottom slope |dd/dx| at each point."""
    dd = np.diff(depths, prepend=depths[0])
    slope = np.abs(dd) / np.maximum(np.abs(dx_arr), 0.1)
    return slope


# ---------------------------------------------------------------------------
# Wave shape computation
# ---------------------------------------------------------------------------


def _wave_shape_at_point(
    d: float, L: float, Hs: float, n_points: int = 20
) -> WaveShape:
    """Compute wave surface shape at a single profile point."""
    dist = 0.0  # placeholder
    if d <= 0.1 or L <= 0.1:
        return WaveShape(distance_m=dist, depth_m=d, regime="bore", surface=None)

    dL = d / L
    if dL > 0.05:
        # Stokes 2nd order
        k = 2.0 * np.pi / L
        a = Hs / 2.0
        phase = np.linspace(0, 2 * np.pi, n_points)
        kd = k * d
        kd_safe = min(kd, 50.0)
        stokes2 = (
            a * np.cos(phase)
            + 0.25 * k * a**2 * np.cosh(kd_safe) * (2 + np.cosh(2 * kd_safe))
            / np.sinh(kd_safe) ** 3
            * np.cos(2 * phase)
        )
        surface = [(float(p), float(e)) for p, e in zip(phase, stokes2)]
        return WaveShape(distance_m=dist, depth_m=d, regime="stokes", surface=surface)
    else:
        # Cnoidal wave theory
        k_cn = 2.0 * np.pi / L
        Ur = Hs * L**2 / d**3  # Ursell number
        m = min(1.0 - 1e-10, max(0.01, 1.0 - 16.0 / max(Ur, 1e-6)))
        Kk = float(ellipk(m))
        phase = np.linspace(0, 2 * Kk, n_points)
        sn, cn, dn, _ = ellipj(phase, m)
        eta = Hs * (cn**2 - (1.0 - m) / m * (1.0 - ellipk(m) / Kk))
        surface = [
            (float(p / (2 * Kk) * 2 * np.pi), float(e))
            for p, e in zip(phase, eta)
        ]
        return WaveShape(distance_m=dist, depth_m=d, regime="cnoidal", surface=surface)


# ---------------------------------------------------------------------------
# Break point detection and zone classification
# ---------------------------------------------------------------------------


def _find_break_points(
    Hs: np.ndarray,
    depths: np.ndarray,
    distances: np.ndarray,
    slopes: np.ndarray,
    gamma: float,
    H0: float,
    L0: float,
) -> list[BreakPoint]:
    """Find H/d = gamma crossings (break points)."""
    ratio = Hs / np.maximum(depths, 0.01)
    breaking = ratio >= gamma

    bps: list[BreakPoint] = []
    was_breaking = False
    min_break_hs = 0.15  # ignore breaks with Hs < 15cm
    for i in range(len(Hs)):
        if breaking[i] and not was_breaking and depths[i] > 0.3 and Hs[i] > min_break_hs:
            xi, btype = _iribarren(float(slopes[i]), H0, L0)
            bps.append(BreakPoint(
                distance_m=float(distances[i]),
                depth_m=float(depths[i]),
                hs_m=float(Hs[i]),
                breaker_type=btype,
                iribarren=xi,
            ))
        was_breaking = breaking[i]
    return bps


def _classify_zones(
    Hs: np.ndarray,
    depths: np.ndarray,
    distances: np.ndarray,
    break_points: list[BreakPoint],
) -> SurfZones:
    """Classify surf zones from Hs profile and break points."""
    if not break_points:
        return SurfZones()

    outer_bp = break_points[0]
    outer_idx = int(np.argmin(np.abs(distances - outer_bp.distance_m)))
    Hs_break = Hs[outer_idx]
    Hs_50pct = Hs_break * 0.707  # 50% energy = 71% height

    impact_end_idx = outer_idx
    for i in range(outer_idx, len(Hs)):
        if Hs[i] <= Hs_50pct:
            impact_end_idx = i
            break
    else:
        impact_end_idx = len(Hs) - 1

    bore_min_idx = impact_end_idx
    for i in range(impact_end_idx, len(Hs)):
        if Hs[i] < 0.3 or depths[i] < 0.2:
            bore_min_idx = i
            break
    else:
        bore_min_idx = len(Hs) - 1

    impact = {
        "start_distance": float(distances[outer_idx]),
        "end_distance": float(distances[impact_end_idx]),
        "start_depth": float(depths[outer_idx]),
        "end_depth": float(depths[impact_end_idx]),
    }
    foam = {
        "start_distance": float(distances[impact_end_idx]),
        "end_distance": float(distances[bore_min_idx]),
        "start_depth": float(depths[impact_end_idx]),
        "end_depth": float(depths[bore_min_idx]),
    }
    total = {
        "width_m": abs(float(distances[bore_min_idx] - distances[outer_idx])),
        "start_distance": float(distances[outer_idx]),
        "end_distance": float(distances[bore_min_idx]),
    }

    reform = None
    if len(break_points) > 1:
        inner_bp = break_points[-1]
        reform = {
            "start_distance": float(distances[impact_end_idx]),
            "end_distance": inner_bp.distance_m,
            "start_depth": float(depths[impact_end_idx]),
            "end_depth": inner_bp.depth_m,
        }

    return SurfZones(
        impact_zone=impact,
        foam_zone=foam,
        total_surf_zone=total,
        reform_trough=reform,
    )


def _compute_jacking(
    Hs: np.ndarray, depths: np.ndarray, distances: np.ndarray, gamma: float
) -> list[JackingFactor]:
    """Compute jacking factor at each bar crest (local Hs peak before breaking)."""
    factors: list[JackingFactor] = []
    bar_idx = 0
    for i in range(2, len(Hs) - 2):
        is_peak = Hs[i] > Hs[i - 1] and Hs[i] > Hs[i + 1]
        is_bar = depths[i] < depths[i - 1] and depths[i] < depths[i + 1]
        if is_peak and is_bar and Hs[i] / max(depths[i], 0.1) > gamma * 0.8:
            approach_hs = np.mean(Hs[max(0, i - 5) : i])
            if approach_hs > 0.01:
                factors.append(JackingFactor(
                    bar_index=bar_idx,
                    distance_m=float(distances[i]),
                    factor=float(Hs[i] / approach_hs),
                ))
                bar_idx += 1
    return factors


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_1d_analytical(
    hs: float,
    tp: float,
    direction: float,
    bathy_profile: np.ndarray,
    tide_level: float = 0.0,
    gamma: float = 0.73,
    beach_facing: float = 180.0,
    cfjon: float | None = None,
) -> Analytical1DResult:
    """Run the analytical 1D cross-shore wave transformation.

    Args:
        hs: significant wave height at handoff (m)
        tp: peak period (s)
        direction: wave direction, degrees true north
        bathy_profile: Nx2 array [[distance_from_shore_m, depth_m], ...]
            ordered from offshore (large distance) to shore (small distance).
            Depths positive = below water.
        tide_level: tide offset in metres (added to depth)
        gamma: breaking parameter (default 0.73, matching SWAN)
        beach_facing: shore-normal direction in degrees true north
        cfjon: JONSWAP bottom-friction coefficient (m²/s³). None = no friction
            (frictionless, original behavior). Use 0.038 for swell, 0.067
            for wind seas (Zijlema et al. 2012).
    """
    t0 = time.perf_counter()

    # Sort offshore to shore (decreasing distance)
    idx_sort = np.argsort(-bathy_profile[:, 0])
    distances = bathy_profile[idx_sort, 0].copy()
    depths = bathy_profile[idx_sort, 1].copy() + tide_level
    depths = np.maximum(depths, 0.01)
    n = len(depths)

    # Deep-water parameters
    L0 = _G * tp**2 / (2.0 * np.pi)
    theta0_deg = direction - beach_facing
    theta0 = np.radians(theta0_deg)

    # Dispersion and group velocity at each point
    L = _dispersion(tp, depths)
    Cg = _group_velocity(tp, depths, L)
    C = L / tp

    # Shoaling coefficient
    Cg0 = Cg[0]
    Ks = np.sqrt(Cg0 / np.maximum(Cg, 0.01))

    # Refraction
    _, Kr = _snell_refraction(theta0, L0, L)

    # Initial Hs profile from shoaling + refraction
    Hs = hs * Ks * Kr

    # Spatial step sizes (negative because we go from offshore to shore)
    dx = np.diff(distances, prepend=distances[0])

    # JONSWAP bottom friction (applied before breaking — acts on all waves)
    if cfjon is not None and cfjon > 0.0:
        Hs = _bottom_friction(Hs, depths, dx, Cg, tp, L, cfjon)

    # Battjes-Janssen breaking dissipation
    Hs = _battjes_janssen(Hs, depths, gamma, dx, Cg, tp)

    # Roller model for realistic post-breaking behavior
    Hs = _roller_model(Hs, depths, gamma, dx, C, tp)

    # Enforce depth-limited saturation: Hs cannot exceed gamma*d
    Hs = np.minimum(Hs, gamma * depths)

    # Local slopes for Iribarren
    slopes = _local_slope(depths, dx)

    # Break points
    break_points = _find_break_points(Hs, depths, distances, slopes, gamma, hs, L0)

    # Wave shapes at selected points (every 10th point to keep output manageable)
    wave_shapes: list[WaveShape] = []
    step = max(1, n // 30)
    for i in range(0, n, step):
        ws = _wave_shape_at_point(float(depths[i]), float(L[i]), float(Hs[i]))
        ws.distance_m = float(distances[i])
        wave_shapes.append(ws)

    # Jacking factors
    jacking = _compute_jacking(Hs, depths, distances, gamma)

    # Surf zones
    zones = _classify_zones(Hs, depths, distances, break_points)

    elapsed = (time.perf_counter() - t0) * 1000.0

    return Analytical1DResult(
        hs_profile=Hs,
        distances=distances,
        depths=depths,
        break_points=break_points,
        wave_shapes=wave_shapes,
        jacking_factors=jacking,
        surf_zones=zones,
        runtime_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Synthetic bathymetry for benchmarking
# ---------------------------------------------------------------------------


def _synthetic_profile(
    length_m: float = 500.0, dx: float = 3.0
) -> np.ndarray:
    """Generate a synthetic beach profile with a sandbar and trough.

    Linear slope from 15m to 0m, with:
    - Sandbar at 200m from shore, crest at 2m depth
    - Trough at 250m from shore, 4m depth
    """
    distances = np.arange(0, length_m + dx, dx)[::-1]  # offshore to shore
    base_depth = 15.0 * distances / length_m

    # Add sandbar gaussian bump
    bar_center = 200.0
    bar_width = 30.0
    bar_height = 2.5
    bar = bar_height * np.exp(-((distances - bar_center) ** 2) / (2 * bar_width**2))
    depth = base_depth - bar

    # Ensure depths are positive
    depth = np.maximum(depth, 0.01)

    return np.column_stack([distances, depth])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SwellTrack cross-shore wave transformation model"
    )
    parser.add_argument("--hs", type=float, default=1.0, help="Hs at handoff (m)")
    parser.add_argument("--tp", type=float, default=12.0, help="Peak period (s)")
    parser.add_argument("--dir", type=float, default=180.0, help="Wave direction (deg)")
    parser.add_argument("--tide", type=float, default=0.0, help="Tide level (m)")
    parser.add_argument("--gamma", type=float, default=0.73, help="Breaking gamma")
    parser.add_argument("--beach-facing", type=float, default=180.0, help="Shore-normal (deg)")
    parser.add_argument("--cfjon", type=float, default=None, help="JONSWAP friction coeff (0.038 swell, 0.067 windsea)")
    parser.add_argument("--bathy", type=str, default=None, help="Bathymetry CSV (distance,depth)")
    parser.add_argument("--output", type=str, default=None, help="Output CSV path")
    args = parser.parse_args()

    if args.bathy:
        bathy = np.loadtxt(args.bathy, delimiter=",", skiprows=1)
    else:
        bathy = _synthetic_profile()

    result = run_1d_analytical(
        hs=args.hs,
        tp=args.tp,
        direction=args.dir,
        bathy_profile=bathy,
        tide_level=args.tide,
        gamma=args.gamma,
        beach_facing=args.beach_facing,
        cfjon=args.cfjon,
    )

    print(f"Runtime: {result.runtime_ms:.2f} ms")
    print(f"Profile points: {len(result.hs_profile)}")
    print(f"Break points: {len(result.break_points)}")
    for bp in result.break_points:
        print(
            f"  Break at {bp.distance_m:.0f}m from shore, "
            f"depth {bp.depth_m:.1f}m, Hs {bp.hs_m:.2f}m, "
            f"{bp.breaker_type} (xi={bp.iribarren:.2f})"
        )
    print(f"Jacking factors: {len(result.jacking_factors)}")
    for jf in result.jacking_factors:
        print(f"  Bar {jf.bar_index} at {jf.distance_m:.0f}m: {jf.factor:.2f}x")
    print(f"Wave shapes: {len(result.wave_shapes)}")
    regimes = [ws.regime for ws in result.wave_shapes]
    print(f"  Regimes: {', '.join(dict.fromkeys(regimes))}")
    if result.surf_zones.total_surf_zone:
        print(f"Surf zone width: {result.surf_zones.total_surf_zone['width_m']:.0f}m")

    if args.output:
        header = "distance_m,depth_m,hs_m"
        data = np.column_stack([result.distances, result.depths, result.hs_profile])
        np.savetxt(args.output, data, delimiter=",", header=header, comments="")
        print(f"Output written to {args.output}")


if __name__ == "__main__":
    main()
