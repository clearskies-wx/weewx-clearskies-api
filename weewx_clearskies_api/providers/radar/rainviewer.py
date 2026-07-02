"""RainViewer radar provider module (ADR-015, ADR-038, 3b-14).

Five responsibilities per ADR-038 §2:
  1. Outbound API call  — RainViewer public weather-maps.json (no auth)
  2. Response parsing   — JSON frame index → _RainViewerWeatherMaps wire model
  3. Translation        — radar has no canonical-entity mapping (§4.5 confirmed);
                          RadarFrame(time=utc_iso, kind="past"|"current"|"nowcast")
  4. Capability         — CAPABILITY symbol consumed at startup
  5. Error handling     — canonical taxonomy (QuotaExhausted, TransientNetworkError,
                          ProviderProtocolError)

Frame-kind mapping (per docs/reference/api-docs/rainviewer.md, lead-direct
fix 2026-05-11 after brief rule-bug surfaced by test-author):
  EXACTLY ONE entry in radar.past with max(time) → "current"
  All other radar.past entries                   → "past"
  radar.nowcast[i]                               → "nowcast"

  The original brief said "time >= generated → current" but live RainViewer
  responses have all past[].time strictly BEFORE generated (the latest past
  is ~5 min before the JSON was generated), so the literal rule produces
  zero "current" frames. The api-docs parenthetical ("the latest past frame
  is current") is the correct semantic.

Cache (ADR-017):
  Key: SHA-256 of (provider_id, "frames").  No lat/lon component — frame index
  is global per provider.  TTL: 60 s (brief lead call 5).
  Serialisation: model_dump(mode="json") → dict → Redis; reconstruct via
  model_validate(cached_dict).  Same pattern as EarthquakeRecord caching in 3b-13.

Tile URL template (CAPABILITY.tile_url_template):
  "{host}{path}/{size}/{z}/{x}/{y}/{color}/{options}.png"
  host + path are resolved per-request from the frame-index JSON.
  The CAPABILITY template shows the placeholder shape; concrete URLs are
  composed by the dashboard per the api-docs/rainviewer.md instructions.

ruff: noqa: N815  (field names match RainViewer JSON camelCase)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, ConfigDict, ValidationError

from weewx_clearskies_api.models.responses import RadarFrame, RadarFrameList
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.datetime_utils import epoch_to_utc_iso8601
from weewx_clearskies_api.providers._common.errors import ProviderProtocolError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "rainviewer"
DOMAIN = "radar"
BASE_URL = "https://api.rainviewer.com"
FRAMES_PATH = "/public/weather-maps.json"
# TTL deviation: ADR-017's default for radar frame metadata is 5 min;
# brief lead-call 5 set 60s to match earthquakes precedent (3b-13). The
# deviation is conscious — radar frame indexes roll forward every 5-10 min
# upstream, but cache hit-rate at 60s is acceptable for keyless polite-use.
# ADR-017 amendment deferred to a future round (3b-14 auditor F3).
_CACHE_TTL = 60  # 60 s — see ADR-017 deviation note above
_API_VERSION = "0.1.0"

ATTRIBUTION = "RainViewer (https://www.rainviewer.com/)"

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),  # radar has no canonical-entity mapping (§4.5)
    geographic_coverage="global",  # global mosaic; default fallback per ADR-015
    auth_required=(),
    default_poll_interval_seconds=_CACHE_TTL,
    operator_notes=(
        "Free for personal or educational use; attribution required: "
        "https://www.rainviewer.com/ on the consuming site. "
        "Global mosaic composite. Default fallback per ADR-015 for regions "
        "without a native provider."
    ),
    tile_url_template="{host}{path}/{size}/{z}/{x}/{y}/{color}/{options}.png",
    tile_content_type="image/png",
    refresh_interval=300,
    attribution=ProviderAttribution(
        attribution_required=True,
        display_name="RainViewer",
        attribution_text="RainViewer",
        url="https://www.rainviewer.com/",
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3) — polite-use guard (no documented limit)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="rainviewer-radar",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Source: docs/reference/api-docs/rainviewer.md + live capture 2026-05-11
# ---------------------------------------------------------------------------


class _RainViewerFrameEntry(BaseModel):
    """One entry in radar.past or radar.nowcast array."""

    model_config = ConfigDict(extra="ignore")

    time: int   # Unix epoch seconds
    path: str   # tile path prefix, e.g. "/v2/radar/1778540400"


class _RainViewerRadar(BaseModel):
    """radar sub-object in weather-maps.json."""

    model_config = ConfigDict(extra="ignore")

    past: list[_RainViewerFrameEntry] = []
    nowcast: list[_RainViewerFrameEntry] = []


class _RainViewerWeatherMaps(BaseModel):
    """Top-level weather-maps.json response envelope.

    extra="ignore" so new top-level keys (satellite, etc.) don't break us.
    """

    model_config = ConfigDict(extra="ignore")

    version: str
    generated: int   # Unix epoch seconds — reference time for kind mapping
    host: str        # tile host, e.g. "https://tilecache.rainviewer.com"
    radar: _RainViewerRadar


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
# Cache key (ADR-017) — no station component for frame index (brief lead call 5)
# ---------------------------------------------------------------------------


def _cache_key() -> str:
    payload = json.dumps({"provider_id": PROVIDER_ID, "kind": "frames"}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Wire → canonical translation
# ---------------------------------------------------------------------------


def _to_canonical_frames(parsed: _RainViewerWeatherMaps) -> list[RadarFrame]:
    """Map RainViewer wire model to a list of canonical RadarFrame instances.

    Frame-kind rule (docs/reference/api-docs/rainviewer.md):
      The single past entry with max(time) → "current"
      All other past entries               → "past"
      nowcast entries                      → "nowcast"

    There is always exactly ONE "current" frame in a non-empty past list.

    Each frame's `path` is set to the wire `path` so the dashboard can
    combine it with the response-level `tileHost` and the CAPABILITY's
    tile_url_template to construct the per-frame tile URL (3b-14 F2).
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
# Cache serialisation helpers
# ---------------------------------------------------------------------------


def _to_cacheable(frames_list: RadarFrameList) -> dict:  # type: ignore[type-arg]
    """Serialise RadarFrameList to a JSON-safe dict for Redis storage."""
    return frames_list.model_dump(mode="json")


def _from_cached(cached: dict) -> RadarFrameList:  # type: ignore[type-arg]
    """Reconstruct RadarFrameList from a cached dict."""
    return RadarFrameList.model_validate(cached)


# ---------------------------------------------------------------------------
# Public frame-index entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def get_frames() -> RadarFrameList:
    """Fetch the RainViewer radar frame index and return canonical RadarFrameList.

    Cache stores post-normalisation dict (JSON-serialisable per ADR-017).
    On cache hit the dict is reconstructed into a RadarFrameList model.

    Returns:
        RadarFrameList with providerId, frames, and attribution.

    Raises:
        QuotaExhausted: RainViewer returned 429.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed (RainViewer schema change).
    """
    cache = get_cache()
    key = _cache_key()
    hit = cache.get(key)
    if hit is not None:
        return _from_cached(hit)

    _rate_limiter.acquire()

    response = _get_http_client().get(f"{BASE_URL}{FRAMES_PATH}")

    try:
        raw_json = response.json()
        parsed = _RainViewerWeatherMaps.model_validate(raw_json)
    except (ValidationError, ValueError) as exc:
        logger.error(
            "RainViewer response validation failed: %s. Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"RainViewer response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    frames = _to_canonical_frames(parsed)
    result = RadarFrameList(
        providerId=PROVIDER_ID,
        frames=frames,
        attribution=ATTRIBUTION,
        tileHost=parsed.host,  # 3b-14 F2 — dashboard combines with frame.path + CAPABILITY.tile_url_template
    )

    cache.set(key, _to_cacheable(result), ttl_seconds=_CACHE_TTL)

    logger.info(
        "RainViewer radar frames fetched: %d frame(s) (%d past, %d nowcast)",
        len(frames),
        sum(1 for f in frames if f.kind in ("past", "current")),
        sum(1 for f in frames if f.kind == "nowcast"),
    )
    return result


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
