"""Imagery config endpoint (Phase LM, §LM-1; provider-selection machinery removed
Q10-6, 2026-08-27).

Endpoint:
  GET /imagery/config?lat=&lon=              — the surf height map's basemap config

General-purpose API provider — NOT marine-specific (phase header design
note). Any card/feature may call /imagery/config, not just the marine
heatmap. HARD RULE (phase header): imagery is DISPLAY-ONLY — nothing here
feeds SWAN, the 1D model, transect selection, or any physics path.

Behavior for /config (SURF-MAP-BASEMAP, PA9, Q5; plan
MARINE-AND-MAPS-PLAN-2026-08-27.md §M4, 2026-08-27; the orthophoto
provider selection machinery the 2026-08-26 IMAGERY-MAP round left in
place -- the [imagery] config, its provider modules, admin section, and
wizard selector -- was removed entirely by Q10-6, 2026-08-27, "if we dont
need it then get rid of it"):
  /imagery/config ALWAYS answers with the product basemap
  (provider="basemap"). The endpoint never 404s: the surf height map must
  always get a basemap answer. lat/lon are still validated (422 on
  missing/out-of-range) but are not otherwise used — the basemap answer
  does not vary by coordinates. light.tileUrl/attribution mirror the
  legacy top-level tileUrl/attribution fields (an old client still
  renders). dark.pmtilesUrl is the LOCAL basemap tier
  (`/api/v1/basemap/local/tiles`) — the surf map lives inside the local box
  by construction.

No DB hit — imagery config comes from a fixed constant, not weewx archive.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from weewx_clearskies_api.models.params import ImageryConfigQueryParams
from weewx_clearskies_api.models.responses import (
    ImageryConfigResponse,
    ImageryDarkSource,
    ImageryLightSource,
)

router = APIRouter()

# SURF-MAP-BASEMAP (PA9, §M4, 2026-08-27): fixed light-theme tile source for
# /imagery/config's "basemap" answer. {s} pre-expanded to "a" per lead
# mechanics — the surf map's mosaic fetches a single subdomain, not a
# rotating {s} substitution.
_BASEMAP_LIGHT_TILE_URL = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
_OSM_ATTRIBUTION = "© OpenStreetMap contributors"


# ---------------------------------------------------------------------------
# Query-param validators (Depends wrappers — extra="forbid" enforcement per
# coding.md "Pydantic extra=forbid requires the right FastAPI wiring")
# ---------------------------------------------------------------------------


def _get_imagery_config_params(request: Request) -> ImageryConfigQueryParams:
    try:
        return ImageryConfigQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        from fastapi.exceptions import RequestValidationError

        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/imagery/config",
    summary="Active imagery provider's tile source info",
    tags=["Imagery"],
    response_model=ImageryConfigResponse,
)
def get_imagery_config(
    params: Annotated[ImageryConfigQueryParams, Depends(_get_imagery_config_params)],
) -> ImageryConfigResponse:
    """Return the product basemap config for the surf height map (§M4, PA9).

    Always answers with provider="basemap" — never 404s. lat/lon are still
    validated (422 on missing/out-of-range) via the Depends params model but
    are not otherwise used — the basemap answer does not vary by
    coordinates.
    """
    del params  # validated for shape only; the basemap answer is fixed

    return ImageryConfigResponse(
        provider="basemap",
        tileUrl=_BASEMAP_LIGHT_TILE_URL,
        attribution=_OSM_ATTRIBUTION,
        proxyMode="direct",
        bounds=None,
        light=ImageryLightSource(
            tileUrl=_BASEMAP_LIGHT_TILE_URL,
            attribution=_OSM_ATTRIBUTION,
        ),
        dark=ImageryDarkSource(
            pmtilesUrl="/api/v1/basemap/local/tiles",
            maxDataZoom=15,
            attribution="© OpenStreetMap contributors © Protomaps",
        ),
        zoomMin=0,
        zoomMax=19,
    )
