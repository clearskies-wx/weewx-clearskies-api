"""Compare SWAN's own watershed spectral partitioning against the existing
``decompose_spectrum()`` ±4-bin neighbourhood algorithm, on REAL SWAN output
(Phase 4B T4B.2).

**This script is build-and-measure only.** Replacing ``decompose_spectrum()``
with SWAN's watershed partitioning is a formula/algorithm change (trigger 1,
per CLAUDE.md's architectural-change rules) and has NOT been approved. This
script does not change, call, or influence any production code path — it
only reads two SWAN output files, runs both algorithms on the same spectra,
and prints a comparison table. It is not part of the service and is not
imported by anything under ``weewx_clearskies_api/``.

Inputs (from the SAME SWAN run, at the SAME handoff point set — see
MARINE-SERVICE-SEPARATION-PLAN.md Phase 4B "The sequencing problem... —
resolved"):
  - A ``TABLE_*.txt`` file containing the PT* watershed-partitioning
    quantities (PTHSIGN/PTRTP/PTDIR/PTDSPR — added to the existing CURVE
    TABLE line by T4B.1's first commit) alongside XP/YP/TIME.
  - The matching ``SPEC_*.txt`` file (SPECOUT SPEC2D ABS) at the SAME
    output point set, for the full 2-D spectrum ``decompose_spectrum()``
    (the existing algorithm) needs, and for the "total m0" conservation
    reference both algorithms are measured against (LC-4B-6).

Stations are matched between the two files BY COORDINATE (XP/YP vs.
LONLAT), never by trusting that both files iterate stations in the same
index order — same discipline as ``swan_runner.py``'s T4A.10 handoff
matching, and for the same reason (an unverified index-order assumption
would silently pair the wrong station's TABLE row with the wrong station's
spectrum).

Usage:
    python scripts/compare_partitioning.py TABLE_1.txt SPEC_1.txt
    python scripts/compare_partitioning.py TABLE_1.txt SPEC_1.txt \\
        --coord-tolerance-deg 0.003 --close-partition-threshold-deg 15
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weewx_clearskies_api.services.swan_spectral import (
    SpecoutParseError,
    compute_total_m0,
    decompose_spectrum,
    parse_specout_file_multi,
    parse_table_pt_partitions,
)


@dataclass
class StationTimeMatch:
    """One (station, timestep) pair found in both input files."""

    time: str
    table_xp: float
    table_yp: float
    spec_station_idx: int
    watershed_partitions: list[dict[str, Any]]
    freqs_hz: list[float]
    dirs_deg: list[float]
    energy: list[list[float]]


def _match_table_rows_to_spec_stations(
    table_rows: list[dict[str, Any]],
    spec_station_lonlat: list[tuple[float, float]],
    spec_station_timesteps: list[list[dict[str, Any]]],
    coord_tolerance_deg: float,
) -> list[StationTimeMatch]:
    """Coordinate-match TABLE rows (one per station per timestep) against
    the SPECOUT file's stations, then align by timestamp within a station.

    Never trusts index order between the two files (see module docstring).
    A TABLE row with no SPEC station within tolerance, or whose time has no
    matching spectrum at that station, is skipped and counted — reported by
    the caller rather than silently dropped.
    """
    matches: list[StationTimeMatch] = []
    unmatched_coord = 0
    unmatched_time = 0

    # spec time -> index, per station (built once per station on first use).
    spec_time_index_cache: dict[int, dict[str, int]] = {}

    for row in table_rows:
        best_idx: int | None = None
        best_dist = float("inf")
        for spec_idx, (slon, slat) in enumerate(spec_station_lonlat):
            d = math.hypot(row["xp"] - slon, row["yp"] - slat)
            if d < coord_tolerance_deg and d < best_dist:
                best_dist = d
                best_idx = spec_idx

        if best_idx is None:
            unmatched_coord += 1
            continue

        if best_idx not in spec_time_index_cache:
            spec_time_index_cache[best_idx] = {
                spec["time"]: t_idx
                for t_idx, spec in enumerate(spec_station_timesteps[best_idx])
            }

        if row["time"] is None or row["time"] not in spec_time_index_cache[best_idx]:
            unmatched_time += 1
            continue

        t_idx = spec_time_index_cache[best_idx][row["time"]]
        spectrum = spec_station_timesteps[best_idx][t_idx]

        matches.append(StationTimeMatch(
            time=row["time"],
            table_xp=row["xp"],
            table_yp=row["yp"],
            spec_station_idx=best_idx,
            watershed_partitions=row["partitions"],
            freqs_hz=spectrum["freqs_hz"],
            dirs_deg=spectrum["dirs_deg"],
            energy=spectrum["energy"],
        ))

    if unmatched_coord or unmatched_time:
        print(
            f"# NOTE: {unmatched_coord} TABLE row(s) had no SPEC station within "
            f"tolerance; {unmatched_time} had no matching timestamp at their "
            f"matched station. {len(matches)} row(s) matched and compared.",
            file=sys.stderr,
        )

    return matches


def _close_partition_pairs(
    partitions: list[dict[str, Any]],
    direction_key: str,
    threshold_deg: float,
) -> list[tuple[int, int, float]]:
    """Return (i, j, angular_separation_deg) for every pair of partitions in
    *partitions* whose directions are within *threshold_deg* of each other —
    the shape of case the plan's finding 5 describes (2026-07-25: 2.9 ft @
    12 s @ 184 deg and 0.7 ft @ 23 s @ 196 deg, 12 deg apart)."""
    pairs: list[tuple[int, int, float]] = []
    for i in range(len(partitions)):
        di = partitions[i].get(direction_key)
        if di is None:
            continue
        for j in range(i + 1, len(partitions)):
            dj = partitions[j].get(direction_key)
            if dj is None:
                continue
            # Circular angular separation.
            sep = abs((di - dj + 180.0) % 360.0 - 180.0)
            if sep <= threshold_deg:
                pairs.append((i, j, sep))
    return pairs


def _format_partition_row(height: float, period: float | None, direction: float | None) -> str:
    period_s = f"{period:5.1f}s" if period is not None else "   n/a"
    dir_s = f"{direction:5.1f}deg" if direction is not None else "  n/a"
    return f"{height:5.2f}m @ {period_s} @ {dir_s}"


def compare_one(match: StationTimeMatch, close_partition_threshold_deg: float) -> dict[str, Any]:
    """Run both algorithms on one matched (station, timestep) spectrum and
    return the raw comparison numbers (LC-4B-6: measured, not asserted)."""
    total_m0 = compute_total_m0(match.freqs_hz, match.dirs_deg, match.energy)

    neighbourhood = decompose_spectrum(match.freqs_hz, match.dirs_deg, match.energy)
    neighbourhood_sum_m0 = sum(p["energy"] for p in neighbourhood)

    watershed = match.watershed_partitions  # already SWAN's own order: 1=wind sea, 2-10 swells desc Hs
    watershed_sum_m0 = sum(p["energy"] for p in watershed)
    watershed_swell_only = [p for p in watershed if not p["is_wind_sea"]]

    close_neighbourhood = _close_partition_pairs(neighbourhood, "direction", close_partition_threshold_deg)
    close_watershed = _close_partition_pairs(watershed, "direction", close_partition_threshold_deg)

    return {
        "match": match,
        "total_m0": total_m0,
        "neighbourhood": neighbourhood,
        "neighbourhood_sum_m0": neighbourhood_sum_m0,
        "watershed": watershed,
        "watershed_swell_only": watershed_swell_only,
        "watershed_sum_m0": watershed_sum_m0,
        "close_neighbourhood": close_neighbourhood,
        "close_watershed": close_watershed,
    }


def print_comparison(result: dict[str, Any]) -> None:
    match: StationTimeMatch = result["match"]
    total_m0 = result["total_m0"]

    print(f"\n=== station ({match.table_xp:.4f}, {match.table_yp:.4f})  time {match.time} ===")
    print(f"total m0 (from SPECOUT 2D spectrum): {total_m0:.6f} m^2")

    print(f"\n  neighbourhood (decompose_spectrum, +/-4-bin, CURRENT DEFAULT):")
    print(f"    partition count: {len(result['neighbourhood'])}")
    for p in result["neighbourhood"]:
        print(f"      {_format_partition_row(p['height'], p['period'], p['direction'])}  (m0={p['energy']:.6f})")
    n_sum = result["neighbourhood_sum_m0"]
    ratio = (n_sum / total_m0 * 100.0) if total_m0 > 0 else float("nan")
    print(f"    Sigma partition m0 = {n_sum:.6f} m^2  ({ratio:.1f}% of total m0)")
    if result["close_neighbourhood"]:
        for i, j, sep in result["close_neighbourhood"]:
            print(f"    ** partitions {i} and {j} are only {sep:.1f} deg apart **")

    print(f"\n  watershed (SWAN TABLE PT*, Hanson & Phillips, NOT wired to production):")
    print(f"    partition count: {len(result['watershed'])} "
          f"({len(result['watershed_swell_only'])} swell + "
          f"{len(result['watershed']) - len(result['watershed_swell_only'])} wind sea)")
    for p in result["watershed"]:
        tag = "windsea" if p["is_wind_sea"] else "swell  "
        print(f"      [{tag}] partition {p['partition_index']:>2}: "
              f"{_format_partition_row(p['height'], p['period'], p['direction'])}  (m0={p['energy']:.6f})")
    w_sum = result["watershed_sum_m0"]
    ratio_w = (w_sum / total_m0 * 100.0) if total_m0 > 0 else float("nan")
    print(f"    Sigma partition m0 = {w_sum:.6f} m^2  ({ratio_w:.1f}% of total m0)")
    if result["close_watershed"]:
        for i, j, sep in result["close_watershed"]:
            print(f"    ** partitions {i} and {j} are only {sep:.1f} deg apart **")


def print_summary(results: list[dict[str, Any]]) -> None:
    n = len(results)
    if n == 0:
        print("\nNo matched (station, timestep) pairs - nothing to summarize.")
        return

    avg_n_count = sum(len(r["neighbourhood"]) for r in results) / n
    avg_w_count = sum(len(r["watershed"]) for r in results) / n

    n_ratios = [
        r["neighbourhood_sum_m0"] / r["total_m0"] for r in results if r["total_m0"] > 0
    ]
    w_ratios = [
        r["watershed_sum_m0"] / r["total_m0"] for r in results if r["total_m0"] > 0
    ]
    avg_n_ratio = sum(n_ratios) / len(n_ratios) * 100.0 if n_ratios else float("nan")
    avg_w_ratio = sum(w_ratios) / len(w_ratios) * 100.0 if w_ratios else float("nan")

    close_cases = [r for r in results if r["close_neighbourhood"] or r["close_watershed"]]

    print("\n" + "=" * 72)
    print(f"SUMMARY across {n} matched (station, timestep) pair(s)")
    print("=" * 72)
    print(f"  avg partition count   neighbourhood={avg_n_count:.2f}   watershed={avg_w_count:.2f}")
    print(f"  avg Sigma-m0/total-m0 neighbourhood={avg_n_ratio:.1f}%   watershed={avg_w_ratio:.1f}%")
    print(f"  (100% = perfect conservation; >100% = double-counted energy, matching the plan's")
    print(f"   finding 5 hypothesis for the +/-4-bin window; <100% = energy uncounted by any partition)")
    print(f"  {len(close_cases)}/{n} pair(s) had partitions within the close-direction threshold")
    print(f"  on at least one algorithm - reported below for the operator to judge; this script")
    print(f"  measures counts and m0 ratios, it does not decide what counts as 'resolved':")
    for r in close_cases:
        m = r["match"]
        n_sum = r["neighbourhood_sum_m0"]
        w_sum = r["watershed_sum_m0"]
        n_ratio = (n_sum / r["total_m0"] * 100.0) if r["total_m0"] > 0 else float("nan")
        w_ratio = (w_sum / r["total_m0"] * 100.0) if r["total_m0"] > 0 else float("nan")
        print(
            f"    time={m.time} station=({m.table_xp:.4f},{m.table_yp:.4f}): "
            f"neighbourhood partition_count={len(r['neighbourhood'])} "
            f"close_pairs={[(round(s, 1)) for _, _, s in r['close_neighbourhood']]} "
            f"sigma_m0/total={n_ratio:.1f}%  |  "
            f"watershed partition_count={len(r['watershed'])} "
            f"close_pairs={[(round(s, 1)) for _, _, s in r['close_watershed']]} "
            f"sigma_m0/total={w_ratio:.1f}%"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("table_file", type=Path, help="SWAN TABLE_*.txt with PT* columns")
    parser.add_argument("spec_file", type=Path, help="Matching SWAN SPEC_*.txt (SPECOUT SPEC2D ABS)")
    parser.add_argument(
        "--coord-tolerance-deg", type=float, default=0.003,
        help="Coordinate matching tolerance in degrees (default 0.003, matches swan_runner.py)",
    )
    parser.add_argument(
        "--close-partition-threshold-deg", type=float, default=15.0,
        help="Directional separation (deg) below which two partitions are flagged as 'close' "
             "(default 15, comfortably covers the 2026-07-25 12-degree case)",
    )
    args = parser.parse_args(argv)

    if not args.table_file.exists():
        print(f"error: TABLE file not found: {args.table_file}", file=sys.stderr)
        return 2
    if not args.spec_file.exists():
        print(f"error: SPEC file not found: {args.spec_file}", file=sys.stderr)
        return 2

    table_text = args.table_file.read_text(encoding="utf-8", errors="replace")
    spec_text = args.spec_file.read_text(encoding="utf-8", errors="replace")

    table_rows = parse_table_pt_partitions(table_text)
    if not table_rows:
        print(f"error: no PT* partition rows parsed from {args.table_file}", file=sys.stderr)
        return 1

    try:
        specout = parse_specout_file_multi(spec_text)
    except SpecoutParseError as exc:
        print(f"error: could not parse {args.spec_file}: {exc}", file=sys.stderr)
        return 1

    matches = _match_table_rows_to_spec_stations(
        table_rows, specout.station_lonlat, specout.station_timesteps, args.coord_tolerance_deg,
    )
    if not matches:
        print("error: zero (station, timestep) pairs matched between the two files", file=sys.stderr)
        return 1

    results = [compare_one(m, args.close_partition_threshold_deg) for m in matches]

    for result in results:
        print_comparison(result)
    print_summary(results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
