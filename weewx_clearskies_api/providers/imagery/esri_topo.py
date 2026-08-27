"""Esri World Topo Map provider module (Round IMAGERY-MAP, 2026-08-26).

Operator ruling 2026-08-26: the orthophoto (World_Imagery) background on the
surf height map is a failure — NAIP was flown at an extremely abnormal low
tide, so surf renders on what looks like dry land. This module adds a
MAP-STYLE tile base (streets/contours/shaded relief, not photography) as an
alternative background. It does not replace esri.py or naip.py — see
endpoints/imagery.py for selection.

Five responsibilities per PROVIDER-MANUAL §1:
  1. Outbound API call — NONE. Config-only provider, identical shape to
     providers/imagery/esri.py: this module never makes an HTTP call. The
     tile URL template is returned as static config; the browser fetches
     Esri tiles directly (proxyMode: "direct"). Tile bytes never flow
     through this module or through endpoints/imagery.py's tile proxy.
  2. Response parsing — n/a (no upstream response).
  3. Translation — n/a; imagery has no canonical-entity mapping and is
     DISPLAY-ONLY per the Phase LM hard rule.
  4. Capability declaration — CAPABILITY symbol, geographic_coverage="global".
  5. Error handling — n/a (nothing can fail; no network call is made).

Tile URL template + attribution pinned from the live World_Topo_Map
MapServer metadata (`GET .../World_Topo_Map/MapServer?f=json`, verified
2026-08-26):
  - `tileInfo` is present (cached tile pyramid, 256px, JPEG, Web Mercator
    wkid 102100/3857, singleFusedMapCache=true) confirming the
    `/tile/{z}/{y}/{x}` XYZ pattern is correct and always live — same
    cached-pyramid shape as World_Imagery (see esri.py).
  - `lods` spans level 0 through level 23 (24 LOD levels) — covers the
    dashboard heat map's 14-19 zoom range with margin.
  - `copyrightText` (verbatim): "Sources: Esri, HERE, Garmin, Intermap,
    increment P Corp., GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, IGN, Kadaster
    NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), (c)
    OpenStreetMap contributors, and the GIS User Community" — used as
    ATTRIBUTION here. This differs from World_Imagery's copyrightText; the
    two providers' attribution strings must not be conflated.

Service status note (operator-flagged, 2026-08-26): the live metadata's
`documentInfo.Subject` reads "In mature support; no longer updated." Esri
has not announced a sunset date, and the service remains fully live and
cached (verified above) — this is not a reason to avoid pinning it. If Esri
ever retires this raster service, the documented successor is Esri's vector
basemap service (a different API shape — vector tiles, not XYZ raster —
which would require mosaic-side rework, not a drop-in URL swap). Recorded
here so a future outage isn't a mystery.

Licensing (Esri Web Site and Service Terms of Use,
https://www.esri.com/en-us/legal/terms/web-site-service): World Topo Map is
free for non-commercial use; no API key required for the tile service. Same
"no server-side enforcement for browser-direct providers" posture as
esri.py — this module does not enforce a rate limit (no proxy, no rate
limiter — the browser talks to Esri directly).
"""

from __future__ import annotations

from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)

PROVIDER_ID = "map"
DOMAIN = "imagery"

TILE_URL_TEMPLATE = (
    "https://services.arcgisonline.com/arcgis/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
)
ATTRIBUTION = (
    "Sources: Esri, HERE, Garmin, Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, NRCAN, "
    "GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), "
    "(c) OpenStreetMap contributors, and the GIS User Community"
)

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),  # imagery has no canonical-entity mapping (display-only)
    geographic_coverage="global",
    auth_required=(),
    default_poll_interval_seconds=0,  # tiles are browser-direct; no polling cadence
    operator_notes=(
        "Esri World Topo Map — global coverage, non-commercial use only, no API key "
        "required for the tile service. Map-style (streets/contours/shaded relief), not "
        "orthophotography — added 2026-08-26 because NAIP's low-tide flyover made surf "
        "render on what looked like dry land. Browser fetches tiles directly "
        "(proxyMode: 'direct') — the API never proxies or caches Esri tile bytes. Service "
        "is in Esri 'mature support' (no longer updated, no announced sunset)."
    ),
    tile_url_template=TILE_URL_TEMPLATE,
    tile_content_type="image/jpeg",
    refresh_interval=None,
    attribution=ProviderAttribution(
        attribution_required=True,
        display_name="Esri",
        attribution_text=ATTRIBUTION,
        text_prefix="",
        text_provider_name="Esri",
        url="https://www.esri.com/en-us/legal/terms/web-site-service",
        logo_required=False,
    ),
)


def get_config() -> dict[str, str]:
    """Return the static Esri World Topo Map tile config (no upstream call — see module docstring).

    Returns:
        {"tileUrl": TILE_URL_TEMPLATE, "attribution": ATTRIBUTION}
    """
    return {"tileUrl": TILE_URL_TEMPLATE, "attribution": ATTRIBUTION}
