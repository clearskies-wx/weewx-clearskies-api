"""Field name normalization — replaces mqtt_fields.strip_suffix.

MQTT is eliminated (ADR-058). In direct mode, field names arrive without
unit suffixes (e.g., 'outTemp' not 'outTemp_F'). This module exists for
compatibility with enrichment processors ported from the realtime service
that called strip_suffix() defensively.
"""

from __future__ import annotations


def strip_suffix(name: str) -> tuple[str, str | None]:
    """Return (base_name, unit_suffix_or_None).

    In the merged API, all fields arrive via direct mode with no unit
    suffixes. Returns (name, None) for all inputs.

    Retained so ported processors can call it without code changes.
    """
    return (name, None)
