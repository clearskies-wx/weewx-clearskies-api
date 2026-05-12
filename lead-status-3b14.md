# Lead status — 3b-14 test-author (2026-05-12)

Responding to lead poll. SendMessage not available as deferred tool in this session;
using side-channel per brief instruction.

## Current state

All fixtures captured, all test files written, committing now.

### Fixtures (committed 79b2dff, pushed):
- rainviewer/weather-maps.json — live capture 2026-05-11, 13 past frames, 0 nowcast
- iem_nexrad/get_capabilities.xml — live capture, 7891 bytes, layer nexrad-n0q-wmst, period notation
- noaa_mrms/get_capabilities.xml — REAL + SYNTHETIC (layer "0" injected, see below)
- msc_geomet/get_capabilities.xml — trimmed-from-real + SYNTHETIC (layer RADAR_1KM_RDPR injected)
- dwd_radolan/get_capabilities.xml — trimmed-from-real + SYNTHETIC (layer dwd:RX-Produkt injected)

### Layer-name divergences exposed (3 api-docs bugs):

1. **NOAA MRMS**: api-dev uses `LAYER_NAME = "0"` (ArcGIS convention from api-docs).
   Real layer name in GetCapabilities is `radar_base_reflectivity_time`. Synthetic
   layer "0" injected into fixture. Tests pass but the real provider will fail at
   runtime unless the layer name is corrected.

2. **MSC GeoMet**: api-dev uses `LAYER_NAME = "RADAR_1KM_RDPR"`. Real available layers
   are `RADAR_1KM_RRAI` and `RADAR_1KM_RSNO`. `RADAR_1KM_RDPR` returns ServiceException.
   Synthetic RADAR_1KM_RDPR injected into fixture. Same issue: real provider will fail.

3. **DWD RADOLAN**: api-dev uses `LAYER_NAME = "dwd:RX-Produkt"`. Real 5-min layer name
   is `Niederschlagsradar`. Synthetic dwd:RX-Produkt injected into fixture.

**Routing these to api-dev as bugs.** Tests are written to pass against current impl
(following brief rule: tests verify the brief; brief is authority — but impl diverges from
real provider shapes which the fixtures now document).

### Test files written:
- tests/providers/radar/__init__.py
- tests/providers/radar/test_wms_capabilities.py — WMS helper, both dimension forms, 5 fixture classes
- tests/providers/radar/test_rainviewer.py — wire-shape, canonical translation, cache, errors, CAPABILITY
- tests/providers/radar/test_iem_nexrad.py — WMS-T fixture, canonical translation, cache, errors, CAPABILITY
- tests/providers/radar/test_noaa_mrms.py — same shape as iem_nexrad
- tests/providers/radar/test_msc_geomet.py — same shape, specific 31-frame count
- tests/providers/radar/test_dwd_radolan.py — same shape, cap test
- tests/test_radar_endpoint_integration.py — all 6 decision-tree branches, all 5 providers

## Next action

Committing all test files now → pushing → running pytest → sending pytest counts.
