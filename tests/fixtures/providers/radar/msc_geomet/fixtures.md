# MSC GeoMet fixtures

## get_capabilities.xml

- **Captured:** 2026-05-12 ~01:15 UTC
- **Source:** trimmed from real `GET https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0&request=GetCapabilities`
- **Provenance:** trimmed-from-real (full response is ~46 MB containing hundreds of non-radar layers).
  The trimmed fixture retains the WMS 1.3.0 envelope, Service block, and the
  RADAR_1KM_RRAI + RADAR_1KM_RSNO layers with their Dimension elements.
  All retained values are verbatim from the real live capture; no field values were invented.
- **Layers:** `RADAR_1KM_RRAI` (real, rain), `RADAR_1KM_RSNO` (real, snow); `RADAR_1KM_RDPR` (synthetic, dead — see below).
  - `RADAR_1KM_RRAI` is the real WMS layer; `msc_geomet.py` `LAYER_NAME` points at it as of lead-direct `f2362ee` (2026-05-11). Tests assert against this name. The snow sibling `RADAR_1KM_RSNO` is exercised by the sibling-layer regression test in `test_wms_capabilities.py`.
  - The synthetic `RADAR_1KM_RDPR` was injected during 3b-14 test-author fixture capture to make tests pass against the original (wrong) `LAYER_NAME="RADAR_1KM_RDPR"` from the brief + api-docs. RDPR is NOT in live GeoMet capabilities. After the lead-direct fix this injection is unused but harmless — left in place as a historical record. A future fixture refresh can drop it.
- **TIME dimension format:** ISO start/end/period notation — `2026-05-11T21:54:00Z/2026-05-12T00:54:00Z/PT6M`
  - 3-hour rolling window, 6-minute cadence (31 frames)
- **Why trimmed:** 46 MB full response is impractical as a test fixture.
  The trimmed file contains all the structure needed by `parse_wms_time_dimension()`.
