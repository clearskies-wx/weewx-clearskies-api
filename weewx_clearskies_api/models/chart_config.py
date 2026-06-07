"""Chart configuration dataclasses for operator-configurable charts.

These dataclasses model the 3-level nesting of Belchertown's ``graphs.conf``
file (groups > charts > series) and are populated by the graphs.conf parser
(T1.2).  They carry parsed, typed values — not raw INI strings.

Hierarchy:

    ChartsConfig               — top-level container with global defaults
      └─ ChartGroupConfig      — one tab / page of charts (INI [group] section)
           └─ ChartConfig      — one chart panel within a group ([[chart]])
                └─ SeriesConfig — one data series within a chart ([[[series]]])

Every field name is snake_case (Python convention).  camelCase JSON
serialisation is handled by separate Pydantic response models added in T1.4.

Field names map 1-to-1 to graphs.conf keys unless a docstring note says
otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# SeriesConfig — one data series within a chart
# ---------------------------------------------------------------------------


@dataclass
class SeriesConfig:
    """One data series within a chart (graphs.conf ``[[[series]]]`` section).

    ``series_id`` is the INI section name (e.g. ``"outTemp"``,
    ``"windRose"``, ``"custom_average_rains"``).

    ``observation_type`` lets the operator alias a series to a different DB
    column — e.g. ``[[[outTemp_min]]]`` with ``observation_type = outTemp``
    means "use outTemp data but display as a separate series."
    """

    # Required identity field
    series_id: str

    # DB / display overrides
    observation_type: str | None = None
    name: str | None = None
    color: str | None = None

    # Chart-type override for this series (e.g. "area", "column")
    type: str | None = None  # noqa: A003  — mirrors graphs.conf key name

    # Axis / stacking / rendering
    z_index: int | None = None
    y_axis: int | None = None          # 0 = primary, 1 = secondary
    y_axis_min: float | None = None
    y_axis_max: float | None = None
    y_axis_label: str | None = None
    y_axis_tick_interval: float | None = None
    line_width: int | None = None
    connect_nulls: bool | None = None
    visible: bool | None = None
    opacity: float | None = None
    stacking: str | None = None        # e.g. "normal"

    # Aggregation
    aggregate_type: str | None = None  # e.g. "avg", "sum", "max", "min"
    average_type: str | None = None    # climatological: "max" or "min"
                                          # (avg of daily highs/lows)

    # Marker options (from [[[[marker]]]] sub-section)
    marker_enabled: bool | None = None
    marker_radius: int | None = None

    # Wind rose (beaufort speed categories 0–6)
    # Maps speed-category index to hex color, e.g. {0: "#1278c8", 1: "#1fafdd"}
    # Populated from graphs.conf keys beauford0 … beauford6
    beaufort_colors: dict[int, str] = field(default_factory=dict)

    # Weather range / radial chart
    range_type: str | None = None      # e.g. "outTemp"
    area_display: int | None = None    # 0 or 1

    # Custom SQL series
    use_custom_sql: bool = False
    custom_sql_query: str | None = None
    x_column: str | None = None
    y_column: str | None = None

    # Y-axis tick formatting: number of decimal places for axis tick labels
    y_axis_tick_decimals: int | None = None

    # Soft axis limits (allow data to exceed)
    y_axis_soft_min: float | None = None
    y_axis_soft_max: float | None = None
    y_axis_minor_ticks: bool | None = None

    # Line/area styling
    dash_style: str | None = None       # Highcharts: Solid, Dash, Dot, DashDot, LongDash, ShortDash
    fill_color: str | None = None       # Separate fill color for area charts
    fill_opacity: float | None = None
    border_width: int | None = None     # Column/bar border width
    mirrored_value: bool | None = None  # Show absolute values on bidirectional axis

    # Hover/interaction states (from [[[[states]]]] sub-section)
    states: dict | None = None

    # Number formatting (from [[[[numberFormat]]]] sub-section)
    number_format: dict | None = None

    # Polar chart options
    polar: bool | None = None
    connect_ends: bool | None = None

    # Gauge chart options (from color1–color7, color1_position, etc.)
    colors_enabled: bool = False
    color_zones: list[dict] | None = None


# ---------------------------------------------------------------------------
# ChartConfig — one chart within a group
# ---------------------------------------------------------------------------


@dataclass
class ChartConfig:
    """One chart panel within a group (graphs.conf ``[[chart]]`` section).

    ``chart_id`` is the INI section name (e.g. ``"chart1"``, ``"roseplt"``,
    ``"radialChartName"``).
    """

    # Required identity field
    chart_id: str

    # Display
    title: str | None = None
    type: str | None = None            # line, spline, area, column, bar, scatter  # noqa: A003

    # Data options
    connect_nulls: bool | None = None
    y_axis_min: float | None = None
    aggregate_type: str | None = None
    aggregate_interval: int | None = None   # seconds

    # X-axis options (climatological / multi-month charts)
    x_axis_groupby: str | None = None       # e.g. "month"
    x_axis_categories: list[str] = field(default_factory=list)  # ['Jan', 'Feb', ...]
    force_full_year: bool | None = None

    # Display extras
    subtitle: str | None = None
    polar: bool | None = None

    # Child series (populated by parser)
    series: list[SeriesConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ChartGroupConfig — one tab / page of charts
# ---------------------------------------------------------------------------


@dataclass
class ChartGroupConfig:
    """One tab / page of charts (graphs.conf ``[group]`` section).

    ``group_id`` is the INI section name (e.g. ``"homepage"``, ``"monthly"``,
    ``"ANNUAL"``).
    """

    # Required identity field
    group_id: str

    # Navigation / display
    title: str | None = None
    show_button: bool = True
    button_text: str | None = None
    type: str | None = None            # default chart type for the group  # noqa: A003

    # Date range features
    enable_date_ranges: bool = False
    rolling_ranges: list[str] = field(default_factory=list)   # ["1d", "3d", "7d", ...]
    available_years: list[int] = field(default_factory=list)  # [2025, 2024, ...]
    enable_monthly_breakdown: bool = False

    # Time settings
    # Can be an integer (seconds) or a string keyword:
    # "month", "year", "all", "timespan_specific"
    time_length: int | str | None = None
    timespan_start: int | None = None          # epoch seconds
    timespan_stop: int | None = None           # epoch seconds
    tooltip_date_format: str | None = None
    gapsize: int | None = None                 # seconds
    aggregate_interval: int | None = None      # seconds
    aggregate_type: str | None = None

    # Display options
    force_full_year: bool = False
    start_at_beginning_of_month: bool = False
    page_content: str | None = None            # HTML/Markdown above chart group
    # Belchertown report generation cadence ("daily" / None); no-op in Clear Skies.
    generate: str | None = None

    # Chart-level display controls
    legend: bool = True
    exporting: bool = True
    credits: str | None = None
    credits_url: str | None = None
    credits_position: dict | None = None
    css_class: str | None = None
    css_height: str | None = None
    css_width: str | None = None

    # Child charts (populated by parser)
    charts: list[ChartConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ChartsConfig — top-level container with global defaults
# ---------------------------------------------------------------------------


@dataclass
class ChartsConfig:
    """Top-level charts configuration container (the whole graphs.conf file).

    Holds global defaults that apply when a group / chart / series does not
    specify its own value, plus the ordered list of chart groups.

    ``colors`` is stored as a list of strings.  The parser splits the
    comma-separated string from graphs.conf (e.g.
    ``"#7cb5ec, #b2df8a, #f7a35c, ..."``) into individual hex strings.
    Similarly ``time_length`` may be an integer (seconds) or a keyword string.
    """

    # Global defaults
    aggregate_type: str | None = None

    # Default: 90000 seconds (~25 hours).  May also be a keyword string.
    time_length: int | str | None = None

    type: str = "line"  # noqa: A003  — mirrors graphs.conf key name

    # Parsed from comma-separated string in graphs.conf
    colors: list[str] = field(default_factory=list)

    tooltip_date_format: str | None = None

    # Ordered chart groups (populated by parser)
    groups: list[ChartGroupConfig] = field(default_factory=list)
