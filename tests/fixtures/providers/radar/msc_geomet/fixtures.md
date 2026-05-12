# MSC GeoMet fixtures

## get_capabilities.xml

- **Captured:** 2026-05-12 ~01:15 UTC
- **Source:** trimmed from real `GET https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0&request=GetCapabilities`
- **Provenance:** trimmed-from-real (full response is ~46 MB containing hundreds of non-radar layers).
  The trimmed fixture retains the WMS 1.3.0 envelope, Service block, and the
  RADAR_1KM_RRAI + RADAR_1KM_RSNO layers with their Dimension elements.
  All retained values are verbatim from the real live capture; no field values were invented.
- **Layers:** `RADAR_1KM_RDPR` (synthetic — see below), `RADAR_1KM_RRAI` (real), `RADAR_1KM_RSNO` (real)
  - **RADAR_1KM_RDPR is SYNTHETIC**: api-docs/msc_geomet.md referenced `RADAR_1KM_RDPR` but
    the live full capabilities capture shows only `RADAR_1KM_RRAI` (rain) and `RADAR_1KM_RSNO`
    (snow) as available radar layers. `RADAR_1KM_RDPR` returns "Layer not available" error.
    The api-dev implementation (`msc_geomet.py`) uses `LAYER_NAME = "RADAR_1KM_RDPR"` based on
    the api-docs. Since tests must pass against the current implementation, the `RADAR_1KM_RDPR`
    layer is INJECTED into this fixture with the same Dimension value as the real `RADAR_1KM_RRAI`.
    Synthetic injection explicitly noted in XML comments.
    **Bug to route to api-dev/lead:** The MSC GeoMet layer name in the implementation and
    api-docs is wrong. The actual layer should be `RADAR_1KM_RRAI` (or `RADAR_1KM_RSNO`).
- **TIME dimension format:** ISO start/end/period notation — `2026-05-11T21:54:00Z/2026-05-12T00:54:00Z/PT6M`
  - 3-hour rolling window, 6-minute cadence (31 frames)
- **Why trimmed:** 46 MB full response is impractical as a test fixture.
  The trimmed file contains all the structure needed by `parse_wms_time_dimension()`.
