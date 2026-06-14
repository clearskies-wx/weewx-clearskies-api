"""Sky-condition packet tap (ADR-044).

Registers each loop packet's radiation readings into the sky_condition
rolling buffer.  Registered as a packet_tap processor via __main__.py so the
buffer is fed on every loop packet regardless of input mode (direct or MQTT).

Keeping sky_condition.update() calls here — and only here — ensures that:
- Direct-mode and MQTT-mode packets both feed the same buffer.
- Neither transform_record() (REST path) nor add_derived_fields() (MQTT path)
  double-count readings by calling update() themselves.
"""

from __future__ import annotations

from weewx_clearskies_api.sse import sky_condition
from weewx_clearskies_api.sse.field_utils import strip_suffix


def _extract_float(raw: object) -> float | None:
    """Extract a float from a raw field value.

    Handles both plain scalars/strings and unit-converted dicts
    ``{"value": ..., "label": ..., "formatted": ...}``.
    Returns None when the value is missing or non-numeric.
    """
    if raw is None:
        return None
    value = raw.get("value") if isinstance(raw, dict) else raw  # type: ignore[union-attr]
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def update_from_packet(packet: dict) -> None:  # type: ignore[type-arg]
    """Feed radiation readings from *packet* into the sky_condition buffer.

    Normalises MQTT suffixed field names (e.g. ``radiation_Wpm2``,
    ``maxSolarRad_Wpm2``) to canonical base names via strip_suffix before
    the lookup.  For direct-mode packets the strip is a no-op.

    Calls sky_condition.update() only when both radiation and maxSolarRad
    are present and numeric.
    """
    # Build a canonical-name → raw-value view of the packet (suffix strip).
    canonical: dict[str, object] = {}
    for field_name, raw_value in packet.items():
        base_name, _ = strip_suffix(field_name)
        canonical.setdefault(base_name, raw_value)

    radiation = _extract_float(canonical.get("radiation"))
    max_solar_rad = _extract_float(canonical.get("maxSolarRad"))

    if radiation is not None and max_solar_rad is not None:
        sky_condition.update(radiation, max_solar_rad)
