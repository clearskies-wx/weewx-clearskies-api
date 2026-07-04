"""AQI provider modules (ADR-013).

Day-1 provider: openmeteo (keyless, global coverage, first AQI provider).
Also wired: aeris (3b-10), iqair (3b-12).

openweathermap and openaq removed from AQI (Phase 2 API removals): OWM AQI
returns SILAM model predictions, not observed PM data; openaq was an orphaned
module never wired into dispatch.
"""
