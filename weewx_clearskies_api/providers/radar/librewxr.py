"""LibreWxR radar provider module (ADR-015, ADR-038).

Five responsibilities per ADR-038 §2:
  1. Outbound API call  — LibreWxR public weather-maps.json (no auth); tile proxy
  2. Response parsing   — JSON frame index → _LibreWxRWeatherMaps wire model;
                          tile response is binary WebP bytes
  3. Translation        — radar has no canonical-entity mapping (§4.5 confirmed);
                          RadarFrame(time=utc_iso, kind="past"|"current"|"nowcast")
  4. Capability         — CAPABILITY symbol consumed at startup
  5. Error handling     — canonical taxonomy (QuotaExhausted, TransientNetworkError,
                          ProviderProtocolError)

Frame-kind mapping (identical to RainViewer v2 — drop-in replacement):
  EXACTLY ONE entry in radar.past with max(time) → "current"
  All other radar.past entries                   → "past"
  radar.nowcast[i]                               → "nowcast"

Tile URL format (from docs/reference/api-docs/librewxr.md):
  {host}/v2/radar/{timestamp}/{size}/{z}/{x}/{y}/{color}/{smooth}_{snow}.{ext}
  Default: size=512, smooth=1, snow=0, ext=webp, color=2 (Universal Blue)

Cache (ADR-017):
  Frame index: key = SHA-256 of (provider_id, "frames").  TTL: 60 s.
  Tile bytes:  key = SHA-256 of (provider_id, "tile", z, x, y, t, color).
               TTL: 300 s.  Cache value is base64 envelope (same as OWM):
                 {"_tile_b64": "<base64>", "content_type": "image/webp"}

Configurable endpoint:
  Default: https://api.librewxr.net (public, best-effort, no SLA).
  Operator may self-host; configure() called from __main__.py to set the URL.

Rate limiter: max_calls=5, window_seconds=1 (polite-use guard; no documented limit).

ruff: noqa: N815  (field names match LibreWxR/RainViewer JSON camelCase)
"""

# ruff: noqa: N815

from __future__ import annotations

import base64
import hashlib
import json
import logging

from pydantic import BaseModel, ConfigDict, ValidationError

from weewx_clearskies_api.models.responses import RadarFrame, RadarFrameList
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import ProviderCapability
from weewx_clearskies_api.providers._common.datetime_utils import epoch_to_utc_iso8601
from weewx_clearskies_api.providers._common.errors import ProviderProtocolError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "librewxr"
DOMAIN = "radar"
_DEFAULT_ENDPOINT = "https://api.librewxr.net"
_FRAMES_PATH = "/public/weather-maps.json"
_FRAME_CACHE_TTL = 60    # 60 s — frame index (same conscious ADR-017 deviation as rainviewer)
_TILE_CACHE_TTL = 300    # 300 s — tile bytes per ADR-017 tile default
_API_VERSION = "0.1.0"
_DEFAULT_COLOR = 2       # Universal Blue (NEXRAD-style color scheme)
_DEFAULT_SIZE = 512      # pixel tile size
_DEFAULT_SMOOTH = 1      # Gaussian blur enabled
_DEFAULT_SNOW = 0        # uniform colour (no rain/snow differentiation)

ATTRIBUTION = "LibreWxR (https://librewxr.net/) — Data: CC-BY-4.0"

# ---------------------------------------------------------------------------
# Configurable endpoint
# ---------------------------------------------------------------------------

_configured_endpoint: str = _DEFAULT_ENDPOINT


def configure(*, endpoint: str | None = None) -> None:
    """Called from __main__.py to set the operator's LibreWxR endpoint.

    Allows operators to point the module at a self-hosted LibreWxR instance
    instead of the public api.librewxr.net (best-effort, no SLA).

    Args:
        endpoint: Base URL of the LibreWxR instance, e.g.
            "https://radar.example.com".  Trailing slash is stripped.
            None or empty string leaves the default unchanged.
    """
    global _configured_endpoint  # noqa: PLW0603
    if endpoint:
        _configured_endpoint = endpoint.rstrip("/")


# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),  # radar has no canonical-entity mapping (§4.5)
    geographic_coverage="global",
    auth_required=(),
    default_poll_interval_seconds=_FRAME_CACHE_TTL,
    operator_notes=(
        "LibreWxR — global radar, satellite, nowcast. Drop-in RainViewer v2 "
        "replacement. 13 color schemes, zoom 12, WebP. Public API at "
        "api.librewxr.net (no SLA) or self-hosted."
    ),
    tile_url_template="{host}{path}/{size}/{z}/{x}/{y}/{color}/{options}.webp",
    tile_content_type="image/webp",
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3) — polite-use guard (no documented limit)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="librewxr-radar",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Source: docs/reference/api-docs/librewxr.md + live capture 2026-06-24
# Format is drop-in identical to RainViewer v2 weather-maps.json.
# ---------------------------------------------------------------------------


class _LibreWxRFrameEntry(BaseModel):
    """One entry in radar.past or radar.nowcast array."""

    model_config = ConfigDict(extra="ignore")

    time: int   # Unix epoch seconds
    path: str   # tile path prefix, e.g. "/v2/radar/1782329400"


class _LibreWxRRadar(BaseModel):
    """radar sub-object in weather-maps.json."""

    model_config = ConfigDict(extra="ignore")

    past: list[_LibreWxRFrameEntry] = []
    nowcast: list[_LibreWxRFrameEntry] = []


class _LibreWxRWeatherMaps(BaseModel):
    """Top-level weather-maps.json response envelope.

    extra="ignore" so additional top-level keys (satellite, colorSchemes, etc.)
    don't break us.  Format is identical to RainViewer v2.
    """

    model_config = ConfigDict(extra="ignore")

    version: str
    generated: int   # Unix epoch seconds
    host: str        # tile host, e.g. "https://api.librewxr.net"
    radar: _LibreWxRRadar


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
# Cache key helpers (ADR-017)
# ---------------------------------------------------------------------------


def _frames_cache_key() -> str:
    """Cache key for the frame index (global — no station component)."""
    payload = json.dumps({"provider_id": PROVIDER_ID, "kind": "frames"}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _tile_cache_key(z: int, x: int, y: int, t: str | None, color: int) -> str:
    """Cache key for a tile byte response.

    Includes provider_id, z, x, y, t (frame timestamp), and color scheme.
    Credentials not in key (no auth required for LibreWxR).
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "kind": "tile",
            "z": z,
            "x": x,
            "y": y,
            "t": t,
            "color": color,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Cache serialisation helpers
# ---------------------------------------------------------------------------


def _frames_to_cacheable(frames_list: RadarFrameList) -> dict:  # type: ignore[type-arg]
    """Serialise RadarFrameList to a JSON-safe dict for Redis storage."""
    return frames_list.model_dump(mode="json")


def _frames_from_cached(cached: dict) -> RadarFrameList:  # type: ignore[type-arg]
    """Reconstruct RadarFrameList from a cached dict."""
    return RadarFrameList.model_validate(cached)


def _tile_to_cacheable(tile_bytes: bytes, content_type: str) -> dict:  # type: ignore[type-arg]
    """Wrap raw tile bytes into a JSON-safe dict for the cache backend.

    RedisCache.set() calls json.dumps() — raw bytes are not JSON-encodable.
    base64 envelope keeps the existing cache abstraction unchanged.
    ~33% storage overhead per tile is acceptable at v0.1.
    """
    return {
        "_tile_b64": base64.b64encode(tile_bytes).decode("ascii"),
        "content_type": content_type,
    }


def _tile_from_cached(cached: dict) -> tuple[bytes, str]:  # type: ignore[type-arg]
    """Reconstruct (bytes, content_type) from a cached base64 envelope."""
    return base64.b64decode(cached["_tile_b64"]), cached["content_type"]


# ---------------------------------------------------------------------------
# Wire → canonical translation
# ---------------------------------------------------------------------------


def _to_canonical_frames(parsed: _LibreWxRWeatherMaps) -> list[RadarFrame]:
    """Map LibreWxR wire model to a list of canonical RadarFrame instances.

    Frame-kind rule (identical to RainViewer v2 per librewxr.md):
      The single past entry with max(time) → "current"
      All other past entries               → "past"
      nowcast entries                      → "nowcast"

    There is always exactly ONE "current" frame in a non-empty past list.
    """
    frames: list[RadarFrame] = []

    if parsed.radar.past:
        latest_past_time = max(entry.time for entry in parsed.radar.past)
        for entry in parsed.radar.past:
            kind = "current" if entry.time == latest_past_time else "past"
            frames.append(
                RadarFrame(
                    time=epoch_to_utc_iso8601(entry.time, provider_id=PROVIDER_ID, domain=DOMAIN),
                    kind=kind,
                    path=entry.path,
                )
            )

    for entry in parsed.radar.nowcast:
        frames.append(
            RadarFrame(
                time=epoch_to_utc_iso8601(entry.time, provider_id=PROVIDER_ID, domain=DOMAIN),
                kind="nowcast",
                path=entry.path,
            )
        )

    return frames


# ---------------------------------------------------------------------------
# Public frame-index entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def get_frames() -> RadarFrameList:
    """Fetch the LibreWxR radar frame index and return canonical RadarFrameList.

    Cache stores post-normalisation dict (JSON-serialisable per ADR-017).
    On cache hit the dict is reconstructed into a RadarFrameList model.

    Returns:
        RadarFrameList with providerId, frames, attribution, and tileHost.

    Raises:
        QuotaExhausted: Rate limit exceeded (5 req/s guard).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed (schema change).
    """
    cache = get_cache()
    key = _frames_cache_key()
    hit = cache.get(key)
    if hit is not None:
        return _frames_from_cached(hit)

    _rate_limiter.acquire()

    response = _get_http_client().get(f"{_configured_endpoint}{_FRAMES_PATH}")

    try:
        raw_json = response.json()
        parsed = _LibreWxRWeatherMaps.model_validate(raw_json)
    except (ValidationError, ValueError) as exc:
        logger.error(
            "LibreWxR response validation failed: %s. Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"LibreWxR response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    frames = _to_canonical_frames(parsed)
    result = RadarFrameList(
        providerId=PROVIDER_ID,
        frames=frames,
        attribution=ATTRIBUTION,
        tileHost=parsed.host,
    )

    cache.set(key, _frames_to_cacheable(result), ttl_seconds=_FRAME_CACHE_TTL)

    logger.info(
        "LibreWxR radar frames fetched: %d frame(s) (%d past, %d nowcast)",
        len(frames),
        sum(1 for f in frames if f.kind in ("past", "current")),
        sum(1 for f in frames if f.kind == "nowcast"),
    )
    return result


# ---------------------------------------------------------------------------
# Public tile entrypoint (ADR-038 §2) — binary response
# ---------------------------------------------------------------------------


def get_tile(
    z: int,
    x: int,
    y: int,
    *,
    t: str | None = None,
    color: int | None = None,
) -> tuple[bytes, str]:
    """Fetch a single radar tile from LibreWxR and return (bytes, content_type).

    Tile URL format (from docs/reference/api-docs/librewxr.md):
      {host}/v2/radar/{timestamp}/{size}/{z}/{x}/{y}/{color}/{smooth}_{snow}.{ext}
      Defaults: size=512, smooth=1, snow=0, ext=webp, color=2 (Universal Blue)

    The response is raw binary WebP bytes — no Pydantic response model, no JSON
    body.  The endpoint handler wraps these bytes in fastapi.Response(content=bytes,
    media_type=ct).

    Cache uses a base64 envelope:
      {"_tile_b64": "<base64>", "content_type": "image/webp"}
    TTL: 300s (ADR-017 tile-bytes default).

    Args:
        z: Slippy-map zoom level (0–12; LibreWxR max zoom is 12).
        x: Tile X coordinate.
        y: Tile Y coordinate.
        t: Optional frame timestamp path prefix from RadarFrame.path
            (e.g. "/v2/radar/1782329400").  If None, builds the URL using
            only the default path segment (current frame behaviour).
        color: Color scheme ID (0–11 or 255 for raw grayscale).
            Default: 2 (Universal Blue / NEXRAD-style).

    Returns:
        (tile_bytes, content_type) — caller wraps in fastapi.Response.

    Raises:
        QuotaExhausted: Rate limit exceeded (5 req/s guard).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Unexpected 4xx (including 404 for out-of-domain
            tiles — status_code=404 on the exception).
    """
    color_id = color if color is not None else _DEFAULT_COLOR

    cache = get_cache()
    key = _tile_cache_key(z, x, y, t, color_id)
    hit = cache.get(key)
    if hit is not None:
        return _tile_from_cached(hit)

    _rate_limiter.acquire()

    # Build tile URL.
    # If t is provided it is the full path prefix from RadarFrame.path,
    # e.g. "/v2/radar/1782329400".  Strip leading slash when concatenating.
    if t is not None:
        # t may be an ISO-8601 timestamp string (from the endpoint ?t= param)
        # or a raw path string (from RadarFrame.path).  Normalise by using
        # the path directly if it starts with "/v2/", otherwise treat as a
        # bare timestamp segment.
        path_segment = t.lstrip("/") if t.startswith("/v2/") else f"v2/radar/{t}"
    else:
        # No timestamp — use a path that lets the server serve the latest frame.
        # LibreWxR inherits RainViewer's convention that tile requests without
        # a timestamp return the most recent frame.
        path_segment = "v2/radar"

    tile_url = (
        f"{_configured_endpoint}/{path_segment}"
        f"/{_DEFAULT_SIZE}/{z}/{x}/{y}/{color_id}"
        f"/{_DEFAULT_SMOOTH}_{_DEFAULT_SNOW}.webp"
    )

    response = _get_http_client().get(tile_url)

    content_type = response.headers.get("Content-Type", "image/webp")
    tile_bytes = response.content

    cache.set(key, _tile_to_cacheable(tile_bytes, content_type), ttl_seconds=_TILE_CACHE_TTL)

    logger.debug(
        "[%s] tile fetched: z=%d x=%d y=%d color=%d content_type=%r size=%d bytes",
        PROVIDER_ID,
        z,
        x,
        y,
        color_id,
        content_type,
        len(tile_bytes),
    )
    return tile_bytes, content_type


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None


def _reset_configured_endpoint_for_tests() -> None:
    """Reset module-level configured endpoint to default.  Used in tests only."""
    global _configured_endpoint  # noqa: PLW0603
    _configured_endpoint = _DEFAULT_ENDPOINT
