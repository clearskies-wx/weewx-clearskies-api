# RainViewer fixtures

## weather-maps.json

- **Captured:** 2026-05-11 ~02:00 UTC
- **Source:** live `GET https://api.rainviewer.com/public/weather-maps.json`
- **Provenance:** real free-tier capture (no auth required; keyless provider)
- **Contents:** version=2.0, generated=1778548535, host=https://tilecache.rainviewer.com, 13 past frames (1778541000..1778548200), 0 nowcast frames
- **Notes:** nowcast array was empty at capture time; provider docs say it may carry 3x10-min forecast frames when active. Module must tolerate empty array (verified here).
