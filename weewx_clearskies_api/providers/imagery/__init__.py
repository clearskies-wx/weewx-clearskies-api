"""Imagery provider modules (Phase LM — orthophoto imagery for heatmap geographic
context; operator-requested + rethought 2026-08-02).

General-purpose API provider domain — NOT marine-specific. Any card/feature
may consume `GET /api/v1/imagery/config`, not just the marine heatmap.

Two providers:
  naip — USGS NAIP Plus (CONUS only). API proxies + caches tile bytes
         (dynamic ArcGIS ImageServer, no upstream tile cache — bbox computed
         per z/x/y and fetched via exportImage). Public domain, keyless.
  esri — Esri World Imagery (global). Config-only: returns the XYZ tile URL
         template + attribution. The API never fetches or caches ESRI tile
         bytes — the browser fetches them directly. Non-commercial use only
         per Esri ToS; no key required for the tile service itself.

Provider selection ([imagery] provider = "auto" | "naip" | "esri") happens
per-request based on the spot's coordinates (CONUS bbox test), not a single
operator-wide pick — see endpoints/imagery.py.

HARD RULE (phase header, operator 2026-08-02): imagery is DISPLAY-ONLY.
Nothing in this domain may feed SWAN, the 1D model, transect selection, or
any physics path.
"""
