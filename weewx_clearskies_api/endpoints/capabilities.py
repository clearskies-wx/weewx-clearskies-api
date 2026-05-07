"""Capabilities endpoint (3a-2).

GET /capabilities — runtime capability registry per ADR-038.

Data sources:
  - weewxColumns: registry.stock (auto-mapped stock columns present in archive).
  - canonicalFieldsAvailable: union of canonical names from registry.stock.
  - providers: empty [] until 3b populates per-provider modules.

No DB hit at request time — registry is in-memory from startup reflection.
No query params.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.models.responses import (
    CapabilityRegistry,
    CapabilityResponse,
    WeewxColumnEntry,
    utc_isoformat,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/capabilities", summary="Runtime capability registry", tags=["Capabilities"])
def get_capabilities() -> CapabilityResponse:
    """Return the runtime capability registry (no DB hit, no query params).

    providers is [] until 3b wires per-provider modules per ADR-038.
    Operator-mapped custom columns (registry.unmapped) are not in weewxColumns
    or canonicalFieldsAvailable — that surface opens when the operator-mapping
    UI ships in Phase 4 (ADR-027 + ADR-035).
    """
    registry = get_registry()

    weewx_columns = [
        WeewxColumnEntry(
            canonicalField=info.canonical_name,
            archiveColumn=info.db_name,
        )
        for info in registry.stock.values()
    ]

    canonical_fields_available = [col.canonicalField for col in weewx_columns]

    return CapabilityResponse(
        data=CapabilityRegistry(
            providers=[],
            weewxColumns=weewx_columns,
            canonicalFieldsAvailable=canonical_fields_available,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
