"""Esri World Imagery provider module (Phase LM, §LM-1).

Five responsibilities per PROVIDER-MANUAL §1:
  1. Outbound API call — NONE. Config-only provider (mirrors the "iframe"
     radar provider's config-slot shape, radar/iframe.py): this module never
     makes an HTTP call. The tile URL template is returned as static config;
     the browser fetches ESRI tiles directly (proxyMode: "direct" per
     §LM-1b). ESRI tile bytes never flow through this module or through
     endpoints/imagery.py's tile proxy (§LM-1c).
  2. Response parsing — n/a (no upstream response).
  3. Translation — n/a; imagery has no canonical-entity mapping and is
     DISPLAY-ONLY per the Phase LM hard rule.
  4. Capability declaration — CAPABILITY symbol, geographic_coverage="global".
  5. Error handling — n/a (nothing can fail; no network call is made).

Tile URL template + attribution pinned from the live World_Imagery MapServer
metadata (`GET .../World_Imagery/MapServer?f=json`, verified 2026-08-02):
  - `tileInfo` is present (cached tile pyramid, 256px, JPEG, zoom 0-23,
    Web Mercator) confirming the `/tile/{z}/{y}/{x}` XYZ pattern is correct
    and always live (unlike NAIP's dynamic ImageServer — see naip.py).
  - `copyrightText` (verbatim): "Source: Esri, Vantor, Earthstar Geographics,
    and the GIS User Community" — used as ATTRIBUTION here rather than an
    older/stale "Maxar" wording still seen on some third-party blog posts.

Licensing (Esri Web Site and Service Terms of Use,
https://www.esri.com/en-us/legal/terms/web-site-service): World Imagery is
free for non-commercial use; no API key required for the tile service. No
formal rate-limit is republished by Esri for this ADR — a documented ceiling
of ~2,000,000 tile requests/month for non-commercial use is the operator-
facing note carried in CAPABILITY.operator_notes (Phase LM design text);
this module does not enforce it (no proxy, no rate limiter — the browser
talks to Esri directly, same "no server-side enforcement for browser-direct
providers" pattern as radar/rainviewer.py).
"""

from __future__ import annotations

from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)

PROVIDER_ID = "esri"
DOMAIN = "imagery"

TILE_URL_TEMPLATE = (
    "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
ATTRIBUTION = "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community"

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(),  # imagery has no canonical-entity mapping (display-only)
    geographic_coverage="global",
    auth_required=(),
    default_poll_interval_seconds=0,  # tiles are browser-direct; no polling cadence
    operator_notes=(
        "Esri World Imagery — global coverage, non-commercial use only, no API "
        "key required for the tile service. Documented operator-facing ceiling: "
        "~2,000,000 tile requests/month. Browser fetches tiles directly "
        "(proxyMode: 'direct') — the API never proxies or caches ESRI tile bytes."
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
    """Return the static ESRI tile config (no upstream call — see module docstring).

    Returns:
        {"tileUrl": TILE_URL_TEMPLATE, "attribution": ATTRIBUTION}
    """
    return {"tileUrl": TILE_URL_TEMPLATE, "attribution": ATTRIBUTION}
