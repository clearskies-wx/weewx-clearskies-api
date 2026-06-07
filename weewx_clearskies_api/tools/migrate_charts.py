"""migrate_charts — Belchertown graphs.conf → Clear Skies charts.conf migration tool.

CLI entry point: clearskies-migrate-charts

Usage
-----
    clearskies-migrate-charts /path/to/graphs.conf -o /path/to/charts.conf
    clearskies-migrate-charts /path/to/graphs.conf --dry-run --verbose

This tool is STANDALONE — it imports only configobj and the Python standard
library.  It does NOT import FastAPI, SQLAlchemy, or any other API-runtime
dependency.  It can therefore be run without a running database or server.

Exit codes
----------
    0  success
    1  input file not found or not valid ConfigObj
    2  output could not be written
"""

from __future__ import annotations

import argparse
import sys
from io import StringIO
from pathlib import Path

import configobj

# ---------------------------------------------------------------------------
# Keys that are Belchertown-only and have no equivalent in Clear Skies.
# The tool emits a # NOTE comment so the operator is aware.
# ---------------------------------------------------------------------------

# Keys that appear in Belchertown [group] sections but have no effect in
# Clear Skies.  They are annotated with # NOTE comments in the output.
_GROUP_UNSUPPORTED: frozenset[str] = frozenset(
    {
        "generate",  # Belchertown report-cadence flag; no-op in Clear Skies
    }
)

# Note: [[[[states]]]] subsections are now supported by the Clear Skies parser
# and are passed through without annotation.

# ---------------------------------------------------------------------------
# Keys present in Belchertown series sections that need an INI-level rename.
# Both formats use the same camelCase INI key names (yAxis, lineWidth, etc.),
# so there are actually NO renames needed at the config-file level.
# The Python dataclass fields are snake_case, but the INI keys are preserved
# as-is.  This dict is kept here for documentation; it is empty by design.
# ---------------------------------------------------------------------------
_SERIES_KEY_RENAMES: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_header(source_path: Path) -> list[str]:
    """Return the migration header comment lines."""
    return [
        "# Converted from Belchertown graphs.conf by clearskies-migrate-charts",
        f"# Source: {source_path}",
        "#",
        "# INI key names are identical between Belchertown and Clear Skies by design.",
        "# Lines marked '# NOTE:' carry migration guidance for the operator",
        "# about Belchertown-only keys that have no equivalent in Clear Skies.",
        "#",
    ]


def _walk_section(  # noqa: C901  — intentionally comprehensive section walker
    section: configobj.Section,
    depth: int,
    verbose: bool,
    log: list[str],
    unsupported_keys: list[str],
) -> configobj.Section:
    """Recursively walk a ConfigObj section and annotate unsupported keys.

    Returns a *new* Section (via ConfigObj string round-trip) that has
    unsupported keys promoted to inline comments.  Because ConfigObj does
    not support inline comments in section bodies directly, we use the
    ``section.comments`` and ``section.inline_comments`` dicts to inject
    comment lines before each key.

    Args:
        section: The ConfigObj section to walk.
        depth: Nesting depth (0 = global, 1 = group, 2 = chart, 3 = series).
        verbose: If True, log every key processed.
        log: Mutable list of log lines to append to.
        unsupported_keys: Mutable list of fully-qualified unsupported key names.

    Returns:
        The (mutated) section — modifications are in-place on ConfigObj objects.
    """
    indent = "  " * depth

    for key in list(section.scalars):
        fully_qualified = f"{'[' * depth}{section.name}{']: ' if depth else ''}{key}"

        # Check for unsupported keys at group level (depth=1)
        if depth == 1 and key in _GROUP_UNSUPPORTED:
            val = section[key]
            msg = (
                f"# NOTE: '{key}' is a Belchertown-only key (no effect in Clear Skies)."
                f"  Value was: {val}"
            )
            # Replace any pre-existing comments with a single NOTE line.
            # (ConfigObj may have already gathered related comment lines from
            # earlier in the file and attached them to this key's comments list.)
            section.comments[key] = [msg]
            unsupported_keys.append(fully_qualified)
            if verbose:
                log.append(f"{indent}[NOTED] {key} = {section[key]}")
        else:
            if verbose:
                log.append(f"{indent}[COPY]  {key} = {section[key]}")

    # Recurse into subsections
    for sub_id in list(section.sections):
        sub = section[sub_id]
        if not isinstance(sub, configobj.Section):
            continue

        # [[[[states]]]] subsections are now supported by the Clear Skies parser
        # and are passed through without annotation.
        if sub_id == "states":
            if verbose:
                log.append(f"{indent}[COPY]  [[[[states]]]] section")
            _walk_section(sub, depth + 1, verbose, log, unsupported_keys)
        else:
            _walk_section(sub, depth + 1, verbose, log, unsupported_keys)

    return section


# ---------------------------------------------------------------------------
# Statistics collector
# ---------------------------------------------------------------------------


def _count_structure(cfg: configobj.ConfigObj) -> tuple[int, int, int]:
    """Count groups, charts, and series in the parsed ConfigObj.

    Returns:
        Tuple of (num_groups, num_charts, num_series).
    """
    num_groups = 0
    num_charts = 0
    num_series = 0
    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        num_groups += 1
        for chart_id in group.sections:
            chart = group[chart_id]
            if not isinstance(chart, configobj.Section):
                continue
            num_charts += 1
            for series_id in chart.sections:
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue
                if series_id not in ("marker", "states"):
                    num_series += 1
    return num_groups, num_charts, num_series


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def migrate(
    source_path: Path,
    verbose: bool = False,
) -> tuple[str, list[str], list[str]]:
    """Read a Belchertown graphs.conf and produce a Clear Skies charts.conf string.

    Args:
        source_path: Path to the Belchertown graphs.conf file.
        verbose: If True, include detailed mapping log in the returned log list.

    Returns:
        A 3-tuple of:
          - output_text: The converted charts.conf content as a string.
          - log_lines: Human-readable processing log (empty when verbose=False).
          - warnings: List of warning strings (unsupported keys, etc.).

    Raises:
        FileNotFoundError: If source_path does not exist.
        configobj.ConfigspecError: If the file cannot be parsed as ConfigObj.
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Input file not found: {source_path}")

    # Read the source file as text lines so that ConfigObj can parse it
    # without being bound to the file path.  When ConfigObj is loaded with a
    # filename string its write() method writes back to that file and returns
    # None; loading from a list of strings makes write() return a list instead.
    # Use utf-8-sig encoding to transparently strip a UTF-8 BOM if present
    # (Windows-edited files often have a BOM that ConfigObj rejects).
    source_lines = source_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    # Load with interpolation disabled (same as the Clear Skies parser).
    cfg = configobj.ConfigObj(infile=source_lines, interpolation=False)

    log: list[str] = []
    unsupported_keys: list[str] = []

    if verbose:
        log.append(f"Reading: {source_path}")
        log.append(f"Global scalars: {', '.join(cfg.scalars)}")

    # ---- Log global-level scalars ----
    for key in cfg.scalars:
        if verbose:
            log.append(f"[COPY]  {key} = {cfg[key]}")

    # ---- Walk all group sections ----
    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        if verbose:
            log.append(f"[GROUP] [{group_id}]")
        _walk_section(group, depth=1, verbose=verbose, log=log, unsupported_keys=unsupported_keys)

    # ---- Render to string with header ----
    header_lines = _build_header(source_path)

    buf = StringIO()
    # Write header comment block
    for line in header_lines:
        buf.write(line + "\n")
    buf.write("\n")

    # ConfigObj.write() returns a list of strings when loaded from a list of
    # lines (not a filename).  Each entry is one config line without a trailing
    # newline — we add "\n" as we join them.
    cfg_lines = cfg.write() or []
    for raw_line in cfg_lines:
        if isinstance(raw_line, bytes):
            buf.write(raw_line.decode("utf-8") + "\n")
        else:
            buf.write(str(raw_line) + "\n")

    output_text = buf.getvalue()

    warnings: list[str] = []
    if unsupported_keys:
        warnings.append(
            f"Noted {len(unsupported_keys)} unsupported / Belchertown-only key(s):"
        )
        for k in unsupported_keys:
            warnings.append(f"  {k}")

    return output_text, log, warnings


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    p = argparse.ArgumentParser(
        prog="clearskies-migrate-charts",
        description=(
            "Convert a Belchertown skin graphs.conf file to a Clear Skies charts.conf file.\n\n"
            "Most keys are identical between the two formats by design.  "
            "Unsupported keys are noted with comments in the output."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "input",
        metavar="GRAPHS_CONF",
        help="Path to the Belchertown graphs.conf file to convert.",
    )
    p.add_argument(
        "-o",
        "--output",
        metavar="CHARTS_CONF",
        default=None,
        help=(
            "Output path for the converted charts.conf file.  "
            "Defaults to stdout if not specified."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Parse and report issues without writing any output file.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Show detailed key-by-key mapping log.",
    )
    return p


def main() -> None:
    """CLI entry point for clearskies-migrate-charts."""
    parser = _build_parser()
    args = parser.parse_args()

    source_path = Path(args.input)

    # ---- Run migration ----
    try:
        output_text, log_lines, warnings = migrate(
            source_path=source_path,
            verbose=args.verbose,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Could not parse input file: {exc}", file=sys.stderr)
        sys.exit(1)

    # ---- Print verbose log ----
    if args.verbose and log_lines:
        print("=== Migration log ===", file=sys.stderr)
        for line in log_lines:
            print(line, file=sys.stderr)
        print("", file=sys.stderr)

    # ---- Print warnings ----
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    # ---- Collect statistics ----
    try:
        cfg_check = configobj.ConfigObj(str(source_path), interpolation=False)
        n_groups, n_charts, n_series = _count_structure(cfg_check)
        print(
            f"Migrated: {n_groups} group(s), {n_charts} chart(s), {n_series} series",
            file=sys.stderr,
        )
    except Exception as _exc:  # noqa: BLE001
        # Statistics are informational — do not abort the migration on error
        _ = _exc

    # ---- Dry run: stop here ----
    if args.dry_run:
        print("Dry run — no output written.", file=sys.stderr)
        sys.exit(0)

    # ---- Write output ----
    if args.output is None:
        # Write to stdout
        sys.stdout.write(output_text)
    else:
        output_path = Path(args.output)
        try:
            output_path.write_text(output_text, encoding="utf-8")
            print(f"Written: {output_path}", file=sys.stderr)
        except OSError as exc:
            print(f"ERROR: Could not write output: {exc}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
