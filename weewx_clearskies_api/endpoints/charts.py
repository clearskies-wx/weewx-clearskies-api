"""Charts endpoint (3a-2).

GET /charts/groups — chart-group structure per ADR-024.

Returns built-in groups with members pruned against the ColumnRegistry.
Groups with zero members after pruning are omitted (self-hide).
No query params.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.models.responses import (
    ChartGroup,
    ChartGroupList,
    ChartGroupResponse,
    utc_isoformat,
)
from weewx_clearskies_api.services.charts import get_chart_groups

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/charts/groups", summary="Chart-group structure", tags=["Charts"])
def get_chart_groups_endpoint() -> ChartGroupResponse:
    """Return built-in chart groups with members pruned against the ColumnRegistry.

    Custom chart groups are empty in 3a-2 (Phase 4 config UI).
    Groups with zero members after pruning self-hide (parallel to /records).
    """
    registry = get_registry()
    groups = get_chart_groups(registry)

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
