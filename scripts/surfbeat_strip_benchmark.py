"""SurfBeat strip INPUT generator — benchmark prototype.

Generates a SWAN INPUT file and bathymetry grid for the SurfBeat strip
benchmark (1D-MODEL-BENCHMARK-BRIEF Part 7 §7.3).

The strip is a small regular 2D grid in its own coordinate frame:
  - x = cross-shore, oriented west→east (offshore→shore)
  - Bathymetry = 1D profile duplicated identically alongshore
  - SURFBEAT enabled with two-COMPUTE procedure
  - Shoreline OBSTACLE with IG reflection on the east side
  - Spectral output at stations along the centerline

Usage:
    python -m weewx_clearskies_api.services.surfbeat_strip_benchmark \\
        --profile profile.csv --hs 1.0 --tp 14 --dir 200 \\
        --cfjon 0.038 --outdir /tmp/surfbeat_strip
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import numpy as np


@dataclass
class StripConfig:
    dx: float = 5.0  # cross-shore resolution (m)
    ny: int = 20  # alongshore rows
    dy: float = 25.0  # alongshore resolution (m)
    cfjon: float = 0.038  # JONSWAP friction coefficient
    gamma: float = 0.73  # breaking parameter
    dir_spread: float = 10.0  # directional spread (deg) for parametric boundary
    n_output_stations: int = 25  # spectral output stations along centerline


def generate_strip_files(
    profile: np.ndarray,
    hs: float,
    tp: float,
    direction: float,
    outdir: str,
    config: StripConfig | None = None,
    surfbeat: bool = True,
) -> dict:
    """Generate SWAN INPUT + BOTTOM.txt for a SurfBeat strip run.

    Args:
        profile: Nx2 array [[distance_from_shore_m, depth_m], ...] sorted
            offshore to shore (decreasing distance, decreasing depth).
            Depths positive = below water.
        hs: significant wave height at offshore boundary (m)
        tp: peak period (s)
        direction: wave direction in strip frame (270 = from west = shore-normal)
        outdir: directory to write INPUT and BOTTOM.txt
        config: strip configuration; defaults used if None
        surfbeat: if True, enable SURFBEAT command + two COMPUTEs

    Returns:
        dict with keys: input_path, bottom_path, nx, ny, n_stations,
        station_distances, strip_length
    """
    if config is None:
        config = StripConfig()

    idx_sort = np.argsort(-profile[:, 0])
    distances = profile[idx_sort, 0]
    depths = profile[idx_sort, 1]

    strip_length = float(distances[0] - distances[-1])
    target_x = np.arange(0, strip_length + config.dx, config.dx)
    interp_depths = np.interp(target_x, distances[-1] + target_x, depths[::-1])
    interp_depths = np.maximum(interp_depths, 0.01)
    # Reverse so index 0 = west (offshore), last = east (shore)
    interp_depths = interp_depths[::-1]

    nx = len(interp_depths) - 1  # mesh count = points - 1
    ny = config.ny
    xlenc = nx * config.dx
    ylenc = ny * config.dy

    # Origin at (0, 0) in strip-local coordinates
    xp, yp = 0.0, 0.0

    # --- Write BOTTOM.txt ---
    # idla=3: south-to-north (row 0 = south), west-to-east within each row
    # Each row is identical (alongshore-uniform)
    os.makedirs(outdir, exist_ok=True)
    bottom_path = os.path.join(outdir, "BOTTOM.txt")
    with open(bottom_path, "w") as f:
        for _row in range(ny + 1):  # ny+1 rows (meshes + 1 = points)
            line = " ".join(f"{d:.2f}" for d in interp_depths)
            f.write(line + "\n")

    # --- Output stations along centerline ---
    y_center = ylenc / 2.0
    station_step = max(1, (nx + 1) // config.n_output_stations)
    station_indices = list(range(0, nx + 1, station_step))
    if station_indices[-1] != nx:
        station_indices.append(nx)

    station_xs = [i * config.dx for i in station_indices]
    station_distances_from_shore = [xlenc - x for x in station_xs]

    # --- Frequency range ---
    # IG needs low frequencies down to ~0.004 Hz (250s period)
    # Sea-swell goes up to 1.0 Hz
    flow = 0.004 if surfbeat else 0.04
    fhigh = 1.0
    nfreq = 60 if surfbeat else 31

    # --- Write INPUT file ---
    input_path = os.path.join(outdir, "INPUT")
    with open(input_path, "w") as f:
        f.write("PROJECT 'SBstrip' '001'\n")
        f.write("$\n")
        f.write("SET 0. 90. 0.05 200 3\n")
        f.write("SET NAUTICAL\n")
        f.write("COORDINATES CARTESIAN\n")
        f.write("$\n")
        f.write("MODE STATIONARY\n")
        f.write("$\n")
        f.write(
            f"CGRID REG {xp:.1f} {yp:.1f} 0. {xlenc:.1f} {ylenc:.1f} "
            f"{nx} {ny} CIRCLE 36 {flow} {fhigh} {nfreq}\n"
        )
        f.write("$\n")
        f.write(
            f"INPGRID BOTTOM REG {xp:.1f} {yp:.1f} 0. {nx} {ny} "
            f"{config.dx:.1f} {config.dy:.1f}\n"
        )
        f.write("READINP BOTTOM 1. 'BOTTOM.txt' 3 0 FREE\n")
        f.write("$\n")
        # West boundary: parametric JONSWAP spectrum
        f.write(
            f"BOUND SIDE WEST CCW CONstant PAR {hs:.3f} {tp:.1f} "
            f"{direction:.1f} {config.dir_spread:.1f}\n"
        )
        f.write("$\n")
        # Physics
        f.write("GEN3 WESTHUYSEN\n")
        f.write(f"BREAKING CONSTANT 1.0 {config.gamma}\n")
        f.write(f"FRICTION JON {config.cfjon}\n")
        f.write("TRIAD\n")
        f.write("$\n")
        # Exception value
        f.write("QUANTITY HSIGN TM01 DIR excv=-9.\n")
        f.write("$\n")
        # Shoreline OBSTACLE on east side (IG reflection)
        # Line runs along the east boundary from south to north
        # REFL 0.5 = 50% reflection, RDIFF 2 = diffuse with cos^2 distribution
        x_east = xlenc
        f.write(
            f"OBSTACLE TRANSM 0.0 REFL 0.5 RDIFF 2 "
            f"LINE {x_east:.1f} 0.0 {x_east:.1f} {ylenc:.1f}\n"
        )
        f.write("$\n")

        if surfbeat:
            f.write("SURFBEAT\n")
            f.write("$\n")

        # Output stations
        for idx, (sx, sd) in enumerate(
            zip(station_xs, station_distances_from_shore)
        ):
            sname = f"S{idx:02d}"
            f.write(f"POINTS '{sname}' {sx:.1f} {y_center:.1f}\n")
        f.write("$\n")
        for idx in range(len(station_xs)):
            sname = f"S{idx:02d}"
            f.write(f"SPECOUT '{sname}' SPEC2D ABS 'SPEC_{sname}.txt'\n")
        f.write("$\n")
        # TABLE at all stations for Hs, depth, QB
        for idx in range(len(station_xs)):
            sname = f"S{idx:02d}"
            f.write(
                f"TABLE '{sname}' HEAD 'TABLE_{sname}.txt' "
                f"HSIGN HSWELL TM01 DIR DEPTH QB\n"
            )
        f.write("$\n")

        # COMPUTE(s) — bare COMPUTE for stationary mode
        f.write("COMPUTE\n")
        if surfbeat:
            f.write("COMPUTE\n")

        f.write("$\n")
        f.write("STOP\n")

    return {
        "input_path": input_path,
        "bottom_path": bottom_path,
        "nx": nx,
        "ny": ny,
        "n_stations": len(station_xs),
        "station_distances_from_shore": station_distances_from_shore,
        "strip_length": strip_length,
        "n_depth_points": len(interp_depths),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate SurfBeat strip SWAN INPUT for benchmark"
    )
    parser.add_argument("--profile", required=True, help="Bathymetry CSV (distance_from_shore_m,depth_m)")
    parser.add_argument("--hs", type=float, required=True, help="Hs at offshore boundary (m)")
    parser.add_argument("--tp", type=float, required=True, help="Peak period (s)")
    parser.add_argument("--dir", type=float, default=270.0, help="Wave direction in strip frame (270=shore-normal)")
    parser.add_argument("--cfjon", type=float, default=0.038, help="JONSWAP friction coefficient")
    parser.add_argument("--outdir", required=True, help="Output directory for INPUT + BOTTOM.txt")
    parser.add_argument("--no-surfbeat", action="store_true", help="Disable SURFBEAT (baseline run)")
    parser.add_argument("--dx", type=float, default=5.0, help="Cross-shore grid spacing (m)")
    parser.add_argument("--ny", type=int, default=20, help="Alongshore row count")
    parser.add_argument("--dy", type=float, default=25.0, help="Alongshore spacing (m)")
    args = parser.parse_args()

    profile = np.loadtxt(args.profile, delimiter=",", skiprows=1)
    config = StripConfig(
        dx=args.dx, ny=args.ny, dy=args.dy, cfjon=args.cfjon
    )

    result = generate_strip_files(
        profile=profile,
        hs=args.hs,
        tp=args.tp,
        direction=args.dir,
        outdir=args.outdir,
        config=config,
        surfbeat=not args.no_surfbeat,
    )

    print(f"Strip generated in {args.outdir}")
    print(f"  Grid: {result['nx']}×{result['ny']} cells at {args.dx}m×{args.dy}m")
    print(f"  Strip length: {result['strip_length']:.0f}m")
    print(f"  Depth points: {result['n_depth_points']}")
    print(f"  Output stations: {result['n_stations']}")
    print(f"  SURFBEAT: {'ON' if not args.no_surfbeat else 'OFF'}")
    print(f"  Boundary: Hs={args.hs}m Tp={args.tp}s Dir={args.dir}°")
    print(f"  Friction: cfjon={args.cfjon}")


if __name__ == "__main__":
    main()
