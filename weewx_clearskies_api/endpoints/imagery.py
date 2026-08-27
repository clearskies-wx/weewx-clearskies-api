"""Imagery endpoints: provider config + NAIP tile proxy (Phase LM, §LM-1).

Endpoints:
  GET /imagery/config?lat=&lon=              — active provider's tile source info
  GET /imagery/tiles/{z}/{x}/{y}             — NAIP tile proxy only

General-purpose API provider — NOT marine-specific (phase header design
note). Any card/feature may call /imagery/config, not just the marine
heatmap. HARD RULE (phase header): imagery is DISPLAY-ONLY — nothing here
feeds SWAN, the 1D model, transect selection, or any physics path.

Behavior for /config (SURF-MAP-BASEMAP, PA9, Q5; plan
MARINE-AND-MAPS-PLAN-2026-08-27.md §M4, 2026-08-27 — supersedes the
naip/esri/map decision tree below the 2026-08-26 IMAGERY-MAP round left in
place):
  /imagery/config now ALWAYS answers with the product basemap
  (provider="basemap") regardless of [imagery] provider — naip/esri/map/auto
  are no longer reachable from any user-facing surface (the operator: "get
  rid of the orthophotography for the surf height map and replace it with a
  regular basemap"). The endpoint never 404s: the surf height map must
  always get a basemap answer. lat/lon are still validated (422 on
  missing/out-of-range) but no longer used to select a provider —
  _select_provider() is not called here or anywhere else in this module
  (kept, unmodified, per lead mechanics; /imagery/tiles inlines its own
  provider check rather than calling it). light.tileUrl/attribution mirror
  the legacy top-level tileUrl/attribution fields (an old client still
  renders). dark.pmtilesUrl is the LOCAL basemap tier
  (`/api/v1/basemap/local/tiles`) — the surf map lives inside the local box
  by construction. wire_imagery_settings() logs ONE startup WARNING naming
  the ignored [imagery] provider value, if set. The [imagery] provider
  config, its modules, admin section, and wizard selector all stay pending
  Q10-6 — only this endpoint's answer changed.

Behavior decision tree for /tiles/{z}/{x}/{y} (§LM-1c):
  1. z/x/y out of range (z via FastAPI Path constraint; x/y validated
     against 2**z here) → 400 Problem. Tile-proxy amplification-surface
     guard (lead ruling 2026-08-03) — do not forward arbitrary z/x/y bbox
     math upstream unchecked.
  2. [imagery] not configured, or provider explicitly pinned to "esri" or
     "map" (neither ever flows through this endpoint per §LM-1c — both are
     browser-direct, config-only providers) → 404 Problem.
  3. Cache hit (inside naip.get_tile()) → return cached bytes, no upstream
     call.
  4. Cache miss → naip.get_tile() → upstream exportImage call → cache →
     200 binary response.
  5. Upstream QuotaExhausted (429) → 503 + Retry-After.
  6. Upstream ProviderProtocolError with status_code==404 (out-of-domain
     bbox) → 404. Other 4xx → 502.
  7. Upstream TransientNetworkError (network/5xx after retries) → 502.

wire_imagery_settings():
  Mirrors wire_seeing_settings() in endpoints/seeing.py — the simplest
  existing precedent for a keyless, no-credential settings wiring. Called
  from __main__.py at startup, after wire_radar_settings.

No DB hit — imagery config/tiles come from the provider, not weewx archive.

Binary response note (/tiles):
  Returns fastapi.Response(content=bytes, media_type=ct) — no Pydantic
  response model, same non-JSON shape as /radar/.../tiles
  (endpoints/radar.py get_radar_tile).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import ValidationError

from weewx_clearskies_api.models.params import ImageryConfigQueryParams, ImageryTileQueryParams
from weewx_clearskies_api.models.responses import (
    ImageryConfigResponse,
    ImageryDarkSource,
    ImageryLightSource,
)
from weewx_clearskies_api.providers._common.dispatch import get_provider_module
from weewx_clearskies_api.providers._common.errors import (
    KeyInvalid,
    ProviderProtocolError,
    QuotaExhausted,
    TransientNetworkError,
)
from weewx_clearskies_api.providers.imagery import naip as naip_module

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level settings wiring (populated at startup by wire_imagery_settings).
# wire_imagery_settings() is called from __main__.py after settings load.
# ---------------------------------------------------------------------------

_imagery_provider: str | None = None  # "auto" | "naip" | "esri" | None (not configured)
_imagery_tile_cache_ttl_seconds: int = 604800  # 7 days

# SURF-MAP-BASEMAP (PA9, §M4, 2026-08-27): fixed light-theme tile source for
# /imagery/config's "basemap" answer. {s} pre-expanded to "a" per lead
# mechanics — the surf map's mosaic fetches a single subdomain, not a
# rotating {s} substitution.
_BASEMAP_LIGHT_TILE_URL = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
_OSM_ATTRIBUTION = "© OpenStreetMap contributors"

# NAIP-sensible zoom bounds (see providers/imagery/naip.py docstring): NAIP's
# native resolution (~0.6-1m/px) is fully represented by z<=20 (~0.15m/px at
# the equator); wider zoom would just upsample, widening the amplification
# surface for no benefit.
_MIN_ZOOM = 0
_MAX_ZOOM = 20


def wire_imagery_settings(settings: object) -> None:
    """Store imagery settings for use by the endpoint.

    Extracts provider + tile_cache_ttl_seconds from settings.imagery.
    Called from __main__.py after settings load. Tests that don't care about
    provider config leave module defaults as-is (matches wire_seeing_settings
    pattern in endpoints/seeing.py — the simplest existing precedent for a
    keyless, no-credential settings wiring).

    SURF-MAP-BASEMAP (PA9, §M4, 2026-08-27): [imagery] provider is no longer
    used by /imagery/config (always answers "basemap") or by any other
    user-facing surface. If provider is set, log ONE WARNING here at wiring
    time naming the ignored value — not per request.
    """
    global _imagery_provider, _imagery_tile_cache_ttl_seconds  # noqa: PLW0603
    imagery_section = getattr(settings, "imagery", None)
    if imagery_section is not None:
        _imagery_provider = getattr(imagery_section, "provider", None)
        _imagery_tile_cache_ttl_seconds = int(
            getattr(imagery_section, "tile_cache_ttl_seconds", 604800)
        )
        if _imagery_provider is not None:
            logger.warning(
                "[imagery] provider=%s is no longer used by any user-facing surface "
                "(PA9, 2026-08-27); the surf height map draws the product basemap",
                _imagery_provider,
            )


def reset_imagery_settings_for_tests() -> None:
    """Reset module-level settings to defaults. Used in tests only."""
    global _imagery_provider, _imagery_tile_cache_ttl_seconds  # noqa: PLW0603
    _imagery_provider = None
    _imagery_tile_cache_ttl_seconds = 604800


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


def _get_imagery_tile_params(request: Request) -> ImageryTileQueryParams:
    try:
        return ImageryTileQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        from fastapi.exceptions import RequestValidationError

        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Provider selection (§LM-1d)
# ---------------------------------------------------------------------------


def _select_provider(lat: float, lon: float) -> str:
    """Return "naip", "esri", or "map" for the given coordinates.

    Only called when _imagery_provider is not None (caller's responsibility).
    "auto" resolves per-request via the CONUS bbox test and never selects
    "map" (naip-vs-esri only, unchanged by the IMAGERY-MAP round); an
    explicit "naip"/"esri"/"map" override pins that provider regardless of
    location.
    """
    if _imagery_provider in ("naip", "esri", "map"):
        return _imagery_provider
    # "auto"
    return "naip" if naip_module.is_conus(lat, lon) else "esri"


def _validate_tile_coords(z: int, x: int, y: int) -> None:
    """Reject out-of-range tile coordinates before forwarding to the provider.

    Amplification-surface guard (lead ruling 2026-08-03): a tile proxy that
    forwards arbitrary bbox math upstream unchecked lets a caller request
    absurd z/x/y combinations. z is bounded by the FastAPI Path constraint
    on the route; x/y must additionally be within [0, 2**z).
    """
    max_index = 2**z
    if not (0 <= x < max_index) or not (0 <= y < max_index):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Tile coordinates out of range for zoom {z}: x and y must be "
                f"within [0, {max_index}); got x={x}, y={y}"
            ),
        )


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

    Always answers with provider="basemap" — never 404s, and does not call
    _select_provider() (see module docstring, unchanged and unused in this
    endpoint). lat/lon are still validated
    (422 on missing/out-of-range) via the Depends params model but are not
    otherwise used — the basemap answer does not vary by coordinates.
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


@router.get(
    "/imagery/tiles/{z}/{x}/{y}",
    summary="NAIP imagery tile proxy",
    tags=["Imagery"],
    # No response_model: binary response, not JSON (see module docstring).
)
def get_imagery_tile(
    params: Annotated[ImageryTileQueryParams, Depends(_get_imagery_tile_params)],
    z: int = Path(..., ge=_MIN_ZOOM, le=_MAX_ZOOM, description="Slippy-map zoom level"),
    x: int = Path(..., ge=0, description="Slippy-map tile X"),
    y: int = Path(..., ge=0, description="Slippy-map tile Y"),
) -> Response:
    """Server-side proxy + cache for NAIP imagery tiles (§LM-1c).

    ESRI tiles never flow through this endpoint — the browser fetches them
    directly per proxyMode="direct" from /imagery/config.

    See module docstring for the full /tiles decision tree.
    """
    del params  # no query params accepted; present only for extra="forbid" enforcement

    _validate_tile_coords(z, x, y)

    if _imagery_provider is None or _imagery_provider in ("esri", "map"):
        logger.debug(
            "Imagery tile proxy: not available (provider=%r; NAIP tile proxy requires "
            "[imagery] provider to be 'auto' or 'naip')",
            _imagery_provider,
        )
        raise HTTPException(
            status_code=404,
            detail=(
                "NAIP tile proxy is not available for this deployment. "
                "Check the [imagery] provider setting in api.conf."
            ),
        )

    module = get_provider_module(domain="imagery", provider_id="naip")

    try:
        tile_bytes, content_type = module.get_tile(
            z, x, y, ttl_seconds=_imagery_tile_cache_ttl_seconds
        )
    except KeyInvalid as exc:
        # Not expected (NAIP is keyless) — handled defensively per the
        # canonical taxonomy contract.
        logger.error("Imagery tile proxy KeyInvalid: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    except QuotaExhausted as exc:
        logger.warning(
            "Imagery tile proxy QuotaExhausted retry_after=%s", exc.retry_after_seconds
        )
        headers: dict[str, str] = {}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(exc.retry_after_seconds)
        raise HTTPException(status_code=503, detail=str(exc), headers=headers) from exc

    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            logger.debug(
                "Imagery tile proxy: upstream 404 z=%d x=%d y=%d (out of NAIP domain)",
                z,
                x,
                y,
            )
            raise HTTPException(
                status_code=404,
                detail=f"NAIP tile not available at z={z} x={x} y={y}",
            ) from exc
        logger.error("Imagery tile proxy ProviderProtocolError: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    except TransientNetworkError as exc:
        logger.error("Imagery tile proxy TransientNetworkError: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(content=tile_bytes, media_type=content_type)
