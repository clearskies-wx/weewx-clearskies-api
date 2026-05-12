# MSC GeoMet fixtures

## get_capabilities.xml

- **Captured:** 2026-05-12 ~01:15 UTC
- **Source:** trimmed from real `GET https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0&request=GetCapabilities`
- **Provenance:** trimmed-from-real (full response is ~46 MB containing hundreds of non-radar layers).
  The trimmed fixture retains the WMS 1.3.0 envelope, Service block, and the
  RADAR_1KM_RRAI + RADAR_1KM_RSNO layers with their Dimension elements.
  All retained values are verbatim from the real live capture; no field values were invented.
- **Layer:** `RADAR_1KM_RRAI` (rain precipitation rate — recommended default per msc_geomet.md)
  - Also `RADAR_1KM_RSNO` (snow precipitation rate) retained as a sibling layer.
  - NOTE: api-docs/msc_geomet.md referenced `RADAR_1KM_RDPR` but the live capture
    shows only `RADAR_1KM_RRAI` and `RADAR_1KM_RSNO` as available radar layers.
    `RADAR_1KM_RDPR` returns "Layer not available" from the filtered GetCapabilities.
    This is a api-docs drift; the provider module should use `RADAR_1KM_RRAI`.
- **TIME dimension format:** ISO start/end/period notation — `2026-05-11T21:54:00Z/2026-05-12T00:54:00Z/PT6M`
  - 3-hour rolling window, 6-minute cadence (31 frames)
- **Why trimmed:** 46 MB full response is impractical as a test fixture.
  The trimmed file contains all the structure needed by `parse_wms_time_dimension()`.
