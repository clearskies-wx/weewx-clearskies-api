"""Charts configuration parser.

Loads operator-defined chart configuration from charts.conf (ConfigObj/INI format)
and builds a ChartsConfig dataclass tree.

Search order (ADR-027 pattern):
  1. CLEARSKIES_CHARTS_CONFIG env var (if set, points directly to charts.conf)
  2. /etc/weewx-clearskies/charts.conf
  3. ~/.config/weewx-clearskies/charts.conf  (XDG fallback)
  4. Built-in default (weewx_clearskies_api/data/charts.conf.default)
"""

from __future__ import annotations

import logging
import os
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any

import configobj  # type: ignore[import-untyped]

from weewx_clearskies_api.db.reflection import ColumnRegistry
from weewx_clearskies_api.models.chart_config import (
    ChartConfig,
    ChartGroupConfig,
    ChartsConfig,
    SeriesConfig,
)

logger = logging.getLogger(__name__)

_CHARTS_CONFIG_SEARCH_PATH: list[Path] = [
    Path("/etc/weewx-clearskies/charts.conf"),
    Path.home() / ".config" / "weewx-clearskies" / "charts.conf",
]

# ---------------------------------------------------------------------------
# Type-conversion helpers
# ---------------------------------------------------------------------------


def _to_bool(val: str | bool) -> bool:
    """Convert a string or bool value to Python bool."""
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


def _to_int_or_none(val: str | int | None) -> int | None:
    """Convert to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _to_float_or_none(val: str | float | None) -> float | None:
    """Convert to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_int_or_str(val: str | int | None) -> int | str | None:
    """Parse time_length: seconds (int) or keyword string ('month', 'year', 'all')."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return str(val).strip()


def _split_csv(val: str | list[Any] | None) -> list[str]:
    """Split a comma-separated string into a list, or return list as-is."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    return [s.strip() for s in str(val).split(",") if s.strip()]


def _split_csv_int(val: str | list[Any] | None) -> list[int]:
    """Split a comma-separated string of integers."""
    raw = _split_csv(val)
    result = []
    for item in raw:
        try:
            result.append(int(item))
        except ValueError:
            logger.warning("Skipping non-integer year value: %s", item)
    return result


# ---------------------------------------------------------------------------
# Config file discovery
# ---------------------------------------------------------------------------


def find_charts_config() -> Path | None:
    """Find the operator charts configuration file.

    Returns:
        Path to the config file if found, None otherwise.
    """
    env = os.environ.get("CLEARSKIES_CHARTS_CONFIG", "").strip()
    if env:
        return Path(env)
    for candidate in _CHARTS_CONFIG_SEARCH_PATH:
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_global_defaults(cfg: configobj.ConfigObj) -> dict[str, Any]:
    """Extract top-level scalar keys (global defaults) from ConfigObj.

    ConfigObj distinguishes scalars (cfg.scalars) from subsections (cfg.sections).
    Only scalars are global defaults; sections are chart groups.

    Returns:
        Dict with keys: aggregate_type, time_length, type, colors,
        tooltip_date_format.
    """
    raw_type = cfg.get("type", "line")
    # Strip inline comments (ConfigObj keeps inline comments in the value
    # when interpolation=False and the comment char is '#').
    # e.g. "90000 # Last 25 hours" → "90000"
    def _strip_comment(s: str) -> str:
        return s.split("#")[0].strip() if isinstance(s, str) else s

    return {
        "aggregate_type": cfg.get("aggregate_type") or None,
        "time_length": _to_int_or_str(_strip_comment(cfg.get("time_length", ""))),
        "type": str(raw_type).strip() if raw_type else "line",
        "colors": _split_csv(cfg.get("colors")),
        "tooltip_date_format": cfg.get("tooltip_date_format") or None,
    }


def _strip_comment(s: str | None) -> str | None:
    """Strip trailing inline comments from a ConfigObj value string."""
    if s is None:
        return None
    if isinstance(s, str):
        return s.split("#")[0].strip() or None
    return s


def _parse_series(series_id: str, section: configobj.Section) -> SeriesConfig:
    """Parse a [[[series]]] subsection into a SeriesConfig.

    Args:
        series_id: The INI section name (e.g. "outTemp", "windRose").
        section: The ConfigObj Section object.

    Returns:
        Populated SeriesConfig.
    """
    beaufort_colors: dict[int, str] = {}
    for i in range(7):
        key = f"beauford{i}"
        val = section.get(key)
        if val is not None:
            beaufort_colors[i] = str(val).strip()

    # Marker sub-section ([[[[marker]]]])
    marker_enabled: bool | None = None
    marker_radius: int | None = None
    marker_section = section.get("marker")
    if isinstance(marker_section, configobj.Section):
        if "enabled" in marker_section:
            marker_enabled = _to_bool(marker_section["enabled"])
        if "radius" in marker_section:
            r = _to_int_or_none(_strip_comment(marker_section.get("radius")))
            if r is not None:
                marker_radius = r

    def _sget(key: str) -> str | None:
        return _strip_comment(section.get(key))

    # z_index: INI key is zIndex
    z_index_raw = _sget("zIndex")
    z_index = _to_int_or_none(z_index_raw)

    # y_axis: INI key is yAxis
    y_axis_raw = _sget("yAxis")
    y_axis = _to_int_or_none(y_axis_raw)

    # line_width: INI key is lineWidth
    lw_raw = _sget("lineWidth")
    line_width = _to_int_or_none(lw_raw)

    # area_display
    ad_raw = _sget("area_display")
    area_display = _to_int_or_none(ad_raw)

    # use_custom_sql boolean
    ucs_raw = section.get("use_custom_sql")
    use_custom_sql: bool = _to_bool(ucs_raw) if ucs_raw is not None else False

    # connect_nulls
    cn_raw = section.get("connectNulls")
    connect_nulls: bool | None = _to_bool(cn_raw) if cn_raw is not None else None

    # visible
    vis_raw = section.get("visible")
    visible: bool | None = _to_bool(vis_raw) if vis_raw is not None else None

    return SeriesConfig(
        series_id=series_id,
        observation_type=_sget("observation_type"),
        name=_sget("name"),
        color=_sget("color"),
        type=_sget("type"),
        z_index=z_index,
        y_axis=y_axis,
        y_axis_min=_to_float_or_none(_sget("yAxis_min")),
        y_axis_max=_to_float_or_none(_sget("yAxis_max")),
        y_axis_label=_sget("yAxis_label"),
        y_axis_tick_interval=_to_float_or_none(_sget("yAxis_tickinterval")),
        line_width=line_width,
        connect_nulls=connect_nulls,
        visible=visible,
        opacity=_to_float_or_none(_sget("opacity")),
        stacking=_sget("stacking"),
        aggregate_type=_sget("aggregate_type"),
        average_type=_sget("average_type"),
        range_type=_sget("range_type"),
        area_display=area_display,
        use_custom_sql=use_custom_sql,
        custom_sql_query=_sget("custom_sql_query"),
        x_column=_sget("x_column"),
        y_column=_sget("y_column"),
        marker_enabled=marker_enabled,
        marker_radius=marker_radius,
        beaufort_colors=beaufort_colors,
    )


def _parse_chart(chart_id: str, section: configobj.Section) -> ChartConfig:
    """Parse a [[chart]] subsection into a ChartConfig.

    Args:
        chart_id: The INI section name (e.g. "chart1", "roseplt").
        section: The ConfigObj Section object.

    Returns:
        Populated ChartConfig with child SeriesConfig objects.
    """

    def _sget(key: str) -> str | None:
        return _strip_comment(section.get(key))

    # connect_nulls: INI key is connectNulls
    cn_raw = section.get("connectNulls")
    connect_nulls: bool | None = _to_bool(cn_raw) if cn_raw is not None else None

    # force_full_year
    ffy_raw = section.get("force_full_year")
    force_full_year: bool | None = _to_bool(ffy_raw) if ffy_raw is not None else None

    # x_axis_categories: can be a ConfigObj list or comma-separated string
    xac_raw = section.get("xAxis_categories")
    x_axis_categories: list[str] = _split_csv(xac_raw)

    # Walk subsections for series
    series: list[SeriesConfig] = []
    for sub_id in section.sections:
        sub = section[sub_id]
        if not isinstance(sub, configobj.Section):
            continue
        # Skip non-series subsections like "marker", "states"
        # Series subsections are those that aren't known structural sub-keys
        if sub_id in ("marker", "states"):
            continue
        series.append(_parse_series(sub_id, sub))

    return ChartConfig(
        chart_id=chart_id,
        title=_sget("title"),
        type=_sget("type"),
        connect_nulls=connect_nulls,
        y_axis_min=_to_float_or_none(_sget("yAxis_min")),
        aggregate_type=_sget("aggregate_type"),
        aggregate_interval=_to_int_or_none(_strip_comment(section.get("aggregate_interval"))),
        x_axis_groupby=_sget("xAxis_groupby"),
        x_axis_categories=x_axis_categories,
        force_full_year=force_full_year,
        series=series,
    )


def _parse_group(
    group_id: str,
    section: configobj.Section,
    global_defaults: dict[str, Any],
) -> ChartGroupConfig:
    """Parse a top-level [group] INI section into a ChartGroupConfig.

    Args:
        group_id: The INI section name (e.g. "homepage", "monthly").
        section: The ConfigObj Section object.
        global_defaults: Global defaults from _parse_global_defaults().

    Returns:
        Populated ChartGroupConfig with child ChartConfig objects.
    """

    def _sget(key: str) -> str | None:
        return _strip_comment(section.get(key))

    # Booleans
    def _sbool(key: str, default: bool) -> bool:
        raw = section.get(key)
        return _to_bool(raw) if raw is not None else default

    # show_button defaults True per dataclass
    show_button = _sbool("show_button", True)
    enable_date_ranges = _sbool("enable_date_ranges", False)
    enable_monthly_breakdown = _sbool("enable_monthly_breakdown", False)
    force_full_year = _sbool("force_full_year", False)
    start_at_beginning_of_month = _sbool("start_at_beginning_of_month", False)

    # time_length may be int or keyword string (e.g. "month", "year", "all")
    tl_raw = _strip_comment(section.get("time_length"))
    time_length: int | str | None = (
        _to_int_or_str(tl_raw) if tl_raw is not None else global_defaults.get("time_length")
    )

    # Integers with comment stripping
    gapsize = _to_int_or_none(_strip_comment(section.get("gapsize")))
    aggregate_interval = _to_int_or_none(_strip_comment(section.get("aggregate_interval")))
    timespan_start = _to_int_or_none(_sget("timespan_start"))
    timespan_stop = _to_int_or_none(_sget("timespan_stop"))

    # Lists
    rolling_ranges = _split_csv(section.get("rolling_ranges"))
    available_years = _split_csv_int(section.get("available_years"))

    # aggregate_type falls back to global default if not set locally
    ag_type = _sget("aggregate_type") or global_defaults.get("aggregate_type")

    # tooltip_date_format falls back to global default
    tdf = _sget("tooltip_date_format") or global_defaults.get("tooltip_date_format")

    # Walk subsections for charts
    charts: list[ChartConfig] = []
    for sub_id in section.sections:
        sub = section[sub_id]
        if not isinstance(sub, configobj.Section):
            continue
        charts.append(_parse_chart(sub_id, sub))

    return ChartGroupConfig(
        group_id=group_id,
        title=_sget("title"),
        show_button=show_button,
        button_text=_sget("button_text"),
        type=_sget("type"),
        enable_date_ranges=enable_date_ranges,
        rolling_ranges=rolling_ranges,
        available_years=available_years,
        enable_monthly_breakdown=enable_monthly_breakdown,
        time_length=time_length,
        timespan_start=timespan_start,
        timespan_stop=timespan_stop,
        tooltip_date_format=tdf,
        gapsize=gapsize,
        aggregate_interval=aggregate_interval,
        aggregate_type=ag_type,
        force_full_year=force_full_year,
        start_at_beginning_of_month=start_at_beginning_of_month,
        page_content=_sget("page_content"),
        charts=charts,
    )


def _parse_configobj(cfg: configobj.ConfigObj) -> ChartsConfig:
    """Parse a loaded ConfigObj into a ChartsConfig dataclass tree.

    Args:
        cfg: Parsed ConfigObj (interpolation=False).

    Returns:
        Populated ChartsConfig.
    """
    global_defaults = _parse_global_defaults(cfg)

    groups: list[ChartGroupConfig] = []
    for section_id in cfg.sections:
        section = cfg[section_id]
        if not isinstance(section, configobj.Section):
            continue
        groups.append(_parse_group(section_id, section, global_defaults))

    return ChartsConfig(
        aggregate_type=global_defaults.get("aggregate_type"),
        time_length=global_defaults.get("time_length"),
        type=global_defaults.get("type") or "line",
        colors=global_defaults.get("colors") or [],
        tooltip_date_format=global_defaults.get("tooltip_date_format"),
        groups=groups,
    )


# ---------------------------------------------------------------------------
# Built-in default loader
# ---------------------------------------------------------------------------


def _load_builtin_default() -> ChartsConfig:
    """Load and parse the bundled default charts.conf.

    Uses importlib.resources to locate the package-data file so it works
    when the package is installed as a wheel or editable install.

    Returns:
        ChartsConfig populated from charts.conf.default.
    """
    pkg_data = pkg_files("weewx_clearskies_api.data")
    default_path = pkg_data.joinpath("charts.conf.default")
    # Resolve to a real filesystem path for ConfigObj
    import importlib.resources as ir
    # Use as_file context manager to get a real path (needed for ConfigObj)
    with ir.as_file(default_path) as real_path:
        cfg = configobj.ConfigObj(str(real_path), interpolation=False)
        return _parse_configobj(cfg)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_charts_config(config_path: Path | None = None) -> ChartsConfig:
    """Find, load, and parse the operator charts configuration.

    Search order when config_path is None:
      1. CLEARSKIES_CHARTS_CONFIG env var
      2. /etc/weewx-clearskies/charts.conf
      3. ~/.config/weewx-clearskies/charts.conf
      4. Built-in default (weewx_clearskies_api/data/charts.conf.default)

    When config_path is provided, it is used directly (test/override mode).
    If the provided path does not exist, a warning is logged and built-in
    defaults are returned.

    Args:
        config_path: Optional explicit path to a charts.conf file.

    Returns:
        ChartsConfig populated from the found configuration.
    """
    path = config_path or find_charts_config()

    if path is None:
        logger.info(
            "No charts.conf found in search path — using built-in defaults"
        )
        return _load_builtin_default()

    if not path.exists():
        logger.warning(
            "charts.conf specified at %s does not exist — using built-in defaults",
            path,
        )
        return _load_builtin_default()

    try:
        cfg = configobj.ConfigObj(str(path), interpolation=False)
    except Exception:
        logger.exception(
            "Failed to parse charts.conf at %s — using built-in defaults", path
        )
        return _load_builtin_default()

    logger.debug("Charts configuration loaded from %s", path)
    return _parse_configobj(cfg)


# ---------------------------------------------------------------------------
# Self-hide pruning
# ---------------------------------------------------------------------------

# Belchertown virtual series that don't map 1-to-1 to a DB column.
# Key: series_id used in graphs.conf; Value: the underlying DB column to
# check availability for.  "rainTotal" is the cumulative-rain series — it
# is derived at render time from the "rain" column, so it should only be
# pruned when "rain" itself is not available.
_VIRTUAL_SERIES_MAP: dict[str, str] = {
    "rainTotal": "rain",
}


def prune_charts_config(
    config: ChartsConfig, registry: ColumnRegistry
) -> ChartsConfig:
    """Return a new ChartsConfig with unavailable series/charts/groups removed.

    A series is kept when ANY of these conditions holds:

    * ``use_custom_sql`` is True — the series fetches its own data; it does
      not depend on a weewx archive column at all.
    * The effective observation type (``series.observation_type`` if set,
      otherwise ``series.series_id``) is in the set of canonical names
      reported by the registry.
    * The series_id is ``"windRose"`` AND both ``"windSpeed"`` and
      ``"windDir"`` are in the registry — windRose consumes both columns.
    * ``range_type`` is not None AND ``range_type`` is in the registry — this
      covers weather-range / radial chart series.
    * The series_id is listed in ``_VIRTUAL_SERIES_MAP`` and its mapped
      underlying column is in the registry (e.g. "rainTotal" → "rain").

    Charts with no surviving series are dropped.  Groups with no surviving
    charts are dropped.  The input ``config`` is never mutated.

    Args:
        config: The full operator-loaded ChartsConfig.
        registry: The ColumnRegistry built from schema reflection at startup.

    Returns:
        A new ChartsConfig containing only the series/charts/groups that have
        at least one available data source.
    """
    # Build the set of available canonical names (same pattern as charts.py).
    available: set[str] = {
        info.canonical_name
        for info in registry.stock.values()
        if info.canonical_name is not None
    }

    pruned_groups: list[ChartGroupConfig] = []

    for group in config.groups:
        pruned_charts: list[ChartConfig] = []

        for chart in group.charts:
            surviving_series: list[SeriesConfig] = []

            for series in chart.series:
                # 1. Custom SQL series are never pruned.
                if series.use_custom_sql:
                    surviving_series.append(series)
                    continue

                # 2. Effective observation type — explicit override wins.
                effective = series.observation_type or series.series_id

                # 3. Wind rose: check both underlying columns explicitly.
                if series.series_id == "windRose":
                    if "windSpeed" in available and "windDir" in available:
                        surviving_series.append(series)
                    else:
                        logger.debug(
                            "Pruned series %s from chart %s"
                            " (windRose requires windSpeed + windDir,"
                            " one or both missing)",
                            series.series_id,
                            chart.chart_id,
                        )
                    continue

                # 4. Weather range / radial series — check range_type column.
                if series.range_type is not None:
                    if series.range_type in available:
                        surviving_series.append(series)
                    else:
                        logger.debug(
                            "Pruned series %s from chart %s"
                            " (range_type %r not available)",
                            series.series_id,
                            chart.chart_id,
                            series.range_type,
                        )
                    continue

                # 5. Direct availability check.
                if effective in available:
                    surviving_series.append(series)
                    continue

                # 6. Virtual series alias (e.g. "rainTotal" → "rain").
                underlying = _VIRTUAL_SERIES_MAP.get(series.series_id)
                if underlying is not None and underlying in available:
                    surviving_series.append(series)
                    continue

                # Not available — prune.
                logger.debug(
                    "Pruned series %s from chart %s"
                    " (observation type %r not available)",
                    series.series_id,
                    chart.chart_id,
                    effective,
                )

            if not surviving_series:
                logger.debug(
                    "Pruned chart %s from group %s (no available series)",
                    chart.chart_id,
                    group.group_id,
                )
                continue

            # Build a new ChartConfig with only the surviving series.
            pruned_charts.append(
                ChartConfig(
                    chart_id=chart.chart_id,
                    title=chart.title,
                    type=chart.type,
                    connect_nulls=chart.connect_nulls,
                    y_axis_min=chart.y_axis_min,
                    aggregate_type=chart.aggregate_type,
                    aggregate_interval=chart.aggregate_interval,
                    x_axis_groupby=chart.x_axis_groupby,
                    x_axis_categories=list(chart.x_axis_categories),
                    force_full_year=chart.force_full_year,
                    series=surviving_series,
                )
            )

        if not pruned_charts:
            logger.debug(
                "Pruned group %s (no available charts)",
                group.group_id,
            )
            continue

        # Build a new ChartGroupConfig with only the surviving charts.
        pruned_groups.append(
            ChartGroupConfig(
                group_id=group.group_id,
                title=group.title,
                show_button=group.show_button,
                button_text=group.button_text,
                type=group.type,
                enable_date_ranges=group.enable_date_ranges,
                rolling_ranges=list(group.rolling_ranges),
                available_years=list(group.available_years),
                enable_monthly_breakdown=group.enable_monthly_breakdown,
                time_length=group.time_length,
                timespan_start=group.timespan_start,
                timespan_stop=group.timespan_stop,
                tooltip_date_format=group.tooltip_date_format,
                gapsize=group.gapsize,
                aggregate_interval=group.aggregate_interval,
                aggregate_type=group.aggregate_type,
                force_full_year=group.force_full_year,
                start_at_beginning_of_month=group.start_at_beginning_of_month,
                page_content=group.page_content,
                charts=pruned_charts,
            )
        )

    return ChartsConfig(
        aggregate_type=config.aggregate_type,
        time_length=config.time_length,
        type=config.type,
        colors=list(config.colors),
        tooltip_date_format=config.tooltip_date_format,
        groups=pruned_groups,
    )


# ---------------------------------------------------------------------------
# Global singleton (wire at startup, access from endpoints)
# ---------------------------------------------------------------------------

_charts_config: ChartsConfig | None = None


def wire_charts_config(config: ChartsConfig) -> None:
    """Store the loaded (and pruned) charts config as a global singleton.

    Called once from __main__.py after load + prune.
    """
    global _charts_config  # noqa: PLW0603
    _charts_config = config


def get_charts_config() -> ChartsConfig:
    """Return the loaded charts config.

    Raises:
        RuntimeError: If called before wire_charts_config().
    """
    if _charts_config is None:
        raise RuntimeError("Charts config not loaded — call wire_charts_config() first")
    return _charts_config
