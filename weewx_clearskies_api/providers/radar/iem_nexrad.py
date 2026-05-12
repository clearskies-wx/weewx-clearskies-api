"""Iowa Environmental Mesonet NEXRAD radar provider module (ADR-015, ADR-038, 3b-14).

Five responsibilities per ADR-038 §2:
  1. Outbound API call  — WMS GetCapabilities (no auth)
  2. Response parsing   — XML TIME dimension via parse_wms_time_dimension()
  3. Translation        — timestamps → RadarFrame(kind="past"|"current")
  4. Capability         — CAPABILITY symbol consumed at startup
  5. Error handling     — canonical taxonomy (QuotaExhausted, TransientNetworkError,
                          ProviderProtocolError; ProviderProtocolError also raised
                          by parse_wms_time_dimension() on XML/layer/dimension errors)

Frame-kind mapping (per docs/reference/api-docs/iem_nexrad.md, brief lead call 4):
  All frames → "past", except the latest (max timestamp) → "current".
  No nowcast frames available from IEM WMS-T.

Cache (ADR-017):
  Key: SHA-256 of (provider_id, "frames").  No lat/lon — frame index is global
  per provider (brief lead call 5).  TTL: 60 s.

WMS layer: N0Q — 8-bit base reflectivity, 0.5 dBZ resolution.
  Per api-docs/iem_nexrad.md: use N0Q as default; N0R is legacy.
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

PROVIDER_ID = "iem_nexrad"
DOMAIN = "radar"
BASE_URL = "https://mesonet.agron.iastate.edu"
FRAMES_PATH = "/cgi-bin/wms/nexrad/n0q-t.cgi"
LAYER_NAME = "nexrad-n0q-wmst"
_CACHE_TTL = 60
_API_VERSION = "0.1.0"

ATTRIBUTION = "NEXRAD imagery courtesy of Iowa Environmental Mesonet."

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),  # radar has no canonical-entity mapping (§4.5)
    geographic_coverage="us-conus",
    auth_required=(),
    default_poll_interval_seconds=_CACHE_TTL,
    operator_notes=(
        "NEXRAD N0Q (8-bit base reflectivity, 0.5 dBZ resolution) via Iowa Environmental "
        "Mesonet WMS-T service. CONUS-only — use noaa_mrms for AK/HI/PR/Guam. "
        "5-minute cadence. Attribution recommended: "
        "'NEXRAD imagery courtesy of Iowa Environmental Mesonet.'"
    ),
    wms_endpoint_url="https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi?",
    wms_layer_name=LAYER_NAME,
    tile_content_type="image/png",
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="iem-nexrad-radar",
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

    Frame-kind rule (brief lead call 4):
      All frames are "past" except the latest (max timestamp) which is "current".
      No nowcast frames for WMS-T providers.

    Args:
        timestamps: ISO-8601 UTC timestamp strings, already sorted ascending
            by parse_wms_time_dimension (order from WMS is preserved; we
            determine latest by max()).

    Returns:
        List of RadarFrame objects.
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
    """Fetch IEM NEXRAD radar frame index from WMS GetCapabilities.

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
        params={"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities"},
    )
    xml_bytes = response.content

    # parse_wms_time_dimension raises ProviderProtocolError on any XML/layer/
    # dimension error — let it propagate (do not re-wrap; coding.md §3 "don't
    # re-construct canonical exceptions you've already received").
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
        "IEM NEXRAD radar frames fetched: %d frame(s)",
        len(frames),
    )
    return result


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
