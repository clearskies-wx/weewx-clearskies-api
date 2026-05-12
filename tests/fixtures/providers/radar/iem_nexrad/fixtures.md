# IEM NEXRAD fixtures

## get_capabilities.xml

- **Captured:** 2026-05-11 ~02:00 UTC
- **Source:** live `GET https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities`
- **Provenance:** real free-tier capture (no auth required; keyless provider)
- **Size:** 7891 bytes (full WMS 1.3.0 capabilities document)
- **Layer:** `nexrad-n0q-wmst`
- **TIME dimension format:** ISO start/end/period notation — `2011-02-16/2026-12-31/PT5M`
  - Note: this is a long historical range covering IEM's full NEXRAD archive;
    actual rolling window is determined by the service at GetMap time.
- **Notes:** uses WMS 1.3.0, MapServer-based. Dimension uses bare-date form (no time component in start/end, just dates + period PT5M).
