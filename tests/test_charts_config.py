"""Unit tests for charts_config parser and pruning logic (T1.6).

Covers:
  - Parser: valid config, missing file, empty file, malformed values,
    beaufort colors, custom SQL series, timespan-specific groups.
  - Pruning: series removal, chart removal, group removal,
    custom SQL survival, wind-rose column requirements.

ADR references: ADR-027 (config search order), ADR-024 (self-hide rule).
"""

from __future__ import annotations

from pathlib import Path

from weewx_clearskies_api.db.reflection import ColumnInfo, ColumnRegistry

# ---------------------------------------------------------------------------
# Registry helper (mirrors pattern from test_charts_unit.py)
# ---------------------------------------------------------------------------


def _make_registry_with_mapped(canonical_names: list[str]) -> ColumnRegistry:
    """Build a ColumnRegistry where the stock set matches canonical_names."""
    registry = ColumnRegistry()
    registry.stock = {
        name: ColumnInfo(db_name=name, canonical_name=name, is_stock=True)
        for name in canonical_names
    }
    return registry


# ---------------------------------------------------------------------------
# Minimal INI content helpers
# ---------------------------------------------------------------------------

_MINIMAL_INI = """\
type = line
colors = "#aaa, #bbb"

[homepage]
    title = "Home"
    show_button = true
    enable_date_ranges = true
    rolling_ranges = 1d, 7d, 30d
    time_length = 86400
    [[chart1]]
        title = Temperature
        [[[outTemp]]]
            name = Temperature
            color = red
            zIndex = 1
        [[[dewpoint]]]
            color = purple
    [[roseplt]]
        title = Wind Rose
        [[[windRose]]]
            beauford0 = "#1278c8"
            beauford1 = "#1fafdd"
"""


# ---------------------------------------------------------------------------
# Test class: parser
# ---------------------------------------------------------------------------


class TestChartsConfigParser:
    """Parser builds correct ChartsConfig dataclass tree from INI content."""

    def test_parse_valid_config_builds_correct_structure(
        self, tmp_path: Path
    ) -> None:
        """Valid INI config produces ChartsConfig with expected groups, charts, series.

        Invariant: homepage group is parsed with 2 charts; chart1 has 2 series
        (outTemp, dewpoint); type is 'line'; colors list contains 2 entries.
        """
        from weewx_clearskies_api.services.charts_config import load_charts_config

        conf_file = tmp_path / "charts.conf"
        conf_file.write_text(_MINIMAL_INI, encoding="utf-8")

        config = load_charts_config(conf_file)

        assert config.type == "line", (
            f"Global type must be 'line', got {config.type!r}"
        )
        assert len(config.colors) == 2, (
            f"Expected 2 colors from '#aaa, #bbb', got {config.colors!r}"
        )
        assert len(config.groups) == 1, (
            f"Expected 1 group (homepage), got {len(config.groups)}"
        )

        homepage = config.groups[0]
        assert homepage.group_id == "homepage"
        assert homepage.title == "Home"
        assert homepage.enable_date_ranges is True
        assert homepage.rolling_ranges == ["1d", "7d", "30d"]
        assert homepage.time_length == 86400

        assert len(homepage.charts) == 2, (
            f"homepage must have 2 charts, got {len(homepage.charts)}"
        )

        chart1 = homepage.charts[0]
        assert chart1.chart_id == "chart1"
        assert chart1.title == "Temperature"
        assert len(chart1.series) == 2, (
            f"chart1 must have 2 series, got {len(chart1.series)}"
        )

        series_ids = {s.series_id for s in chart1.series}
        assert "outTemp" in series_ids
        assert "dewpoint" in series_ids

        out_temp = next(s for s in chart1.series if s.series_id == "outTemp")
        assert out_temp.name == "Temperature"
        assert out_temp.color == "red"
        assert out_temp.z_index == 1

    def test_parse_missing_file_returns_builtin_defaults(self) -> None:
        """Nonexistent config path falls back to built-in defaults without error.

        Invariant: returned ChartsConfig is not None, has groups, type is 'line'.
        """
        from weewx_clearskies_api.services.charts_config import load_charts_config

        config = load_charts_config(Path("/nonexistent/path/charts.conf"))

        assert config is not None, "load_charts_config must not return None"
        assert config.type == "line", (
            f"Built-in default type must be 'line', got {config.type!r}"
        )
        # Built-in default has at least one group
        assert len(config.groups) >= 1, (
            "Built-in default must have at least one chart group"
        )

    def test_parse_empty_file_returns_empty_config(
        self, tmp_path: Path
    ) -> None:
        """An empty config file produces 0 groups with default global values.

        Invariant: groups list is empty; type defaults to 'line'.
        """
        from weewx_clearskies_api.services.charts_config import load_charts_config

        conf_file = tmp_path / "charts.conf"
        conf_file.write_text("", encoding="utf-8")

        config = load_charts_config(conf_file)

        assert len(config.groups) == 0, (
            f"Empty file must produce 0 groups, got {len(config.groups)}"
        )
        assert config.type == "line", (
            f"Empty file must default type to 'line', got {config.type!r}"
        )

    def test_parse_malformed_values_skip_gracefully(
        self, tmp_path: Path
    ) -> None:
        """Non-integer time_length and zIndex are coerced gracefully without crash.

        Invariant: group is parsed, series is parsed, z_index is None (bad int),
        time_length is the raw string 'not_a_number' (keyword fallback).
        """
        from weewx_clearskies_api.services.charts_config import load_charts_config

        ini = """\
[homepage]
    time_length = not_a_number
    show_button = true
    [[chart1]]
        [[[outTemp]]]
            zIndex = bad_int
"""
        conf_file = tmp_path / "charts.conf"
        conf_file.write_text(ini, encoding="utf-8")

        config = load_charts_config(conf_file)

        assert len(config.groups) == 1
        group = config.groups[0]
        assert group.group_id == "homepage"
        # time_length: _to_int_or_str returns string when int parse fails
        assert group.time_length == "not_a_number", (
            f"Non-integer time_length must be returned as string, "
            f"got {group.time_length!r}"
        )

        assert len(group.charts) == 1
        chart = group.charts[0]
        assert len(chart.series) == 1
        series = chart.series[0]
        assert series.series_id == "outTemp"
        # zIndex: _to_int_or_none returns None for non-integer string
        assert series.z_index is None, (
            f"Non-integer zIndex must parse to None, got {series.z_index!r}"
        )

    def test_parse_beaufort_colors_populated_from_ini_keys(
        self, tmp_path: Path
    ) -> None:
        """beauford0..beauford6 INI keys populate beaufort_colors dict with 7 entries.

        Invariant: all 7 speed-category colors are captured with correct indices.
        """
        from weewx_clearskies_api.services.charts_config import load_charts_config

        colors = {
            0: "#1278c8",
            1: "#1fafdd",
            2: "#00ffcc",
            3: "#00aa55",
            4: "#ffaa00",
            5: "#ff5500",
            6: "#ff0000",
        }
        color_lines = "\n".join(
            f"            beauford{i} = \"{c}\"" for i, c in colors.items()
        )
        ini = f"""\
[homepage]
    [[roseplt]]
        [[[windRose]]]
{color_lines}
"""
        conf_file = tmp_path / "charts.conf"
        conf_file.write_text(ini, encoding="utf-8")

        config = load_charts_config(conf_file)
        series = config.groups[0].charts[0].series[0]

        assert series.series_id == "windRose"
        assert len(series.beaufort_colors) == 7, (
            f"Expected 7 beaufort colors, got {len(series.beaufort_colors)}"
        )
        for i, expected_color in colors.items():
            assert series.beaufort_colors[i] == expected_color, (
                f"beaufort_colors[{i}] must be {expected_color!r}, "
                f"got {series.beaufort_colors.get(i)!r}"
            )

    def test_parse_custom_sql_series_captured(self, tmp_path: Path) -> None:
        """use_custom_sql=true is parsed as bool True; custom_sql_query is captured.

        Invariant: use_custom_sql is True, custom_sql_query is non-None (any type
        is acceptable — ConfigObj may return a list when the SQL contains commas).
        x_column and y_column are captured as strings.

        Note on ConfigObj behaviour: a value like
          ``custom_sql_query = SELECT month avg_temp FROM climatology``
        is parsed as a plain string, but a value containing commas is parsed as a
        ConfigObj list (e.g. ``['SELECT month', 'avg_temp FROM ...']``).  This is a
        known ConfigObj quirk; the test uses a comma-free query to exercise the
        plain-string path without tripping on the list representation.
        """
        from weewx_clearskies_api.services.charts_config import load_charts_config

        # Use a comma-free SQL so ConfigObj returns a plain string (not a list).
        ini = """\
[homepage]
    [[mychart]]
        [[[custom_avg_temp]]]
            use_custom_sql = true
            custom_sql_query = SELECT month FROM climatology ORDER BY month
            x_column = month
            y_column = avg_temp
"""
        conf_file = tmp_path / "charts.conf"
        conf_file.write_text(ini, encoding="utf-8")

        config = load_charts_config(conf_file)
        series = config.groups[0].charts[0].series[0]

        assert series.series_id == "custom_avg_temp"
        assert series.use_custom_sql is True, (
            "use_custom_sql must be True when set to 'true' in INI"
        )
        assert series.custom_sql_query is not None, (
            "custom_sql_query must be captured from INI"
        )
        assert "climatology" in series.custom_sql_query, (
            f"custom_sql_query content mismatch: {series.custom_sql_query!r}"
        )
        assert series.x_column == "month"
        assert series.y_column == "avg_temp"

    def test_parse_timespan_specific_fields_captured(
        self, tmp_path: Path
    ) -> None:
        """time_length=timespan_specific + timespan_start/stop are all parsed.

        Invariant: all three fields have expected values after parsing.
        """
        from weewx_clearskies_api.services.charts_config import load_charts_config

        ini = """\
[tropical]
    time_length = timespan_specific
    timespan_start = 1692428400
    timespan_stop = 1692687599
    [[chart1]]
        [[[outTemp]]]
"""
        conf_file = tmp_path / "charts.conf"
        conf_file.write_text(ini, encoding="utf-8")

        config = load_charts_config(conf_file)
        group = config.groups[0]

        assert group.group_id == "tropical"
        assert group.time_length == "timespan_specific", (
            f"time_length must be 'timespan_specific', got {group.time_length!r}"
        )
        assert group.timespan_start == 1692428400, (
            f"timespan_start must be 1692428400, got {group.timespan_start!r}"
        )
        assert group.timespan_stop == 1692687599, (
            f"timespan_stop must be 1692687599, got {group.timespan_stop!r}"
        )


# ---------------------------------------------------------------------------
# Test class: pruning
# ---------------------------------------------------------------------------


class TestChartsConfigPruning:
    """prune_charts_config drops unavailable series, charts, and groups."""

    def _make_config_with_series(
        self, series_ids: list[str]
    ):  # -> ChartsConfig
        """Build a minimal ChartsConfig with one group, one chart, N series."""
        from weewx_clearskies_api.models.chart_config import (
            ChartConfig,
            ChartGroupConfig,
            ChartsConfig,
            SeriesConfig,
        )

        series = [SeriesConfig(series_id=sid) for sid in series_ids]
        chart = ChartConfig(chart_id="chart1", series=series)
        group = ChartGroupConfig(group_id="homepage", charts=[chart])
        return ChartsConfig(groups=[group])

    def test_prune_removes_series_absent_from_registry(self) -> None:
        """Series not in registry are removed; series in registry survive.

        Invariant: [outTemp, barometer, windchill] + registry[outTemp] → only outTemp.
        """
        from weewx_clearskies_api.services.charts_config import prune_charts_config

        config = self._make_config_with_series(["outTemp", "barometer", "windchill"])
        registry = _make_registry_with_mapped(["outTemp"])

        pruned = prune_charts_config(config, registry)

        assert len(pruned.groups) == 1
        surviving = {s.series_id for s in pruned.groups[0].charts[0].series}
        assert surviving == {"outTemp"}, (
            f"Only outTemp must survive, got {surviving!r}"
        )

    def test_prune_removes_chart_when_all_series_pruned(self) -> None:
        """A chart whose only series is unavailable is removed entirely.

        Invariant: config with only barometer series + registry without barometer
        → chart is dropped → group has 0 charts → group is dropped.
        """
        from weewx_clearskies_api.services.charts_config import prune_charts_config

        config = self._make_config_with_series(["barometer"])
        registry = _make_registry_with_mapped([])  # empty registry

        pruned = prune_charts_config(config, registry)

        # The chart has no surviving series → chart is dropped
        # The group has no surviving charts → group is dropped
        assert len(pruned.groups) == 0, (
            f"Group with all charts pruned must be removed, "
            f"got {len(pruned.groups)} groups"
        )

    def test_prune_removes_group_when_all_charts_pruned(self) -> None:
        """A group whose every chart has all series pruned is removed.

        Invariant: single group with single chart with single unavailable series
        → 0 groups after prune.
        """
        from weewx_clearskies_api.services.charts_config import prune_charts_config

        config = self._make_config_with_series(["windGust"])
        registry = _make_registry_with_mapped(["outTemp"])  # windGust not present

        pruned = prune_charts_config(config, registry)

        assert len(pruned.groups) == 0, (
            f"Group with all-pruned charts must be removed; "
            f"got {len(pruned.groups)} groups"
        )

    def test_prune_keeps_custom_sql_series_regardless_of_registry(self) -> None:
        """Custom SQL series survive pruning even when registry is empty.

        Invariant: use_custom_sql=True means no column availability check.
        """
        from weewx_clearskies_api.models.chart_config import (
            ChartConfig,
            ChartGroupConfig,
            ChartsConfig,
            SeriesConfig,
        )
        from weewx_clearskies_api.services.charts_config import prune_charts_config

        series = SeriesConfig(
            series_id="custom_query",
            use_custom_sql=True,
            custom_sql_query="SELECT 1",
        )
        chart = ChartConfig(chart_id="chart1", series=[series])
        group = ChartGroupConfig(group_id="homepage", charts=[chart])
        config = ChartsConfig(groups=[group])

        registry = _make_registry_with_mapped([])  # empty — nothing available

        pruned = prune_charts_config(config, registry)

        assert len(pruned.groups) == 1, (
            "Group with custom SQL series must survive even with empty registry"
        )
        surviving_ids = {s.series_id for s in pruned.groups[0].charts[0].series}
        assert "custom_query" in surviving_ids, (
            f"Custom SQL series 'custom_query' must survive pruning; "
            f"got {surviving_ids!r}"
        )

    def test_prune_windrose_requires_both_windspeed_and_winddir(self) -> None:
        """windRose series is pruned unless BOTH windSpeed and windDir are in registry.

        Invariant 1: registry with only windSpeed → windRose pruned.
        Invariant 2: registry with both windSpeed + windDir → windRose survives.
        """
        from weewx_clearskies_api.models.chart_config import (
            ChartConfig,
            ChartGroupConfig,
            ChartsConfig,
            SeriesConfig,
        )
        from weewx_clearskies_api.services.charts_config import prune_charts_config

        series = SeriesConfig(series_id="windRose")
        chart = ChartConfig(chart_id="roseplt", series=[series])
        group = ChartGroupConfig(group_id="homepage", charts=[chart])
        config = ChartsConfig(groups=[group])

        # Only windSpeed — missing windDir → should be pruned
        registry_missing_dir = _make_registry_with_mapped(["windSpeed"])
        pruned_missing = prune_charts_config(config, registry_missing_dir)
        assert len(pruned_missing.groups) == 0, (
            "windRose must be pruned when windDir is absent from registry"
        )

        # Both windSpeed and windDir → should survive
        registry_both = _make_registry_with_mapped(["windSpeed", "windDir"])
        pruned_both = prune_charts_config(config, registry_both)
        assert len(pruned_both.groups) == 1, (
            "windRose must survive when both windSpeed and windDir are in registry"
        )
        surviving_ids = {s.series_id for s in pruned_both.groups[0].charts[0].series}
        assert "windRose" in surviving_ids, (
            f"windRose must survive when both columns available; got {surviving_ids!r}"
        )
