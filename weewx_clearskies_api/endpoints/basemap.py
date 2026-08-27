"""Basemap endpoints -- tiered PMTiles serving + status + admin extraction
(M1 -- CS-BASEMAP, plan MARINE-AND-MAPS-PLAN-2026-08-27 SS"M1").

Generalises ADR-078's single geographic-features PMTiles file/endpoint into
three tiers (world/local/radar) serving every Clear Skies map surface.
ADR-078's own endpoints/service (endpoints/geographic_features.py,
services/geographic_features.py) stay live this round (additive build) --
this module does not touch them.

Endpoints:
  GET  /api/v1/basemap/{tier}/tiles  -- serve one tier's PMTiles file (public)
  GET  /api/v1/basemap/status        -- per-tier availability + extract state (public)
  POST /setup/basemap/update         -- start (or 409) a background extract of
                                         all three tiers (auth required)

Starlette's FileResponse handles HTTP Range requests natively (206 Partial
Content), same mechanism ADR-078's tiles endpoint uses.

Auth pattern for the setup endpoint mirrors endpoints/geographic_features.py
:69-81, 176-187 -- copied inline (not imported) for the same circular-import
reason documented there.

Two routers, mirroring geographic_features.py:
  router       -- data endpoints, mounted with prefix /api/v1 in app.py
  setup_router -- setup endpoint, mounted without prefix (lives at /setup/...)
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from weewx_clearskies_api.services import basemap_extract

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level settings wiring (populated at startup)
# ---------------------------------------------------------------------------

#: The full Settings instance -- basemap_extract's compute_local_bounds()/
#: compute_radar_bounds() need settings.earthquakes and settings.radar, not
#: just [basemap] (which has one key, enabled).
_settings: object | None = None
_enabled: bool = True


def wire_basemap_settings(settings: object) -> None:
    """Store the Settings reference for the setup endpoint + gate tile
    serving on [basemap] enabled. Called from __main__.py after settings load.
    """
    global _settings, _enabled  # noqa: PLW0603
    _settings = settings
    basemap_section = getattr(settings, "basemap", None)
    _enabled = (
        bool(getattr(basemap_section, "enabled", True))
        if basemap_section is not None
        else True
    )


def reset_basemap_settings_for_tests() -> None:
    """Reset module-level wiring to defaults. Used in tests only."""
    global _settings, _enabled  # noqa: PLW0603
    _settings = None
    _enabled = True


# ---------------------------------------------------------------------------
# Auth helper -- mirrors _check_proxy_auth from endpoints/geographic_features.py
# Copied inline to avoid circular import risk (same reason documented there).
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

router = APIRouter(tags=["basemap"])


@router.get(
    "/basemap/{tier}/tiles",
    summary="Serve one basemap tier's PMTiles file",
    tags=["basemap"],
)
def get_basemap_tiles(tier: str, request: Request) -> FileResponse:
    """Serve one tier's PMTiles file (world/local/radar).

    404 problem+json (via the global RFC 9457 handler, ADR-018) for an
    unknown tier; 404 plain JSON (ADR-078's shape) when the tier is known
    but its file has not been extracted yet, or when [basemap] enabled=false.
    """
    if tier not in basemap_extract.TIERS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown basemap tier {tier!r}. Valid tiers: "
                f"{sorted(basemap_extract.TIERS)}."
            ),
        )

    path = basemap_extract.tier_path(tier)
    if not _enabled or not path.exists():
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"Basemap tier {tier!r} not available. "
                    "Use the admin panel to update the basemap."
                )
            },
        )

    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get(
    "/basemap/status",
    summary="Basemap extract status (all three tiers)",
    tags=["basemap"],
)
def get_basemap_status() -> dict:
    """Return per-tier availability + extract-run state.

    Shape: {tiers: {world|local|radar: {available, size_bytes, updated_at,
    bounds, minzoom, maxzoom}}, updating, last_error, last_started_at,
    last_finished_at}.
    """
    return basemap_extract.get_basemap_status()


# ---------------------------------------------------------------------------
# Setup router (no prefix in app.py -- lives at /setup/...)
# ---------------------------------------------------------------------------

setup_router = APIRouter(prefix="/setup", tags=["basemap-setup"])


@setup_router.post(
    "/basemap/update",
    summary="Start a background basemap extraction (world+local+radar)",
    tags=["basemap-setup"],
)
def post_basemap_update(request: Request) -> JSONResponse:
    """Start the background extract thread for all three tiers.

    Requires X-Clearskies-Proxy-Auth header (same shared secret as other
    admin operations). One extraction runs at a time.

    Returns:
        202 {"status": "started"} -- extraction begun in the background.
        409 {"status": "already_running"} -- an extraction is already running.

    Raises:
        401: Missing or invalid X-Clearskies-Proxy-Auth header.
        503: Proxy secret not configured (admin operations unavailable).
    """
    secret_configured = bool(os.environ.get("WEEWX_CLEARSKIES_PROXY_SECRET", "").strip())
    if not secret_configured:
        raise HTTPException(
            status_code=503,
            detail="Proxy secret not configured -- admin operations unavailable.",
        )
    if not _check_proxy_auth(request):
        raise HTTPException(
            status_code=401,
            detail="Requires valid X-Clearskies-Proxy-Auth header.",
        )

    started = basemap_extract.start_extract_in_background(_settings)
    if not started:
        return JSONResponse(status_code=409, content={"status": "already_running"})
    return JSONResponse(status_code=202, content={"status": "started"})
