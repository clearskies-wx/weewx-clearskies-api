"""Endpoint enrichment registry — accumulate per-endpoint transform functions.

Replaces the realtime proxy.py register_enrichment() pattern with a simple
dict-based registry.  All enrichment functions are synchronous
``Callable[[dict], dict]`` — no async support needed because all HTTP calls
have been replaced with sync internal function calls.

Usage:
    # At startup (after processor registration):
    from weewx_clearskies_api.sse.endpoint_enrichment import register_enrichment
    register_enrichment("current", barometer_trend.enrich_barometer_trend)
    register_enrichment("current", weather_text.enrich_weather_text)

    # In an endpoint handler (after model_dump()):
    from weewx_clearskies_api.sse.endpoint_enrichment import apply_enrichments
    response_dict = response.model_dump(by_alias=True, exclude_none=True)
    response_dict = apply_enrichments("current", response_dict)
    return response_dict
"""

import logging

logger = logging.getLogger(__name__)

_registry: dict[str, list] = {}


def register_enrichment(endpoint_key: str, fn: object) -> None:
    """Register *fn* to run on responses for *endpoint_key*.

    Functions are applied in registration order.  *fn* must be a callable
    that accepts a dict and returns a dict (Callable[[dict], dict]).

    Args:
        endpoint_key: Logical endpoint name (e.g. "current", "almanac/planets").
        fn:           Enrichment function — sync, must never raise.
    """
    _registry.setdefault(endpoint_key, []).append(fn)


def apply_enrichments(endpoint_key: str, data: dict) -> dict:
    """Run all registered enrichments for *endpoint_key*.

    Errors from individual enrichment functions are logged at ERROR level and
    skipped — the remaining enrichments still run.  The dict is returned in
    whatever state it reached when the error occurred, so partial enrichment
    is possible but data is never lost.

    Args:
        endpoint_key: Logical endpoint name matching a prior register_enrichment() call.
        data:         Response dict to enrich (mutated in-place and returned).

    Returns:
        Enriched response dict.
    """
    for fn in _registry.get(endpoint_key, []):
        try:
            data = fn(data)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Enrichment %s failed for endpoint %s",
                getattr(fn, "__name__", repr(fn)),
                endpoint_key,
            )
    return data


def clear_enrichments() -> None:
    """Clear all registrations.  For test isolation only."""
    _registry.clear()
