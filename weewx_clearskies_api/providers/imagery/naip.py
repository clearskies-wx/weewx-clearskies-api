"""USGS NAIP Plus imagery provider module (Phase LM, §LM-1).

Five responsibilities per PROVIDER-MANUAL §1:
  1. Outbound API call — USGSNAIPPlus ImageServer `exportImage` operation.
     USGSNAIPPlus is a **dynamic** ArcGIS ImageServer, not a cached tile
     service: live `GET .../USGSNAIPPlus/ImageServer?f=json` (2026-08-02)
     returns `exportTilesAllowed: false` and no `tileInfo` object, so there
     is no `/tile/{z}/{y}/{x}` shortcut (confirmed against the USGS FAQ,
     "What are the URLs for imagery services in The National Map, and are
     they cached or dynamic?" — https://www.usgs.gov/faqs/what-are-urls-imagery-services-national-map-and-are-they-cached-or-dynamic).
     Each requested slippy-map tile (z/x/y) is converted to a Web Mercator
     bbox and rasterized on demand via `exportImage?bbox=...&size=256,256`.
  2. Response parsing — tile response is binary PNG; no Pydantic wire model
     (same non-JSON shape as radar/openweathermap.py's get_tile()).
  3. Translation — imagery has no canonical-entity mapping; it is
     DISPLAY-ONLY per the Phase LM hard rule (nothing here feeds SWAN, the
     1D model, transect selection, or any physics path).
  4. Capability declaration — CAPABILITY symbol, geographic_coverage="CONUS".
  5. Error handling — canonical taxonomy via ProviderHTTPClient.get()
     (QuotaExhausted, TransientNetworkError, ProviderProtocolError). No
     re-construction; upstream 404 (out-of-domain z/x/y bbox) propagates via
     ProviderHTTPClient's status_code attribute per coding.md's "dispatch on
     exception state via attributes" rule.

Keyless provider — no auth_required, no rate-limit documented by USGS. A
polite-use RateLimiter (5 req/s) is applied anyway, matching every other
keyless provider module in this codebase.

Cache layer (ADR-017):
  Tile bytes cached with a base64 envelope (JSON-safe cache backend, same
  pattern as radar/openweathermap.py's _tile_to_cacheable/_tile_from_cached).
  TTL is operator-configurable via `[imagery] tile_cache_ttl_seconds`
  (default 7 days — NAIP tiles are static imagery, unlike weather radar
  frames which must never be treated as a static asset per coding.md §1).
  Caller (endpoints/imagery.py) passes the configured TTL into get_tile();
  this module does not read Settings directly (matches the module-contract
  boundary — settings wiring lives at the endpoint layer via
  wire_imagery_settings()).

CONUS bounds (`CONUS_BOUNDS` / `is_conus()`):
  Simple bounding-box test (lead call 2 — "documented constant, not a new
  dependency"), used by endpoints/imagery.py to decide NAIP-vs-ESRI provider
  selection (§LM-1d). Not a precise polygon — acceptable because ESRI covers
  the same area as a fallback; NAIP is a quality upgrade, not the only
  source.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math

from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "naip"
DOMAIN = "imagery"

# Dynamic ImageServer — exportImage, not a cached tile endpoint (see module
# docstring §1 for the live-verification citation).
_EXPORT_URL = (
    "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage"
)
# 512, not the slippy-map-conventional 256 (operator-approved 2026-08-11):
# exportImage rasterizes on demand, so raster size is independent of the
# tile grid. At the heat map's typical zoom 17 (8-tile mosaic cap over a
# ~1.8 km frame), 256px sampled NAIP at ~1.0 m/px — coarser than the
# source's ~0.6 m native GSD. 512px doubles that to ~0.5 m/px (at-native)
# with the same tile count and bboxes. Tile placement is unaffected: the
# dashboard stretches each tile to its ground rectangle regardless of
# raster size (HeatMapCard <image> with preserveAspectRatio="none").
_TILE_SIZE_PX = 512
_DEFAULT_TILE_TTL = 604800  # 7 days — static imagery (see module docstring)
_API_VERSION = "0.1.0"

ATTRIBUTION = "USGS National Agriculture Imagery Program (NAIP) — public domain"

# CONUS bounding box (approximate rectangle, not a precise polygon).
CONUS_BOUNDS = {
    "south": 24.396308,
    "west": -125.0,
    "north": 49.384358,
    "east": -66.93457,
}

# ---------------------------------------------------------------------------
# Capability declaration (PROVIDER-MANUAL §1)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),  # imagery has no canonical-entity mapping (display-only)
    geographic_coverage="CONUS",
    auth_required=(),
    default_poll_interval_seconds=_DEFAULT_TILE_TTL,
    operator_notes=(
        "USGS NAIP Plus — public domain, no usage limits, keyless. CONUS only "
        "(use 'esri' or 'auto' for non-CONUS spots). Dynamic ImageServer — "
        "tiles are rasterized on demand via exportImage, not served from a "
        "pre-cached tile pyramid. Tile bytes cached server-side "
        "([imagery] tile_cache_ttl_seconds, default 7 days)."
    ),
    tile_content_type="image/png",
    refresh_interval=_DEFAULT_TILE_TTL,
    attribution=ProviderAttribution(
        attribution_required=False,
        display_name="USGS NAIP",
        attribution_text=ATTRIBUTION,
        text_prefix="",
        text_provider_name="USGS NAIP",
        url="https://naip-usdaonline.hub.arcgis.com/",
        logo_required=False,
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter — polite-use guard (no documented upstream limit)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="naip-imagery",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# HTTP client (module-level singleton)
# ---------------------------------------------------------------------------

_http_client: ProviderHTTPClient | None = None


def _get_http_client() -> ProviderHTTPClient:
    global _http_client  # noqa: PLW0603
    if _http_client is None:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent=f"weewx-clearskies-api/{_API_VERSION}",
        )
    return _http_client


# ---------------------------------------------------------------------------
# CONUS bounds check
# ---------------------------------------------------------------------------


def is_conus(lat: float, lon: float) -> bool:
    """Return True if (lat, lon) falls within the CONUS bounding box.

    Simple bbox test — see module docstring "CONUS bounds" section.
    """
    return (
        CONUS_BOUNDS["south"] <= lat <= CONUS_BOUNDS["north"]
        and CONUS_BOUNDS["west"] <= lon <= CONUS_BOUNDS["east"]
    )


# ---------------------------------------------------------------------------
# Slippy-tile z/x/y -> Web Mercator (EPSG:3857) bbox
# ---------------------------------------------------------------------------

# Half the circumference of the Web Mercator projection (meters), i.e. the
# standard XYZ-tile-to-EPSG:3857 origin shift used by every slippy-map server.
_ORIGIN_SHIFT = 2 * math.pi * 6378137 / 2.0  # 20037508.342789244


def _tile_to_web_mercator_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Convert slippy-map tile coordinates to an EPSG:3857 bbox (xmin, ymin, xmax, ymax).

    NAIP has no native tile grid (dynamic ImageServer); this bbox is passed
    to exportImage to rasterize a 256x256 tile at the correct resolution.
    """
    n = 2**z
    tile_size_m = (2 * _ORIGIN_SHIFT) / n
    xmin = -_ORIGIN_SHIFT + x * tile_size_m
    xmax = -_ORIGIN_SHIFT + (x + 1) * tile_size_m
    ymax = _ORIGIN_SHIFT - y * tile_size_m
    ymin = _ORIGIN_SHIFT - (y + 1) * tile_size_m
    return xmin, ymin, xmax, ymax


# ---------------------------------------------------------------------------
# Cache key + serialisation helpers (ADR-017, LC-A base64 envelope pattern)
# ---------------------------------------------------------------------------


def _build_tile_cache_key(z: int, x: int, y: int) -> str:
    payload = json.dumps(
        # px in the key so cached tiles at an older raster size (7-day TTL)
        # can never be served after a _TILE_SIZE_PX change.
        {"provider_id": PROVIDER_ID, "endpoint": "tile", "z": z, "x": x, "y": y, "px": _TILE_SIZE_PX},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _tile_to_cacheable(tile_bytes: bytes, content_type: str) -> dict:  # type: ignore[type-arg]
    return {
        "_tile_b64": base64.b64encode(tile_bytes).decode("ascii"),
        "content_type": content_type,
    }


def _tile_from_cached(cached: dict) -> tuple[bytes, str]:  # type: ignore[type-arg]
    return base64.b64decode(cached["_tile_b64"]), cached["content_type"]


# ---------------------------------------------------------------------------
# Public tile entrypoint
# ---------------------------------------------------------------------------


def get_tile(z: int, x: int, y: int, *, ttl_seconds: int = _DEFAULT_TILE_TTL) -> tuple[bytes, str]:
    """Fetch a single 256x256 tile from USGSNAIPPlus and return (bytes, content_type).

    z/x/y range validation is the endpoint's responsibility (endpoints/imagery.py)
    per coding.md's trust-boundary rule — this function assumes valid inputs.

    Args:
        z, x, y: Slippy-map tile coordinates.
        ttl_seconds: Cache TTL, sourced from [imagery] tile_cache_ttl_seconds
            by the caller. Defaults to 7 days if not provided.

    Returns:
        (tile_bytes, content_type) — caller wraps in fastapi.Response.

    Raises:
        QuotaExhausted: upstream 429.
        TransientNetworkError: network/5xx after retries.
        ProviderProtocolError: unexpected 4xx (including out-of-domain bbox
            requests — status_code set on the exception; endpoint dispatches
            on the attribute per coding.md, not the message string).
    """
    cache = get_cache()
    key = _build_tile_cache_key(z, x, y)
    hit = cache.get(key)
    if hit is not None:
        return _tile_from_cached(hit)

    _rate_limiter.acquire()

    xmin, ymin, xmax, ymax = _tile_to_web_mercator_bbox(z, x, y)

    response = _get_http_client().get(
        _EXPORT_URL,
        params={
            "bbox": f"{xmin},{ymin},{xmax},{ymax}",
            "bboxSR": "102100",
            "imageSR": "102100",
            "size": f"{_TILE_SIZE_PX},{_TILE_SIZE_PX}",
            "format": "png",
            "f": "image",
        },
    )

    content_type = response.headers.get("Content-Type", "image/png")
    tile_bytes = response.content

    cache.set(key, _tile_to_cacheable(tile_bytes, content_type), ttl_seconds=ttl_seconds)

    logger.debug(
        "[%s] tile fetched: z=%d x=%d y=%d content_type=%r size=%d bytes",
        PROVIDER_ID,
        z,
        x,
        y,
        content_type,
        len(tile_bytes),
    )
    return tile_bytes, content_type


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
