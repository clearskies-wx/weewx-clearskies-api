"""Geographic features endpoints — PMTiles file serving and management (ADR-078).

Endpoints:
  GET  /api/v1/geographic-features/tiles   — serve PMTiles file (public)
  GET  /api/v1/geographic-features/status  — availability status (public)
  POST /setup/geographic-features/update   — download + extract PMTiles (auth required)

The PMTiles file is served directly via Starlette FileResponse which handles
HTTP Range requests natively (returns 206 Partial Content for Range headers).
This lets MapLibre GL JS / PMTiles JS library fetch only the needed tiles via
byte-range requests — the full file is never transmitted in one shot.

Auth pattern for the setup endpoint:
  Uses the same X-Clearskies-Proxy-Auth header check as other /setup/* endpoints
  (_check_proxy_auth from setup.py).  If that causes a circular import, the
  5-line pattern is copied inline.

Two routers are defined:
  router       — data endpoints, mounted with prefix /api/v1 in app.py
  setup_router — setup endpoint, mounted without prefix (lives at /setup/...)
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from weewx_clearskies_api.services.geographic_features import (
    PMTILES_PATH,
    download_and_extract,
    get_pmtiles_status,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level settings wiring (populated at startup)
# ---------------------------------------------------------------------------

_bounds: str | None = None      # "west,south,east,north" from config
_maxzoom: int = 12              # maximum zoom level from config


def wire_geographic_features_settings(settings: object) -> None:
    """Store geographic features settings for use by the setup endpoint.

    Extracts bounds and maxzoom from settings.geographic_features.
    Called from __main__.py after settings load.
    Tests that don't care about these values leave them at module defaults.
    """
    global _bounds, _maxzoom  # noqa: PLW0603
    gf_section = getattr(settings, "geographic_features", None)
    if gf_section is not None:
        _bounds = getattr(gf_section, "bounds", None)
        _maxzoom = int(getattr(gf_section, "maxzoom", 12))


# ---------------------------------------------------------------------------
# Auth helper — mirrors _check_proxy_auth from endpoints/setup.py
# Copied inline to avoid circular import risk (setup.py imports from app.py
# via trust, and app.py imports from here).
# ---------------------------------------------------------------------------


def _check_proxy_auth(request: Request) -> bool:
    """Return True if the request carries a valid X-Clearskies-Proxy-Auth header.

    Constant-time comparison against WEEWX_CLEARSKIES_PROXY_SECRET.
    Returns False (not raises) when the secret env var is unset.
    """
    secret = os.environ.get("WEEWX_CLEARSKIES_PROXY_SECRET", "").strip()
    if not secret:
        return False
    provided = request.headers.get("X-Clearskies-Proxy-Auth", "")
    if not provided:
        return False
    return hmac.compare_digest(secret.encode("utf-8"), provided.encode("utf-8"))


# ---------------------------------------------------------------------------
# Data router (prefix /api/v1 in app.py)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["geographic-features"])


@router.get(
    "/geographic-features/tiles",
    summary="Serve PMTiles geographic features file",
    tags=["geographic-features"],
)
def get_tiles(request: Request) -> FileResponse:
    """Serve the PMTiles geographic features file.

    Starlette's FileResponse handles HTTP Range requests natively, returning
    206 Partial Content for requests with a Range header.  This allows the
    MapLibre GL JS / PMTiles JS library to fetch individual tile data via
    byte-range requests — the client never downloads the entire file.

    Returns 404 with a JSON body when the PMTiles file has not been downloaded
    yet (direct the operator to the admin panel).
    """
    if not PMTILES_PATH.exists():
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    "Geographic features data not available. "
                    "Use the admin panel to download map data."
                )
            },
        )

    return FileResponse(
        path=str(PMTILES_PATH),
        media_type="application/octet-stream",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get(
    "/geographic-features/status",
    summary="Geographic features PMTiles availability status",
    tags=["geographic-features"],
)
def get_geographic_features_status() -> dict:
    """Return availability status of the PMTiles geographic features file.

    Returns:
        JSON object with:
          - available (bool): whether the PMTiles file exists
          - size_bytes (int|null): file size in bytes, null if not available
          - updated_at (str|null): ISO 8601 UTC modification time, null if not available
    """
    return get_pmtiles_status()


# ---------------------------------------------------------------------------
# Setup router (no prefix in app.py — lives at /setup/...)
# ---------------------------------------------------------------------------

setup_router = APIRouter(prefix="/setup", tags=["geographic-features-setup"])


@setup_router.post(
    "/geographic-features/update",
    summary="Download and extract PMTiles geographic features file",
    tags=["geographic-features-setup"],
)
def post_geographic_features_update(request: Request) -> dict:
    """Download and extract a BBOX-clipped PMTiles file from the Protomaps CDN.

    Requires X-Clearskies-Proxy-Auth header (same shared secret as other admin
    operations).  This endpoint is synchronous — it blocks while the pmtiles
    CLI downloads and extracts the file.  Duration depends on bbox size and
    zoom level; typical runs take 30-120 seconds.

    Uses bounds and maxzoom from [geographic_features] in api.conf.
    When bounds is not configured, extracts a global tile set.

    Returns:
        JSON object with: status ("ok"), size_bytes (int), updated_at (ISO 8601 str).

    Raises:
        401: Missing or invalid X-Clearskies-Proxy-Auth header.
        503: Proxy secret not configured (admin operations unavailable).
        500: pmtiles CLI not found, extraction failed, or filesystem error.
    """
    # --- Auth check ---
    secret_configured = bool(os.environ.get("WEEWX_CLEARSKIES_PROXY_SECRET", "").strip())
    if not secret_configured:
        raise HTTPException(
            status_code=503,
            detail="Proxy secret not configured — admin operations unavailable.",
        )
    if not _check_proxy_auth(request):
        raise HTTPException(
            status_code=401,
            detail="Requires valid X-Clearskies-Proxy-Auth header.",
        )

    # --- Build extraction parameters ---
    bounds = _bounds
    maxzoom = _maxzoom

    # When no bounds configured, use a wide global default that covers most
    # inhabited areas (excluding poles).
    if bounds is None:
        bounds = "-180,-85,180,85"
        logger.warning(
            "No [geographic_features] bounds configured; "
            "extracting global tiles (bounds=%s maxzoom=%d). "
            "This may take a long time. Consider setting bounds in api.conf.",
            bounds,
            maxzoom,
        )
    else:
        logger.info(
            "Extracting PMTiles: bounds=%r maxzoom=%d", bounds, maxzoom
        )

    # --- Run extraction ---
    try:
        result = download_and_extract(bounds=bounds, maxzoom=maxzoom)
    except RuntimeError as exc:
        logger.error("PMTiles extraction failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"PMTiles extraction failed: {exc}",
        ) from exc

    return result
