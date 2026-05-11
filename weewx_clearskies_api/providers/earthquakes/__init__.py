"""Earthquake provider modules (ADR-040).

Day-1 provider set (all keyless per ADR-040 §Day-1 provider set):
  usgs    — global coverage; FDSN GeoJSON (epoch ms timestamps).
  geonet  — New Zealand; GeoNet GeoJSON variant (ISO timestamps, no radius param).
  emsc    — Europe + Mediterranean + global; FDSN JSON (ISO timestamps).
  renass  — France + neighbours; FDSN JSON (ISO timestamps, bilingual fields).

Single source per deploy (ADR-040 §Single source per deploy).
"""
