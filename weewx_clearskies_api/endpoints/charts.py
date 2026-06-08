"""Charts endpoints.

GET /charts/groups — chart-group structure per ADR-024.
GET /charts/config — full operator chart configuration.

Groups are derived from the operator-configured charts.conf loaded at startup.
Groups whose series are all unavailable in the archive are pruned at startup
and therefore never appear in these responses.
No query params.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter

from weewx_clearskies_api.models.chart_config import (
    ChartConfig,
    ChartGroupConfig,
    ChartsConfig,
    SeriesConfig,
)
from weewx_clearskies_api.models.responses import (
    ChartConfigResponse,
    ChartGroup,
    ChartGroupConfigResponse,
    ChartGroupList,
    ChartGroupResponse,
    ChartsConfigData,
    ChartsConfigResponse,
    SeriesConfigResponse,
    utc_isoformat,
)
from weewx_clearskies_api.services.charts import get_chart_groups
from weewx_clearskies_api.services.charts_config import get_charts_config

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Mapping helpers: dataclass tree → Pydantic response tree
# ---------------------------------------------------------------------------


def _series_to_response(s: SeriesConfig) -> SeriesConfigResponse:
    return SeriesConfigResponse(
        seriesId=s.series_id,
        observationType=s.observation_type,
        name=s.name,
        color=s.color,
        type=s.type,
        zIndex=s.z_index,
        yAxis=s.y_axis,
        yAxisMin=s.y_axis_min,
        yAxisMax=s.y_axis_max,
        yAxisTickDecimals=s.y_axis_tick_decimals,
        yAxisLabel=s.y_axis_label,
        yAxisTickInterval=s.y_axis_tick_interval,
        lineWidth=s.line_width,
        connectNulls=s.connect_nulls,
        visible=s.visible,
        opacity=s.opacity,
        stacking=s.stacking,
        aggregateType=s.aggregate_type,
        averageType=s.average_type,
        markerEnabled=s.marker_enabled,
        markerRadius=s.marker_radius,
        beaufortColors=s.beaufort_colors,
        rangeType=s.range_type,
        areaDisplay=s.area_display,
        useCustomSql=s.use_custom_sql,
        customSqlQuery=s.custom_sql_query,
        xColumn=s.x_column,
        yColumn=s.y_column,
        yAxisSoftMin=s.y_axis_soft_min,
        yAxisSoftMax=s.y_axis_soft_max,
        borderWidth=s.border_width,
        numberFormat=s.number_format,
    )


def _chart_to_response(c: ChartConfig) -> ChartConfigResponse:
    return ChartConfigResponse(
        chartId=c.chart_id,
        title=c.title,
        type=c.type,
        connectNulls=c.connect_nulls,
        yAxisMin=c.y_axis_min,
        aggregateType=c.aggregate_type,
        aggregateInterval=c.aggregate_interval,
        xAxisGroupby=c.x_axis_groupby,
        xAxisCategories=list(c.x_axis_categories),
        forceFullYear=c.force_full_year,
        timeLength=c.time_length,
        series=[_series_to_response(s) for s in c.series],
    )


def _group_to_response(g: ChartGroupConfig) -> ChartGroupConfigResponse:
    return ChartGroupConfigResponse(
        groupId=g.group_id,
        title=g.title,
        showButton=g.show_button,
        buttonText=g.button_text,
        type=g.type,
        enableDateRanges=g.enable_date_ranges,
        rollingRanges=list(g.rolling_ranges),
        availableYears=list(g.available_years),
        enableMonthlyBreakdown=g.enable_monthly_breakdown,
        timeLength=g.time_length,
        timespanStart=g.timespan_start,
        timespanStop=g.timespan_stop,
        tooltipDateFormat=g.tooltip_date_format,
        gapsize=g.gapsize,
        aggregateInterval=g.aggregate_interval,
        aggregateType=g.aggregate_type,
        forceFullYear=g.force_full_year,
        startAtBeginningOfMonth=g.start_at_beginning_of_month,
        pageContent=g.page_content,
        generate=g.generate,
        charts=[_chart_to_response(c) for c in g.charts],
    )


def _config_to_response(config: ChartsConfig) -> ChartsConfigData:
    return ChartsConfigData(
        aggregateType=config.aggregate_type,
        timeLength=config.time_length,
        type=config.type,
        colors=list(config.colors),
        tooltipDateFormat=config.tooltip_date_format,
        groups=[_group_to_response(g) for g in config.groups],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/charts/groups", summary="Chart-group structure", tags=["Charts"])
def get_chart_groups_endpoint() -> ChartGroupResponse:
    """Return operator-configured chart groups from charts.conf.

    Groups and their member series are derived from the charts.conf loaded at
    startup.  Series unavailable in the archive were already pruned at startup;
    groups with no surviving series do not appear in this response.
    """
    groups = get_chart_groups()

    response_groups = [
        ChartGroup(
            id=g.group_id,
            name=g.name,
            builtIn=g.built_in,
            members=g.members,
            defaultRange=g.default_range,
        )
        for g in groups
    ]

    return ChartGroupResponse(
        data=ChartGroupList(groups=response_groups),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get("/charts/config", summary="Full chart configuration", tags=["Charts"])
def get_charts_config_endpoint() -> ChartsConfigResponse:
    """Return the full operator chart configuration tree.

    Includes all groups, charts, and series that survived pruning against the
    ColumnRegistry at startup.  Fields that were not set in charts.conf carry
    their default (None / False / []).
    """
    config = get_charts_config()
    return ChartsConfigResponse(
        data=_config_to_response(config),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
