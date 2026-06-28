"""Capabilities endpoint (3a-2, extended 3b-1).

GET /capabilities — runtime capability registry per ADR-038.

Data sources:
  - weewxColumns: registry.stock (auto-mapped stock columns present in archive).
  - canonicalFieldsAvailable: union of canonical names from:
      - registry.stock (weewx archive columns)
      - provider-supplied canonical fields (populated this round via wire_providers)
  - providers: list of CapabilityDeclaration per configured provider (3b-1).

No DB hit at request time — all registries are in-memory from startup.
No query params.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.models.responses import (
    CapabilityDeclaration,
    CapabilityRegistry,
    CapabilityResponse,
    WeewxColumnEntry,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/capabilities", summary="Runtime capability registry", tags=["Capabilities"])
def get_capabilities() -> CapabilityResponse:
    """Return the runtime capability registry (no DB hit, no query params).

    providers: populated from the provider registry (wire_providers() at startup).
    canonicalFieldsAvailable: union of stock weewx columns AND provider-supplied
      canonical fields per CapabilityRegistry schema.
    Operator-mapped custom columns (registry.unmapped) are not in weewxColumns
    or canonicalFieldsAvailable — that surface opens when the operator-mapping
    UI ships in Phase 4 (ADR-027 + ADR-035).
    """
    registry = get_registry()
    provider_registry = get_provider_registry()

    weewx_columns = [
        WeewxColumnEntry(
            canonicalField=info.canonical_name,
            archiveColumn=info.db_name,
        )
        for info in registry.stock.values()
    ]

    # canonicalFieldsAvailable = union of weewx-archive canonical fields
    # + provider-supplied canonical fields (ADR-038 §4, CapabilityRegistry schema).
    stock_fields = {col.canonicalField for col in weewx_columns}
    provider_fields: set[str] = set()
    for cap in provider_registry:
        provider_fields.update(cap.supplied_canonical_fields)

    canonical_fields_available = sorted(stock_fields | provider_fields)

    providers = [
        CapabilityDeclaration(
            providerId=cap.provider_id,
            domain=cap.domain,
            suppliedCanonicalFields=list(cap.supplied_canonical_fields),
            geographicCoverage=cap.geographic_coverage,
            defaultPollIntervalSeconds=cap.default_poll_interval_seconds,
            operatorNotes=cap.operator_notes,
            tileUrlTemplate=cap.tile_url_template,
            wmsEndpointUrl=cap.wms_endpoint_url,
            wmsLayerName=cap.wms_layer_name,
            tileContentType=cap.tile_content_type,
            iframeUrl=cap.iframe_url,
            caddyPrefix=cap.caddy_prefix,
            alertUrl=cap.alert_url,
            bounds=cap.bounds,
            refreshInterval=cap.refresh_interval,
            nowcastAvailable=cap.nowcast_available,
            alertsAvailable=cap.alerts_available,
            satelliteAvailable=cap.satellite_available,
            satelliteTileUrlTemplate=cap.satellite_tile_url_template,
        )
        for cap in provider_registry
    ]

    return CapabilityResponse(
        data=CapabilityRegistry(
            providers=providers,
            weewxColumns=weewx_columns,
            canonicalFieldsAvailable=canonical_fields_available,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
        stationClock=build_station_clock(),
        freshness=build_freshness("station_metadata"),
    )
