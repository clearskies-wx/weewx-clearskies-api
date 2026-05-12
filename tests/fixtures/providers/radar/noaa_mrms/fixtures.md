# NOAA MRMS fixtures

## get_capabilities.xml

- **Captured:** 2026-05-12 ~01:10 UTC
- **Source:** live `GET https://mapservices.weather.noaa.gov/eventdriven/services/radar/radar_base_reflectivity_time/ImageServer/WMSServer?service=WMS&version=1.3.0&request=GetCapabilities`
- **Provenance:** real free-tier capture (no auth required; keyless provider)
- **Size:** 7925 bytes (full WMS 1.3.0 capabilities document)
- **Layers:** `radar_base_reflectivity_time` (real, verbatim from live capture); `0` (synthetic, dead — see below).
  - `radar_base_reflectivity_time` is the real WMS layer; `noaa_mrms.py` `LAYER_NAME` points at it as of lead-direct `f2362ee` (2026-05-11). Tests assert against this name.
  - The synthetic `"0"` layer was injected during 3b-14 test-author fixture capture to make tests pass against the original (wrong) `LAYER_NAME="0"` from the brief + api-docs. After the lead-direct fix this injection is unused but harmless — left in place as a historical record. A future fixture refresh can drop it.
- **TIME dimension format:** ISO start/end/period notation — `2026-05-11T23:16:00.0Z/2026-05-12T01:06:59.0Z/PT1S`
  - Note: PT1S period is surprising (1-second intervals) but the actual data cadence is ~5 min;
    the period value documents the time-dimension precision, not the update cadence.
- **Notes:** ArcGIS ImageServer WMS. Very fine-grained period (PT1S) in start/end/period form.
