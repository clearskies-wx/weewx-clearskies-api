"""LibreWxR radar provider module.

Five responsibilities per ADR-038 §2:
  1. Outbound API call  — LibreWxR public weather-maps.json (RainViewer v2-compatible
                          wire format)
  2. Response parsing   — JSON frame index → _LibreWxRWeatherMaps wire model
  3. Translation        — radar has no canonical-entity mapping (§4.5 confirmed);
                          RadarFrame(time=utc_iso, kind="past"|"current"|"nowcast")
  4. Capability         — CAPABILITY symbol consumed at startup (rebuilt at configure()
                          time to include dynamic bounds + refresh_interval)
  5. Error handling     — canonical taxonomy (QuotaExhausted, TransientNetworkError,
                          ProviderProtocolError)

Wire format:
  LibreWxR uses the same RainViewer v2-compatible weather-maps.json structure,
  extended with:
    radar.colorSchemes — list of {id: int, name: str} available color schemes
  The API base URL is configurable (default: https://api.librewxr.net) to support
  self-hosted deployments.

Frame-kind mapping (same rule as RainViewer — per rainviewer.py):
  EXACTLY ONE entry in radar.past with max(time) → "current"
  All other radar.past entries                   → "past"
  radar.nowcast[i]                               → "nowcast"

Tile delivery:
  LibreWxR tiles are proxied directly by Caddy (CAPABILITY.caddy_prefix="/librewxr").
  The API never handles tile bytes for LibreWxR — NO get_tile() method is defined.

Cache (ADR-017):
  Key: SHA-256 of (provider_id, "frames"). No lat/lon component — frame index is
  global per provider. TTL: 60 s (same as rainviewer).
  Serialisation: model_dump(mode="json") → dict → cache; reconstruct via
  model_validate(cached_dict).

configure() / CAPABILITY lifecycle:
  CAPABILITY is a module-level variable (not a constant) initialized with defaults.
  configure() is called at startup from __main__.py with values from RadarSettings.
  Each configure() call rebuilds CAPABILITY via _build_capability() so module.CAPABILITY
  always reflects the current dynamic fields (bounds, refresh_interval, base_url).
  dispatch.py / capabilities.py read module.CAPABILITY — this works transparently.

ruff: noqa: N815  (field names match RainViewer/LibreWxR JSON camelCase)
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

PROVIDER_ID = "librewxr"
DOMAIN = "radar"
FRAMES_PATH = "/public/weather-maps.json"
# TTL deviation note: same 60s as rainviewer (see rainviewer.py for rationale).
_CACHE_TTL = 60
_API_VERSION = "0.1.0"

ATTRIBUTION = "LibreWxR (https://librewxr.net/) — Data: CC-BY-4.0"

# ---------------------------------------------------------------------------
# Module-level configurable variables (set at startup via configure())
# ---------------------------------------------------------------------------

_base_url: str = "https://api.librewxr.net"
_bounds: dict[str, float] | None = None   # {south, west, north, east} or None
_refresh_interval: int = 600              # seconds between dashboard re-fetches


# ---------------------------------------------------------------------------
# Capability builder + module-level CAPABILITY symbol
# ---------------------------------------------------------------------------


def _build_capability() -> ProviderCapability:
    """Build a ProviderCapability using current dynamic field values.

    Called once at module load (with defaults) and again each time configure()
    is called.  The module-level CAPABILITY variable is reassigned so that
    dispatch.py / capabilities.py always read the current state.
    """
    return ProviderCapability(
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
        supplied_canonical_fields=(),   # radar has no canonical-entity mapping (§4.5)
        geographic_coverage="global",
        auth_required=(),
        default_poll_interval_seconds=_CACHE_TTL,
        operator_notes=(
            "Optional radar upgrade. Zoom 12, 13 color schemes, WebP, "
            "60-min nowcast, satellite, weather alerts. "
            "Public API (api.librewxr.net) — no SLA. Self-host recommended for production."
        ),
        tile_url_template="/librewxr/{path}/{size}/{z}/{x}/{y}/{color}/{options}.webp",
        tile_content_type="image/webp",
        # LibreWxR-specific fields (new on ProviderCapability):
        caddy_prefix="/librewxr",
        alert_url="/librewxr/v2/alerts",
        bounds=_bounds,
        refresh_interval=_refresh_interval,
        nowcast_available=True,
        alerts_available=True,
        satellite_available=True,
        satellite_tile_url_template="/librewxr/{path}/{size}/{z}/{x}/{y}/0/0_0.webp",
        attribution=ProviderAttribution(
            attribution_required=True,
            display_name="LibreWxR",
            attribution_text="LibreWxR — Data: CC-BY-4.0",
            url="https://librewxr.net/",
        ),
    )


# Module-level CAPABILITY — initialized with defaults; rebuilt by configure().
CAPABILITY: ProviderCapability = _build_capability()


# ---------------------------------------------------------------------------
# configure() — called at startup from __main__.py with RadarSettings values
# ---------------------------------------------------------------------------


def configure(
    *,
    endpoint: str,
    bounds: str | None,
    refresh_interval: int,
) -> None:
    """Configure LibreWxR provider with operator settings.

    Called from __main__.py at startup after loading RadarSettings.  Updates
    module-level variables and rebuilds CAPABILITY so that the capability
    registry reflects the configured state.

    Args:
        endpoint: LibreWxR API base URL (e.g. "https://api.librewxr.net" or
            a self-hosted URL).  Trailing slashes are stripped.
        bounds: Optional bounding box as "south,west,north,east" CSV string.
            When set, the dashboard may use it to constrain radar tile fetches.
            None or empty string → no bounds constraint.
        refresh_interval: Seconds between dashboard re-fetches of the frame
            index.  Exposed via CAPABILITY.refresh_interval so the dashboard
            can use it without hardcoding.
    """
    global _base_url, _bounds, _refresh_interval, CAPABILITY  # noqa: PLW0603

    _base_url = endpoint.rstrip("/")
    _refresh_interval = refresh_interval

    if bounds and bounds.strip():
        parts = [float(x.strip()) for x in bounds.split(",")]
        if len(parts) == 4:
            _bounds = {
                "south": parts[0],
                "west": parts[1],
                "north": parts[2],
                "east": parts[3],
            }
        else:
            logger.warning(
                "LibreWxR bounds %r has %d parts (expected 4); ignoring bounds config",
                bounds,
                len(parts),
            )
            _bounds = None
    else:
        _bounds = None

    CAPABILITY = _build_capability()

    logger.info(
        "LibreWxR configured: endpoint=%r bounds=%r refresh_interval=%d",
        _base_url,
        _bounds,
        _refresh_interval,
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
# Source: LibreWxR API (RainViewer v2-compatible wire format)
# ---------------------------------------------------------------------------


class _LibreWxRFrameEntry(BaseModel):
    """One entry in radar.past or radar.nowcast array."""

    model_config = ConfigDict(extra="ignore")

    time: int   # Unix epoch seconds
    path: str   # tile path prefix, e.g. "/v2/radar/1778540400"


class _LibreWxRColorScheme(BaseModel):
    """One entry in radar.colorSchemes array."""

    model_config = ConfigDict(extra="ignore")

    id: int      # color scheme id (numeric)
    name: str    # human-readable name


class _LibreWxRRadar(BaseModel):
    """radar sub-object in weather-maps.json."""

    model_config = ConfigDict(extra="ignore")

    past: list[_LibreWxRFrameEntry] = []
    nowcast: list[_LibreWxRFrameEntry] = []
    colorSchemes: list[_LibreWxRColorScheme] = []


class _LibreWxRSatellite(BaseModel):
    """satellite sub-object in weather-maps.json."""

    model_config = ConfigDict(extra="ignore")

    infrared: list[_LibreWxRFrameEntry] = []


class _LibreWxRWeatherMaps(BaseModel):
    """Top-level weather-maps.json response envelope.

    extra="ignore" so new top-level keys (alerts, etc.) don't break us.
    """

    model_config = ConfigDict(extra="ignore")

    version: str
    generated: int   # Unix epoch seconds
    host: str        # tile host
    radar: _LibreWxRRadar
    satellite: _LibreWxRSatellite = _LibreWxRSatellite()


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
# Cache key (ADR-017) — no station component for frame index
# ---------------------------------------------------------------------------


def _cache_key() -> str:
    payload = json.dumps({"provider_id": PROVIDER_ID, "kind": "frames"}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Wire → canonical translation
# ---------------------------------------------------------------------------


def _to_canonical_frames(parsed: _LibreWxRWeatherMaps) -> list[RadarFrame]:
    """Map LibreWxR wire model to a list of canonical RadarFrame instances.

    Frame-kind rule (same as RainViewer per rainviewer.py):
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


def _to_satellite_frames(parsed: _LibreWxRWeatherMaps) -> list[RadarFrame]:
    """Map LibreWxR satellite infrared entries to canonical RadarFrame instances.

    All satellite IR frames are kind="past" — satellite imagery is observational,
    not forecast.
    """
    frames: list[RadarFrame] = []
    for entry in parsed.satellite.infrared:
        frames.append(
            RadarFrame(
                time=epoch_to_utc_iso8601(entry.time, provider_id=PROVIDER_ID, domain=DOMAIN),
                kind="past",
                path=entry.path,
            )
        )
    return frames


# ---------------------------------------------------------------------------
# Cache serialisation helpers
# ---------------------------------------------------------------------------


def _to_cacheable(frames_list: RadarFrameList) -> dict:  # type: ignore[type-arg]
    """Serialise RadarFrameList to a JSON-safe dict for cache storage."""
    return frames_list.model_dump(mode="json")


def _from_cached(cached: dict) -> RadarFrameList:  # type: ignore[type-arg]
    """Reconstruct RadarFrameList from a cached dict."""
    return RadarFrameList.model_validate(cached)


# ---------------------------------------------------------------------------
# Public frame-index entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def get_frames() -> RadarFrameList:
    """Fetch the LibreWxR radar frame index and return canonical RadarFrameList.

    Cache stores post-normalisation dict (JSON-serialisable per ADR-017).
    On cache hit the dict is reconstructed into a RadarFrameList model.

    Returns:
        RadarFrameList with providerId, frames, attribution, tileHost,
        and colorSchemes (LibreWxR-specific).

    Raises:
        QuotaExhausted: LibreWxR returned 429.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed (schema change).
    """
    cache = get_cache()
    key = _cache_key()
    hit = cache.get(key)
    if hit is not None:
        return _from_cached(hit)

    _rate_limiter.acquire()

    response = _get_http_client().get(f"{_base_url}{FRAMES_PATH}")

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
    satellite_frames = _to_satellite_frames(parsed)

    # Build colorSchemes list from wire model for dashboard consumption.
    color_schemes = [
        {"id": cs.id, "name": cs.name}
        for cs in parsed.radar.colorSchemes
    ]

    result = RadarFrameList(
        providerId=PROVIDER_ID,
        frames=frames,
        attribution=ATTRIBUTION,
        tileHost=parsed.host,
        colorSchemes=color_schemes if color_schemes else None,
        satelliteFrames=satellite_frames if satellite_frames else None,
    )

    cache.set(key, _to_cacheable(result), ttl_seconds=_CACHE_TTL)

    logger.info(
        "LibreWxR radar frames fetched: %d frame(s) (%d past, %d nowcast) "
        "%d color scheme(s) %d satellite frame(s)",
        len(frames),
        sum(1 for f in frames if f.kind in ("past", "current")),
        sum(1 for f in frames if f.kind == "nowcast"),
        len(color_schemes),
        len(satellite_frames),
    )
    return result


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
