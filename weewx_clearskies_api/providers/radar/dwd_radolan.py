"""DWD RADOLAN (Deutscher Wetterdienst) radar provider module (ADR-015, ADR-038, 3b-14).

Five responsibilities per ADR-038 §2:
  1. Outbound API call  — WMS GetCapabilities (no auth)
  2. Response parsing   — XML TIME dimension via parse_wms_time_dimension()
  3. Translation        — timestamps → RadarFrame(kind="past"|"current")
  4. Capability         — CAPABILITY symbol consumed at startup
  5. Error handling     — canonical taxonomy

Frame-kind mapping (per docs/reference/api-docs/dwd_radolan.md, brief lead call 4):
  All frames → "past", except the latest (max timestamp) → "current".
  No nowcast frames available from RX-Produkt (WN-Produkt offers prediction
  frames separately but is out of scope for v0.1 — use plain RX).

Cache (ADR-017): TTL 60 s; key = SHA-256(provider_id, "frames").

WMS layer: dwd:RX-Produkt (5-min reflectivity composite — recommended default).
  Per api-docs/dwd_radolan.md. DWD layers use "dwd:" namespace prefix.
"""

from __future__ import annotations

import hashlib
import json
import logging

from weewx_clearskies_api.models.responses import RadarFrame, RadarFrameList
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter
from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "dwd_radolan"
DOMAIN = "radar"
BASE_URL = "https://maps.dwd.de"
FRAMES_PATH = "/geoserver/dwd/wms"
# Lead-direct 2026-05-11: brief + api-docs said "dwd:RX-Produkt" but live
# GetCapabilities (verified from test-author's real fixture) names the
# 5-min reflectivity layer "Niederschlagsradar" (German: "precipitation radar").
# "dwd:RX-Produkt" is not a valid layer name in the current GeoServer.
LAYER_NAME = "Niederschlagsradar"  # 5-min reflectivity composite
# TTL deviation: ADR-017's default for radar frame metadata is 5 min;
# brief lead-call 5 set 60s. ADR-017 amendment deferred (3b-14 auditor F3).
_CACHE_TTL = 60  # see deviation note above
_API_VERSION = "0.1.0"

ATTRIBUTION = "Source: Deutscher Wetterdienst (DWD)"

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),
    geographic_coverage="germany",
    auth_required=(),
    default_poll_interval_seconds=_CACHE_TTL,
    operator_notes=(
        "RADOLAN Niederschlagsradar (5-min reflectivity composite) via DWD GeoServer "
        "WMS. Germany only. 5-minute cadence. DWD Open Data — attribution required: "
        "'Quelle: Deutscher Wetterdienst' / 'Source: Deutscher Wetterdienst (DWD)'. "
        "WN-Produkt nowcast frames are out of scope for v0.1."
    ),
    wms_endpoint_url="https://maps.dwd.de/geoserver/dwd/wms?",
    wms_layer_name=LAYER_NAME,
    tile_content_type="image/png",
    refresh_interval=300,
    attribution=ProviderAttribution(
        attribution_required=False,
        display_name="DWD",
        attribution_text="Deutscher Wetterdienst",
        text_prefix="",
        text_provider_name="Deutscher Wetterdienst",
        url="https://www.dwd.de/",
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="dwd-radolan-radar",
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
    """Fetch DWD RADOLAN radar frame index from WMS GetCapabilities.

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
        params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"},
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
        "DWD RADOLAN radar frames fetched: %d frame(s)",
        len(frames),
    )
    return result


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
