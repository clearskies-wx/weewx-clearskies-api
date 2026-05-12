"""MSC GeoMet (Environment and Climate Change Canada) radar provider (ADR-015, ADR-038, 3b-14).

Five responsibilities per ADR-038 §2:
  1. Outbound API call  — WMS GetCapabilities (no auth)
  2. Response parsing   — XML TIME dimension via parse_wms_time_dimension()
  3. Translation        — timestamps → RadarFrame(kind="past"|"current")
  4. Capability         — CAPABILITY symbol consumed at startup
  5. Error handling     — canonical taxonomy

Frame-kind mapping (per docs/reference/api-docs/msc_geomet.md, brief lead call 4):
  All frames → "past", except the latest (max timestamp) → "current".
  No nowcast frames available.

Cache (ADR-017): TTL 60 s; key = SHA-256(provider_id, "frames").

WMS layer: RADAR_1KM_RDPR (dual-pol QPE rain-or-snow composite — recommended default).
  Per api-docs/msc_geomet.md: use RDPR for operator-configured default.
"""

from __future__ import annotations

import hashlib
import json
import logging

from weewx_clearskies_api.models.responses import RadarFrame, RadarFrameList
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import ProviderCapability
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter
from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "msc_geomet"
DOMAIN = "radar"
BASE_URL = "https://geo.weather.gc.ca"
FRAMES_PATH = "/geomet"
# Lead-direct 2026-05-11: brief + api-docs said RADAR_1KM_RDPR but live
# GetCapabilities (verified from test-author's real fixture) exposes only
# RADAR_1KM_RRAI (rain) and RADAR_1KM_RSNO (snow). RDPR returns "Layer not
# available". RRAI is the most universally useful default for the live radar tab.
LAYER_NAME = "RADAR_1KM_RRAI"  # 1 km rain composite
# TTL deviation: ADR-017's default for radar frame metadata is 5 min;
# brief lead-call 5 set 60s. ADR-017 amendment deferred (3b-14 auditor F3).
_CACHE_TTL = 60  # see deviation note above
_API_VERSION = "0.1.0"

ATTRIBUTION = "Environment and Climate Change Canada — MSC GeoMet"

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),
    geographic_coverage="canada",
    auth_required=(),
    default_poll_interval_seconds=_CACHE_TTL,
    operator_notes=(
        "Canada national mosaic via Environment Canada MSC GeoMet WMS service. "
        "Layer: RADAR_1KM_RRAI (1 km rain composite — RADAR_1KM_RSNO is the snow "
        "sibling, not used by default). 6-minute cadence. Open Government Licence "
        "— Canada; commercial use allowed with attribution: 'Environment and "
        "Climate Change Canada — MSC GeoMet'."
    ),
    wms_endpoint_url="https://geo.weather.gc.ca/geomet?",
    wms_layer_name=LAYER_NAME,
    tile_content_type="image/png",
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="msc-geomet-radar",
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
# Cache key
# ---------------------------------------------------------------------------


def _cache_key() -> str:
    payload = json.dumps({"provider_id": PROVIDER_ID, "kind": "frames"}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Wire → canonical translation
# ---------------------------------------------------------------------------


def _to_canonical_frames(timestamps: list[str]) -> list[RadarFrame]:
    """Map WMS-T timestamps to canonical RadarFrame list.

    Latest timestamp (max) → "current"; all others → "past".
    """
    if not timestamps:
        return []

    latest = max(timestamps)
    return [
        RadarFrame(time=ts, kind="current" if ts == latest else "past")
        for ts in timestamps
    ]


# ---------------------------------------------------------------------------
# Cache serialisation helpers
# ---------------------------------------------------------------------------


def _to_cacheable(frames_list: RadarFrameList) -> dict:  # type: ignore[type-arg]
    return frames_list.model_dump(mode="json")


def _from_cached(cached: dict) -> RadarFrameList:  # type: ignore[type-arg]
    return RadarFrameList.model_validate(cached)


# ---------------------------------------------------------------------------
# Public frame-index entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def get_frames() -> RadarFrameList:
    """Fetch MSC GeoMet radar frame index from WMS GetCapabilities.

    Uses the layer-filtered GetCapabilities endpoint for efficiency —
    requesting only RADAR_1KM_RDPR reduces response size significantly.

    Returns:
        RadarFrameList with providerId, frames, and attribution.

    Raises:
        QuotaExhausted: Server returned 429.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: XML parse error, layer not found, or no TIME dimension.
    """
    cache = get_cache()
    key = _cache_key()
    hit = cache.get(key)
    if hit is not None:
        return _from_cached(hit)

    _rate_limiter.acquire()

    response = _get_http_client().get(
        f"{BASE_URL}{FRAMES_PATH}",
        params={
            "service": "WMS",
            "version": "1.3.0",
            "request": "GetCapabilities",
            "layer": LAYER_NAME,  # layer filter reduces response size
        },
    )
    xml_bytes = response.content

    timestamps = parse_wms_time_dimension(
        xml_bytes, layer=LAYER_NAME, provider_id=PROVIDER_ID, domain=DOMAIN
    )

    frames = _to_canonical_frames(timestamps)
    result = RadarFrameList(
        providerId=PROVIDER_ID,
        frames=frames,
        attribution=ATTRIBUTION,
    )

    cache.set(key, _to_cacheable(result), ttl_seconds=_CACHE_TTL)

    logger.info(
        "MSC GeoMet radar frames fetched: %d frame(s)",
        len(frames),
    )
    return result


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
