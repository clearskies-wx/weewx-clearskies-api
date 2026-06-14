"""Enrichment processors for the Clear Skies API (ported from clearskies-realtime).

Each module in this package is either a packet-tap processor (registered via
sse.packet_tap.register_processor) or an endpoint enrichment function
(registered via sse.endpoint_enrichment.register_enrichment), or both.

All enrichment functions are synchronous.  HTTP calls from the realtime
versions have been replaced with direct internal function calls.
"""
