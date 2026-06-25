"""Unified NOAA radar provider module (ADR-015, ADR-038, API-MANUAL.md §12).

Replaces the separate ``iem_nexrad`` and ``noaa_mrms`` modules with a single
provider that declares multiple data layers via the ``layers`` field on
``ProviderCapability`` (API-MANUAL.md §12, PROVIDER-MANUAL.md §unified-NOAA).

Fourteen layers declared (per API-MANUAL.md §12 layer table):
  Radar (time-enabled, browser-direct):
    nexrad         — IEM NEXRAD (CONUS), WMS-T
    mrms           — NOAA MRMS (US all territories), WMS-T
  Satellite (time-enabled, browser-direct):
    goes_visible   — GOES Visible via nowCOAST WMS
    goes_longwave  — GOES Longwave IR via nowCOAST WMS
    goes_water_vapor — GOES Water Vapor via nowCOAST WMS
    goes_snow_ice  — GOES Snow/Ice via nowCOAST WMS
  SPC overlays (NOT time-enabled, browser-direct GeoJSON):
    spc_day1_cat   — SPC Day 1 Categorical
    spc_day1_tornado — SPC Day 1 Tornado
    spc_day1_hail  — SPC Day 1 Hail
    spc_day1_wind  — SPC Day 1 Wind
    spc_mesoscale  — SPC Mesoscale Discussions
    spc_fire       — SPC Fire Weather
  Alert polygons (NOT time-enabled, uses existing /api/v1/alerts):
    alerts         — Alert Polygons

Five responsibilities per ADR-038 §2:
  1. Outbound API call  — WMS GetCapabilities (no auth)
  2. Response parsing   — XML TIME dimension via parse_wms_time_dimension()
  3. Translation        — timestamps → RadarFrame(kind="past"|"current")
  4. Capability         — CAPABILITY symbol consumed at startup
  5. Error handling     — canonical taxonomy (ProviderProtocolError for
                          non-time-enabled layer dispatch; propagate others)

Frame-kind mapping (brief lead call 4):
  Latest timestamp (max) → "current"; all others → "past".
  No nowcast frames for WMS-T providers.

Cache (ADR-017): TTL 60 s per sub-layer; key = SHA-256(PROVIDER_ID, layer_id).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from weewx_clearskies_api.models.responses import RadarFrame, RadarFrameList
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import LayerDeclaration, ProviderCapability
from weewx_clearskies_api.providers._common.errors import ProviderProtocolError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter
from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "noaa"
DOMAIN = "radar"
_CACHE_TTL = 60  # seconds, per sub-layer (ADR-017 deviation: brief set 60s)
_API_VERSION = "0.1.0"

# IEM NEXRAD (CONUS radar)
_IEM_NEXRAD_URL = "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0r-t.cgi"
_IEM_NEXRAD_LAYER = "nexrad-n0r-wmst"

# NOAA MRMS (AK/HI/PR/Guam radar)
_MRMS_URL = (
    "https://mapservices.weather.noaa.gov/eventdriven/services/radar/"
    "radar_base_reflectivity_time/ImageServer/WMSServer"
)
_MRMS_LAYER = "radar_base_reflectivity_time"

# nowCOAST satellite WMS
_NOWCOAST_SAT_URL = "https://nowcoast.noaa.gov/geoserver/satellite/wms"

# SPC overlays (ArcGIS REST/GeoJSON)
_SPC_OUTLOOKS_URL = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer"
_SPC_MESOSCALE_URL = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/spc_mesoscale_discussion/MapServer"
_SPC_FIRE_URL = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_firewx/MapServer"

ATTRIBUTION = "NEXRAD imagery courtesy of Iowa Environmental Mesonet. MRMS data courtesy of NOAA."

# ---------------------------------------------------------------------------
# Layer declarations (API-MANUAL.md §12)
# ---------------------------------------------------------------------------

_LAYERS: tuple[LayerDeclaration, ...] = (
    # --- Radar sub-layers (time-enabled, browser-direct) ---
    LayerDeclaration(
        layer_id="nexrad",
        layer_name="NEXRAD Radar",
        layer_type="radar",
        wms_endpoint_url=_IEM_NEXRAD_URL,
        wms_layer_name=_IEM_NEXRAD_LAYER,
        time_enabled=True,
        geographic_coverage="CONUS",
        default_enabled=True,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="mrms",
        layer_name="MRMS Radar",
        layer_type="radar",
        wms_endpoint_url=_MRMS_URL,
        wms_layer_name=_MRMS_LAYER,
        time_enabled=True,
        geographic_coverage="US all territories",
        default_enabled=True,
        browser_direct=True,
    ),
    # --- Satellite layers (time-enabled, browser-direct) ---
    LayerDeclaration(
        layer_id="goes_visible",
        layer_name="GOES Visible",
        layer_type="satellite",
        wms_endpoint_url=_NOWCOAST_SAT_URL,
        wms_layer_name="goes_visible_imagery",
        time_enabled=True,
        geographic_coverage="US region",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="goes_longwave",
        layer_name="GOES Longwave IR",
        layer_type="satellite",
        wms_endpoint_url=_NOWCOAST_SAT_URL,
        wms_layer_name="goes_longwave_imagery",
        time_enabled=True,
        geographic_coverage="US region",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="goes_water_vapor",
        layer_name="GOES Water Vapor",
        layer_type="satellite",
        wms_endpoint_url=_NOWCOAST_SAT_URL,
        wms_layer_name="goes_water_vapor_imagery",
        time_enabled=True,
        geographic_coverage="US region",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="goes_snow_ice",
        layer_name="GOES Snow/Ice",
        layer_type="satellite",
        wms_endpoint_url=_NOWCOAST_SAT_URL,
        wms_layer_name="goes_snow_ice_imagery",
        time_enabled=True,
        geographic_coverage="US region",
        default_enabled=False,
        browser_direct=True,
    ),
    # --- SPC overlay layers (NOT time-enabled, browser-direct GeoJSON) ---
    LayerDeclaration(
        layer_id="spc_day1_cat",
        layer_name="SPC Day 1 Categorical",
        layer_type="overlay",
        wms_endpoint_url=f"{_SPC_OUTLOOKS_URL}/0/query",
        time_enabled=False,
        geographic_coverage="CONUS",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="spc_day1_tornado",
        layer_name="SPC Day 1 Tornado",
        layer_type="overlay",
        wms_endpoint_url=f"{_SPC_OUTLOOKS_URL}/2/query",
        time_enabled=False,
        geographic_coverage="CONUS",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="spc_day1_hail",
        layer_name="SPC Day 1 Hail",
        layer_type="overlay",
        wms_endpoint_url=f"{_SPC_OUTLOOKS_URL}/4/query",
        time_enabled=False,
        geographic_coverage="CONUS",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="spc_day1_wind",
        layer_name="SPC Day 1 Wind",
        layer_type="overlay",
        wms_endpoint_url=f"{_SPC_OUTLOOKS_URL}/6/query",
        time_enabled=False,
        geographic_coverage="CONUS",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="spc_mesoscale",
        layer_name="SPC Mesoscale Discussions",
        layer_type="overlay",
        wms_endpoint_url=f"{_SPC_MESOSCALE_URL}/0/query",
        time_enabled=False,
        geographic_coverage="CONUS",
        default_enabled=False,
        browser_direct=True,
    ),
    LayerDeclaration(
        layer_id="spc_fire",
        layer_name="SPC Fire Weather",
        layer_type="overlay",
        wms_endpoint_url=f"{_SPC_FIRE_URL}/0/query",
        time_enabled=False,
        geographic_coverage="CONUS",
        default_enabled=False,
        browser_direct=True,
    ),
    # --- Alert polygons layer (uses existing /api/v1/alerts, not browser-direct WMS) ---
    LayerDeclaration(
        layer_id="alerts",
        layer_name="Alert Polygons",
        layer_type="alerts",
        time_enabled=False,
        geographic_coverage="US",
        default_enabled=False,
        browser_direct=False,  # uses existing /api/v1/alerts endpoint
    ),
)

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

_IEM_TMS_TEMPLATE = (
    "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/{path}/{z}/{x}/{y}.png"
)

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),
    geographic_coverage="us",
    auth_required=(),
    default_poll_interval_seconds=60,
    operator_notes=(
        "Unified NOAA provider — IEM NEXRAD (CONUS) + MRMS (AK/HI/PR/Guam) radar, "
        "GOES satellite (5 bands), SPC severe weather overlays, alert polygons. "
        "All free government endpoints, no API key required."
    ),
    tile_url_template=_IEM_TMS_TEMPLATE,
    tile_content_type="image/png",
    layers=_LAYERS,
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="noaa-radar",
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
# Layer lookup helper
# ---------------------------------------------------------------------------

# Index _LAYERS by layer_id for O(1) lookup.
_LAYER_INDEX: dict[str, LayerDeclaration] = {layer.layer_id: layer for layer in _LAYERS}

# Canonical layer_id for default (no layer argument) is "nexrad".
_DEFAULT_LAYER_ID = "nexrad"


def _resolve_layer(layer: str | None) -> LayerDeclaration:
    """Resolve an optional layer argument to a LayerDeclaration.

    Args:
        layer: layer_id string, or None (treated as "nexrad").

    Returns:
        The matching LayerDeclaration.

    Raises:
        ProviderProtocolError: layer_id is not recognised.
    """
    layer_id = layer if layer is not None else _DEFAULT_LAYER_ID
    decl = _LAYER_INDEX.get(layer_id)
    if decl is None:
        known = ", ".join(sorted(_LAYER_INDEX))
        raise ProviderProtocolError(
            f"Unknown layer {layer_id!r} for provider {PROVIDER_ID!r}. "
            f"Known layer IDs: {known}.",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )
    return decl


# ---------------------------------------------------------------------------
# Cache key (per sub-layer)
# ---------------------------------------------------------------------------


def _cache_key(layer_id: str) -> str:
    """SHA-256 cache key incorporating provider_id and layer_id."""
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "layer_id": layer_id}, sort_keys=True
    )
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
        timestamps: ISO-8601 UTC timestamp strings (order from WMS is preserved;
            latest is determined by max()).

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


def _get_nexrad_tms_frames() -> RadarFrameList:
    """Generate NEXRAD TMS tile frame list.

    IEM serves NEXRAD as pre-rendered TMS tiles at fixed 5-minute offsets
    from the current time — NOT WMS-T.  11 frames covering 50 minutes.
    URL: ``/cache/tile.py/1.0.0/nexrad-n0q-900913-mXXm/{z}/{x}/{y}.png``
    Reference: https://mesonet.agron.iastate.edu/ogc/
    """
    offsets = [50, 45, 40, 35, 30, 25, 20, 15, 10, 5, 0]
    now = datetime.now(tz=UTC)
    # Snap to 5-minute boundary
    now = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)

    frames: list[RadarFrame] = []
    for offset_min in offsets:
        frame_time = now - timedelta(minutes=offset_min)
        if offset_min == 0:
            path = "nexrad-n0q-900913"
            kind = "current"
        else:
            path = f"nexrad-n0q-900913-m{offset_min:02d}m"
            kind = "past"
        frames.append(RadarFrame(
            time=frame_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            path=path,
            kind=kind,
        ))

    return RadarFrameList(
        providerId=PROVIDER_ID,
        frames=frames,
        attribution=ATTRIBUTION,
    )


def get_frames(*, layer: str | None = None) -> RadarFrameList:
    """Fetch radar or satellite frame index from WMS GetCapabilities.

    Dispatches to the appropriate WMS endpoint based on the ``layer`` argument.
    Only time-enabled layers (radar and satellite) are supported; SPC overlays
    and alert polygon layers are current-snapshot only and raise an error.

    Args:
        layer: Sub-layer identifier (e.g. ``"nexrad"``, ``"mrms"``,
            ``"goes_visible"``).  ``None`` (default) is equivalent to
            ``"nexrad"`` — fetches from IEM NEXRAD (primary, CONUS).

    Returns:
        RadarFrameList with providerId, frames, and attribution.

    Raises:
        ProviderProtocolError: Unknown layer_id; or layer is not time-enabled
            (SPC/alert layers do not have frame metadata).
        QuotaExhausted: Server returned 429.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
    """
    decl = _resolve_layer(layer)

    if not decl.time_enabled:
        raise ProviderProtocolError(
            f"Layer {decl.layer_id!r} ({decl.layer_name!r}) does not support "
            "frame metadata — it is a current-snapshot overlay, not a time-animated "
            "layer. Use the declared wms_endpoint_url directly to fetch GeoJSON.",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    # NEXRAD uses IEM TMS tiles (pre-rendered, CDN-cached) — no WMS GetCapabilities.
    if decl.layer_id == _DEFAULT_LAYER_ID:
        return _get_nexrad_tms_frames()

    # Per-layer cache check (WMS-T layers only — MRMS, satellite).
    cache = get_cache()
    key = _cache_key(decl.layer_id)
    hit = cache.get(key)
    if hit is not None:
        return _from_cached(hit)

    _rate_limiter.acquire()

    # wms_endpoint_url is guaranteed non-None for all time-enabled layers.
    endpoint_url = decl.wms_endpoint_url
    wms_layer_name = decl.wms_layer_name

    response = _get_http_client().get(
        endpoint_url,  # type: ignore[arg-type]
        params={
            "SERVICE": "WMS",
            "VERSION": "1.3.0",
            "REQUEST": "GetCapabilities",
        },
    )
    xml_bytes = response.content

    # parse_wms_time_dimension raises ProviderProtocolError on any XML/layer/
    # dimension error — let it propagate (do not re-wrap; coding.md §3).
    timestamps = parse_wms_time_dimension(
        xml_bytes,
        layer=wms_layer_name,  # type: ignore[arg-type]
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
    )

    frames = _to_canonical_frames(timestamps)
    result = RadarFrameList(
        providerId=PROVIDER_ID,
        frames=frames,
        attribution=ATTRIBUTION,
    )

    cache.set(key, _to_cacheable(result), ttl_seconds=_CACHE_TTL)

    logger.info(
        "NOAA radar provider frames fetched: layer=%r frames=%d",
        decl.layer_id,
        len(frames),
    )
    return result


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton.  Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
