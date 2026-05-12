# DWD RADOLAN fixtures

## get_capabilities.xml

- **Captured:** 2026-05-12 ~01:15 UTC
- **Source:** trimmed from real `GET https://maps.dwd.de/geoserver/dwd/wms?service=WMS&version=1.3.0&request=GetCapabilities`
- **Provenance:** trimmed-from-real (full response is ~859 KB containing 200+ layers).
  The trimmed fixture retains the WMS 1.3.0 envelope, Service block, and the
  Niederschlagsradar + RADOLAN-RW layers with their Dimension elements.
  All retained values are verbatim from the real live capture; no field values were invented.
- **Layers:** `Niederschlagsradar` (real, 5-min reflectivity, alias for RV-Produkt); `RADOLAN-RW` (real, hourly calibrated precipitation, sibling); `dwd:RX-Produkt` (synthetic, dead — see below).
  - `Niederschlagsradar` is the real WMS layer; `dwd_radolan.py` `LAYER_NAME` points at it as of lead-direct `f2362ee` (2026-05-11). Tests assert against this name. The hourly sibling `RADOLAN-RW` is exercised by the sibling-layer regression test in `test_wms_capabilities.py`.
  - The synthetic `dwd:RX-Produkt` was injected during 3b-14 test-author fixture capture to make tests pass against the original (wrong) `LAYER_NAME="dwd:RX-Produkt"` from the brief + api-docs. RX-Produkt is NOT in live GeoServer capabilities. After the lead-direct fix this injection is unused but harmless — left in place as a historical record. A future fixture refresh can drop it.
- **TIME dimension format:** ISO start/end/period notation — `2026-05-08T00:00:00.000Z/2026-05-12T03:15:00.000Z/PT5M`
  - ~4-day rolling window, 5-minute cadence
- **Why trimmed:** 859 KB full response contains 200+ layers unrelated to radar; trimmed to
  the minimum structure needed by `parse_wms_time_dimension()`.
- **Note on comma-separated form:** The DWD full capabilities does include a `REFERENCE_TIME`
  dimension with comma-separated timestamps on some layers, but the primary TIME dimension
  for all radar products uses ISO start/end/period form. The comma-separated form coverage is
  provided by test_wms_capabilities_unit.py's synthetic fixture (see that file).
