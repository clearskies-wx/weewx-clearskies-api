# NOAA MRMS fixtures

## get_capabilities.xml

- **Captured:** 2026-05-12 ~01:10 UTC
- **Source:** live `GET https://mapservices.weather.noaa.gov/eventdriven/services/radar/radar_base_reflectivity_time/ImageServer/WMSServer?service=WMS&version=1.3.0&request=GetCapabilities`
- **Provenance:** real free-tier capture (no auth required; keyless provider)
- **Size:** 7925 bytes (full WMS 1.3.0 capabilities document)
- **Layer:** `radar_base_reflectivity_time`
- **TIME dimension format:** ISO start/end/period notation — `2026-05-11T23:16:00.0Z/2026-05-12T01:06:59.0Z/PT1S`
  - Note: PT1S period is surprising (1-second intervals) but the actual data cadence is ~5 min;
    the period value documents the time-dimension precision, not the update cadence.
- **Notes:** ArcGIS ImageServer WMS. Very fine-grained period (PT1S) in start/end/period form.
