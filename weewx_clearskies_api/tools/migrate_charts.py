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
# Injection maps — default axis labels, number formats, and observation aliases
# ---------------------------------------------------------------------------

# Subsection names that are metadata/config, not real observation series.
# These are skipped when injecting axis labels, number formats, and aliases.
_SKIP_SERIES: frozenset[str] = frozenset({"marker", "states", "numberFormat"})

# Maps observation type → default yAxis_label for US-unit deployments.
# Only injected when the series does not already carry a yAxis_label.
_DEFAULT_AXIS_LABELS: dict[str, str] = {
    "outTemp": "Temperature (°F)",
    "dewpoint": "Temperature (°F)",
    "windchill": "Temperature (°F)",
    "heatindex": "Temperature (°F)",
    "inTemp": "Temperature (°F)",
    "windSpeed": "Wind Speed (mph)",
    "windGust": "Wind Speed (mph)",
    "windDir": "Wind Direction (°)",
    "barometer": "Barometer (inHg)",
    "pressure": "Barometer (inHg)",
    "altimeter": "Barometer (inHg)",
    "rainRate": "Rain Rate (in/hr)",
    "rain": "Rain (in)",
    "rainTotal": "Rain (in)",
    "radiation": "Solar Radiation (W/m²)",
    "maxSolarRad": "Solar Radiation (W/m²)",
    "UV": "UV Index",
    "lightning_strike_count": "Number of Strikes",
    "lightning_distance": "Distance (miles)",
    "outHumidity": "Humidity (%)",
    "inHumidity": "Humidity (%)",
    "aqi": "AQI",
}

# Maps observation type → number of decimal places for the tooltip/label.
# Only injected when the series does not already have a [[[[numberFormat]]]] subsection.
_DEFAULT_NUMBER_FORMATS: dict[str, int] = {
    "outTemp": 1,
    "dewpoint": 1,
    "windchill": 1,
    "heatindex": 1,
    "barometer": 3,
    "rain": 2,
    "rainRate": 2,
    "rainTotal": 2,
    "windSpeed": 1,
    "windGust": 1,
    "UV": 1,
    "radiation": 0,
    "lightning_strike_count": 0,
    "lightning_distance": 1,
}

# Maps series section name → canonical DB column name.
# Injected as observation_type when the series does not already have one.
_OBSERVATION_ALIASES: dict[str, str] = {
    "rainTotal": "rain",
}

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
# Injection helpers
# ---------------------------------------------------------------------------


def _inject_default_axis_labels(
    cfg: configobj.ConfigObj, verbose: bool, log: list[str]
) -> None:
    """Inject yAxis_label defaults for series that lack one.

    Walks groups → charts → series (3 levels).  For each chart, tracks which
    yAxis indices (0 = left, 1 = right) already have a label committed by any
    series.  For the FIRST series on each axis that still needs a label, looks
    up the observation type in _DEFAULT_AXIS_LABELS and injects it.

    Series named in _SKIP_SERIES (marker, states, numberFormat) are skipped.
    Series that already carry yAxis_label are never overwritten.
    """
    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        for chart_id in group.sections:
            chart = group[chart_id]
            if not isinstance(chart, configobj.Section):
                continue
            # Track which axis indices already have a label within this chart.
            axes_labeled: set[int] = set()
            # First pass: record axes that already have a label.
            for series_id in chart.sections:
                if series_id in _SKIP_SERIES:
                    continue
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue
                if series.get("yAxis_label"):
                    axis_idx = int(series.get("yAxis", "0") or "0")
                    axes_labeled.add(axis_idx)
            # Second pass: inject labels for axes that still need one.
            for series_id in chart.sections:
                if series_id in _SKIP_SERIES:
                    continue
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue
                axis_idx = int(series.get("yAxis", "0") or "0")
                if axis_idx in axes_labeled:
                    continue  # This axis already has a label — skip.
                obs_type = series.get("observation_type", series_id)
                label = _DEFAULT_AXIS_LABELS.get(obs_type)
                if label:
                    series["yAxis_label"] = label
                    axes_labeled.add(axis_idx)
                    if verbose:
                        log.append(
                            f"[INJECT] yAxis_label={label!r} on"
                            f" [{group_id}][{chart_id}][{series_id}]"
                        )


def _inject_number_format_defaults(
    cfg: configobj.ConfigObj, verbose: bool, log: list[str]
) -> None:
    """Inject a [[[[numberFormat]]]] subsection for series that lack one.

    Only injects when the observation type appears in _DEFAULT_NUMBER_FORMATS
    and the series does not already carry a numberFormat subsection.
    Series named in _SKIP_SERIES are skipped.
    """
    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        for chart_id in group.sections:
            chart = group[chart_id]
            if not isinstance(chart, configobj.Section):
                continue
            for series_id in chart.sections:
                if series_id in _SKIP_SERIES:
                    continue
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue
                # Skip if a numberFormat subsection already exists.
                if "numberFormat" in series.sections:
                    continue
                obs_type = series.get("observation_type", series_id)
                decimals = _DEFAULT_NUMBER_FORMATS.get(obs_type)
                if decimals is not None:
                    series["numberFormat"] = {}
                    series["numberFormat"]["decimals"] = str(decimals)
                    if verbose:
                        log.append(
                            f"[INJECT] numberFormat.decimals={decimals} on"
                            f" [{group_id}][{chart_id}][{series_id}]"
                        )


def _inject_observation_aliases(
    cfg: configobj.ConfigObj, verbose: bool, log: list[str]
) -> None:
    """Inject observation_type for series whose section name is a known alias.

    If a series section name matches a key in _OBSERVATION_ALIASES and the
    series does NOT already have observation_type set, the canonical DB column
    name is injected so the dashboard archive fetch uses the correct column.
    Series named in _SKIP_SERIES are skipped.
    """
    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        for chart_id in group.sections:
            chart = group[chart_id]
            if not isinstance(chart, configobj.Section):
                continue
            for series_id in chart.sections:
                if series_id in _SKIP_SERIES:
                    continue
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue
                # Only inject when series name is an alias and obs_type not yet set.
                if series_id in _OBSERVATION_ALIASES and not series.get("observation_type"):
                    db_col = _OBSERVATION_ALIASES[series_id]
                    series["observation_type"] = db_col
                    if verbose:
                        log.append(
                            f"[INJECT] observation_type={db_col!r} on"
                            f" [{group_id}][{chart_id}][{series_id}]"
                        )


def _inject_marker_defaults(
    cfg: configobj.ConfigObj, verbose: bool, log: list[str]
) -> None:
    """Inject markerEnabled (and markerRadius) defaults for all series.

    Walk groups → charts → series.  For each series (skip _SKIP_SERIES):

    * line / spline / area / areaspline  → markerEnabled = false
    * scatter                            → markerEnabled = true, markerRadius = 2
    * lineWidth = '0' on non-scatter     → promote to type = scatter,
                                           markerEnabled = true, markerRadius = 3
                                           (Belchertown's windDir-as-scatter trick)

    Effective type is resolved by walking up: series → chart → 'line'.
    All injections are idempotent (only inject when key absent).
    Series in _SKIP_SERIES are skipped.
    """
    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        for chart_id in group.sections:
            chart = group[chart_id]
            if not isinstance(chart, configobj.Section):
                continue
            chart_type = chart.get("type", "line") or "line"
            for series_id in chart.sections:
                if series_id in _SKIP_SERIES:
                    continue
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue

                # Determine effective type: series → chart → 'line'
                effective_type = (series.get("type") or chart_type or "line").strip().lower()

                # Check for the lineWidth=0 scatter trick (before scatter type check)
                line_width_val = series.get("lineWidth")
                if (
                    line_width_val is not None
                    and str(line_width_val).strip() == "0"
                    and effective_type != "scatter"
                ):
                    # Promote to scatter — Belchertown's trick for rendering
                    # windDir (and similar) as scatter points on a line chart.
                    if "type" not in series:
                        series["type"] = "scatter"
                        if verbose:
                            log.append(
                                f"[INJECT] type=scatter (lineWidth=0 promotion) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )
                    if "markerEnabled" not in series:
                        series["markerEnabled"] = "true"
                        if verbose:
                            log.append(
                                f"[INJECT] markerEnabled=true (lineWidth=0 promotion) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )
                    if "markerRadius" not in series:
                        series["markerRadius"] = "3"
                        if verbose:
                            log.append(
                                f"[INJECT] markerRadius=3 (lineWidth=0 promotion) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )
                    continue

                # Normal type dispatch
                if effective_type == "scatter":
                    if "markerEnabled" not in series:
                        series["markerEnabled"] = "true"
                        if verbose:
                            log.append(
                                f"[INJECT] markerEnabled=true (scatter) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )
                    if "markerRadius" not in series:
                        series["markerRadius"] = "2"
                        if verbose:
                            log.append(
                                f"[INJECT] markerRadius=2 (scatter) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )
                elif effective_type in ("line", "spline", "area", "areaspline"):
                    if "markerEnabled" not in series:
                        series["markerEnabled"] = "false"
                        if verbose:
                            log.append(
                                f"[INJECT] markerEnabled=false ({effective_type}) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )


def _inject_axis_defaults(
    cfg: configobj.ConfigObj, verbose: bool, log: list[str]
) -> None:
    """Inject special axis defaults for barometer and rain series.

    Walk groups → charts → series.  For each series (skip _SKIP_SERIES):

    * barometer / pressure / altimeter → yAxisTickDecimals = 2
      (inHg values need 2 decimal places for legible Y-axis ticks)
    * rain / rainRate / rainTotal      → yAxis_min = 0
      (precipitation can't be negative)

    The observation type is determined from:
      1. The series.observation_type key (injected by _inject_observation_aliases)
      2. The series section name (key)

    All injections are idempotent (only inject when key absent).
    Series in _SKIP_SERIES are skipped.
    """
    _BAROMETER_TYPES = frozenset({"barometer", "pressure", "altimeter"})
    _RAIN_TYPES = frozenset({"rain", "rainRate", "rainTotal"})

    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        for chart_id in group.sections:
            chart = group[chart_id]
            if not isinstance(chart, configobj.Section):
                continue
            for series_id in chart.sections:
                if series_id in _SKIP_SERIES:
                    continue
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue

                # Observation type: explicit override wins, else section name
                obs_type = series.get("observation_type") or series_id

                if obs_type in _BAROMETER_TYPES:
                    if "yAxisTickDecimals" not in series:
                        series["yAxisTickDecimals"] = "2"
                        if verbose:
                            log.append(
                                f"[INJECT] yAxisTickDecimals=2 (barometer) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )

                if obs_type in _RAIN_TYPES:
                    if "yAxis_min" not in series:
                        series["yAxis_min"] = "0"
                        if verbose:
                            log.append(
                                f"[INJECT] yAxis_min=0 (rain) on"
                                f" [{group_id}][{chart_id}][{series_id}]"
                            )


def _inject_cumulative_aggregate(
    cfg: configobj.ConfigObj, verbose: bool, log: list[str]
) -> None:
    """Promote the rainTotal series aggregate_type to sumcumulative.

    Walk groups → charts → series.  For each series (skip _SKIP_SERIES):

    * series_name.lower() == 'raintotal' → aggregate_type = sumcumulative

    Idempotent: if aggregate_type is already sumcumulative, no change is made.
    Series in _SKIP_SERIES are skipped.
    """
    for group_id in cfg.sections:
        group = cfg[group_id]
        if not isinstance(group, configobj.Section):
            continue
        for chart_id in group.sections:
            chart = group[chart_id]
            if not isinstance(chart, configobj.Section):
                continue
            for series_id in chart.sections:
                if series_id in _SKIP_SERIES:
                    continue
                series = chart[series_id]
                if not isinstance(series, configobj.Section):
                    continue

                if (
                    series_id.lower() == "raintotal"
                    and series.get("aggregate_type") != "sumcumulative"
                ):
                    series["aggregate_type"] = "sumcumulative"
                    if verbose:
                        log.append(
                            f"[INJECT] aggregate_type=sumcumulative (rainTotal) on"
                            f" [{group_id}][{chart_id}][{series_id}]"
                        )


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

    # ---- T0.4: Strip stale comments injected by previous migration runs ----
    # Running the tool multiple times accumulates "# UNSUPPORTED:" and "# NOTE:"
    # lines in the operator's graphs.conf (because they appear in the output and
    # the operator may copy that output back).  Filter them before parsing so
    # they don't compound on every run.
    source_lines = [
        line
        for line in source_lines
        if not line.lstrip().startswith("# UNSUPPORTED:")
        and not line.lstrip().startswith("# NOTE:")
    ]

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

    # ---- Inject default axis labels for common observation types ----
    # Belchertown derives axis labels automatically from weewx's label system.
    # Clear Skies doesn't have that — the migration tool injects sensible defaults
    # so operators don't get unlabeled axes.
    _inject_default_axis_labels(cfg, verbose, log)

    # ---- Inject default numberFormat subsections ----
    # Belchertown inherits decimal precision from weewx's unit system at runtime.
    # Clear Skies needs explicit decimals in each series so the tooltip formatter
    # knows how many decimal places to show.
    _inject_number_format_defaults(cfg, verbose, log)

    # ---- Inject observation_type for known series-name aliases ----
    # Some series use a display name (e.g., "rainTotal") that differs from the
    # actual database column ("rain").  Inject observation_type so the archive
    # fetch uses the correct column.
    _inject_observation_aliases(cfg, verbose, log)

    # ---- Inject markerEnabled defaults for all series ----
    # Belchertown's Highcharts defaults hide markers on line/spline/area series
    # but the Clear Skies dashboard needs explicit settings.  Also promotes
    # lineWidth=0 series (the windDir scatter trick) to type=scatter.
    _inject_marker_defaults(cfg, verbose, log)

    # ---- Inject special axis defaults for barometer and rain ----
    # Barometer needs yAxisTickDecimals=2 for proper inHg formatting.
    # Rain/rainRate/rainTotal need yAxisMin=0 (can't be negative).
    _inject_axis_defaults(cfg, verbose, log)

    # ---- Promote rainTotal series to sumcumulative aggregate ----
    # Belchertown computes a running daily total for rainTotal at render time.
    # Clear Skies requires the aggregate_type to be explicit in charts.conf.
    _inject_cumulative_aggregate(cfg, verbose, log)

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
