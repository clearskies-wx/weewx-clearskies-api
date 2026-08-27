"""Provider module dispatch.

Maps (domain, provider_id) → the provider module's CAPABILITY + fetch entrypoint.
Phase-2 simple: explicit dict, NOT entry-points (per ADR-038 §Internal contract —
no runtime plugin loading; outside contributors PR into the bundled set).

Adding a new provider = importing the new module and adding one row here.
Aeris alerts wired in 3b round 7; OWM alerts wired in 3b round 8 (third and FINAL
day-1 alerts provider per ADR-016).
Forecast domain: four rows (one per provider per ADR-007 day-1 set; the
  fifth day-1 forecast provider was removed Phase 0 T0.1 2026-07-05 for
  insufficient data quality).
  Wired: openmeteo (3b-2), nws (3b-3), aeris (3b-4), openweathermap (3b-5).
AQI domain: openmeteo (3b-9), aeris (3b-10), iqair (3b-12). openweathermap (3b-11) and
  openaq removed from AQI — OWM AQI returns SILAM model predictions, not observed PM
  data (Phase 2 API removals); openaq was never wired into dispatch.
Earthquakes domain: usgs, geonet, emsc, renass (3b-13 — domain opener; all keyless per ADR-040).
Radar domain:
  Keyless (3b-14): rainviewer, iem_nexrad, noaa_mrms, msc_geomet, dwd_radolan.
  Keyed (3b-15): aeris (deprecated from radar — module stays on disk, no longer in valid_providers),
    openweathermap.
  Keyless + Caddy-proxied tile (T1.2): librewxr (configurable endpoint; no get_tile()).
  mapbox_jma deferred per ADR-015 2026-05-11 amendment (raster-array shape;
    requires Mapbox GL JS, incompatible with Leaflet).

Imagery domain (Phase LM, §LM-1): naip, esri. Unlike every other domain,
  provider selection is a per-request CONUS-bbox test (endpoints/imagery.py),
  not a single operator-wide pick — but dispatch.py itself is just a flat
  (domain, provider_id) -> module lookup with no single-pick assumption
  baked in, so both modules register normally; the per-request selection
  logic lives entirely in the endpoint, same as every other dispatch caller.
  esri_topo ("map" provider id) added IMAGERY-MAP round 2026-08-26 — same
  config-only, browser-direct shape as esri; "auto" never selects it, only
  an explicit operator override does.

Marine domains (buoy/marine/tides/ocean/wind/nearshore) REMOVED (Phase 6,
C-41/C-42, 2026-07-25). All marine provider logic — including the ndbc,
wavewatch/nws_marine/nws_srf, coops, ofs/erddap_ocean, hrrr/gfs, and swan
modules previously registered here — moved to the weewx-clearskies-marine
service per the "marine service is an add-on reached only through the API"
invariant (ARCHITECTURE.md). The API no longer imports or dispatches any
marine provider module.
"""

from __future__ import annotations

from types import ModuleType

from weewx_clearskies_api.providers.alerts import aeris as alerts_aeris
from weewx_clearskies_api.providers.alerts import nws as alerts_nws
from weewx_clearskies_api.providers.alerts import openweathermap as alerts_openweathermap
from weewx_clearskies_api.providers.aqi import aeris as aqi_aeris
from weewx_clearskies_api.providers.aqi import iqair as aqi_iqair
from weewx_clearskies_api.providers.aqi import openmeteo as aqi_openmeteo
from weewx_clearskies_api.providers.earthquakes import emsc as earthquakes_emsc
from weewx_clearskies_api.providers.earthquakes import geonet as earthquakes_geonet
from weewx_clearskies_api.providers.earthquakes import renass as earthquakes_renass
from weewx_clearskies_api.providers.earthquakes import usgs as earthquakes_usgs
from weewx_clearskies_api.providers.forecast import aeris as forecast_aeris
from weewx_clearskies_api.providers.forecast import nws as forecast_nws
from weewx_clearskies_api.providers.forecast import openmeteo as forecast_openmeteo
from weewx_clearskies_api.providers.forecast import openweathermap as forecast_openweathermap
from weewx_clearskies_api.providers.imagery import esri as imagery_esri
from weewx_clearskies_api.providers.imagery import esri_topo as imagery_esri_topo
from weewx_clearskies_api.providers.imagery import naip as imagery_naip
from weewx_clearskies_api.providers.radar import aeris as radar_aeris
from weewx_clearskies_api.providers.radar import dwd_radolan as radar_dwd_radolan
from weewx_clearskies_api.providers.radar import iem_nexrad as radar_iem_nexrad
from weewx_clearskies_api.providers.radar import iframe as radar_iframe
from weewx_clearskies_api.providers.radar import librewxr as radar_librewxr
from weewx_clearskies_api.providers.radar import msc_geomet as radar_msc_geomet
from weewx_clearskies_api.providers.radar import noaa_mrms as radar_noaa_mrms
from weewx_clearskies_api.providers.radar import openweathermap as radar_openweathermap
from weewx_clearskies_api.providers.radar import rainviewer as radar_rainviewer

PROVIDER_MODULES: dict[tuple[str, str], ModuleType] = {
    ("alerts", "aeris"): alerts_aeris,
    ("alerts", "nws"): alerts_nws,
    ("alerts", "openweathermap"): alerts_openweathermap,
    ("aqi", "aeris"): aqi_aeris,
    ("aqi", "iqair"): aqi_iqair,
    ("aqi", "openmeteo"): aqi_openmeteo,
    ("earthquakes", "emsc"): earthquakes_emsc,
    ("earthquakes", "geonet"): earthquakes_geonet,
    ("earthquakes", "renass"): earthquakes_renass,
    ("earthquakes", "usgs"): earthquakes_usgs,
    ("forecast", "openmeteo"): forecast_openmeteo,
    ("forecast", "nws"): forecast_nws,
    ("forecast", "aeris"): forecast_aeris,
    ("forecast", "openweathermap"): forecast_openweathermap,
    ("imagery", "esri"): imagery_esri,    # config-only, browser-direct — Phase LM §LM-1
    ("imagery", "map"): imagery_esri_topo,  # config-only, browser-direct — IMAGERY-MAP 2026-08-26
    ("imagery", "naip"): imagery_naip,    # API-proxied+cached — Phase LM §LM-1
    ("radar", "aeris"): radar_aeris,          # keyed — 3b-15; deprecated from radar valid set (T1.2)
    ("radar", "dwd_radolan"): radar_dwd_radolan,
    ("radar", "iframe"): radar_iframe,        # iframe embed — 3b-16
    ("radar", "iem_nexrad"): radar_iem_nexrad,
    ("radar", "librewxr"): radar_librewxr,   # Caddy-proxied tile, configurable endpoint — T1.2
    ("radar", "msc_geomet"): radar_msc_geomet,
    ("radar", "noaa_mrms"): radar_noaa_mrms,
    ("radar", "openweathermap"): radar_openweathermap,  # keyed — 3b-15
    ("radar", "rainviewer"): radar_rainviewer,
}


def get_provider_module(*, domain: str, provider_id: str) -> ModuleType:
    """Return the provider module by (domain, provider_id).

    Args:
        domain: Provider domain e.g. "alerts", "forecast".
        provider_id: Provider id e.g. "nws", "aeris".

    Returns:
        The provider module (has CAPABILITY symbol and fetch() callable).

    Raises:
        KeyError: Unknown (domain, provider_id) pair.
    """
    key = (domain, provider_id)
    if key not in PROVIDER_MODULES:
        raise KeyError(
            f"Unknown provider: domain={domain!r}, provider_id={provider_id!r}. "
            f"Known providers: {sorted(PROVIDER_MODULES.keys())}"
        )
    return PROVIDER_MODULES[key]
