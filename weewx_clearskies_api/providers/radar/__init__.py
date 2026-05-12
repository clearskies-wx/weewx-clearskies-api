"""Radar provider modules (ADR-015, 3b-14).

Day-1 keyless provider set (3b-14 — 5 providers, all keyless):
  rainviewer  — global, XYZ slippy tiles + JSON frame index.
  iem_nexrad  — US CONUS, WMS-T, GetCapabilities frame index.
  noaa_mrms   — US AK/HI/PR/Guam/Caribbean, WMS-T, GetCapabilities.
  msc_geomet  — Canada, WMS-T, GetCapabilities.
  dwd_radolan — Germany, WMS-T, GetCapabilities.

Keyed providers (aeris, openweathermap, mapbox_jma) and the tile-proxy
endpoint are deferred to 3b-15.  iframe config slot deferred to 3b-16.

Single source per deploy (ADR-015 §Operator setup flow).
No canonical-entity mapping — radar tiles are bytes (canonical-data-model §4.5).
"""
