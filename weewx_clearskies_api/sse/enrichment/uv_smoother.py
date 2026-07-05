"""UV index hysteresis — asymmetric rise-fast / fall-slow for GET /api/v1/current.

Directional hysteresis stabilizer for the UV field.  Rises are confirmed
after 3 consecutive samples above the current displayed value (~15 seconds
at 5-second loop intervals).  Falls require 5 minutes of sustained lower
readings before the displayed value steps down to the current reading.

This prevents transient cloud-shadow dips from misleading visitors while
allowing genuine UV increases to surface quickly.

Registered as a packet_tap processor so every loop packet feeds the state
machine.  Applied as an enrichment on the ``current`` endpoint to replace
the raw UV value with the hysteresis-filtered value.

The dashboard excludes UV from the SSE overlay merge so the REST-enriched
value is what the card displays.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Consecutive samples above displayed value before the displayed value rises.
_RISE_SAMPLES: int = 3

# Consecutive samples below displayed value before the displayed value drops.
# 5 minutes at ~5-second loop packet intervals.
_FALL_SAMPLES: int = 60

_lock = threading.Lock()
_displayed_uv: float | None = None
_rise_streak: int = 0
_fall_streak: int = 0
_warmup_count: int = 0


def accumulate_uv(packet: dict) -> None:  # type: ignore[type-arg]
    """Feed the UV value from a loop packet into the hysteresis state machine.

    Called by packet_tap for every loop packet.  Non-numeric and None values
    are silently skipped.  Must not modify the packet dict.
    """
    global _displayed_uv, _rise_streak, _fall_streak, _warmup_count

    raw = packet.get("UV")
    if raw is None:
        return
    value = raw.get("value") if isinstance(raw, dict) else raw
    if value is None:
        return
    try:
        current = float(value)
    except (TypeError, ValueError):
        return

    with _lock:
        if _displayed_uv is None:
            _warmup_count += 1
            if _warmup_count >= _RISE_SAMPLES:
                _displayed_uv = current
            return

        if current > _displayed_uv:
            _rise_streak += 1
            _fall_streak = 0
            if _rise_streak >= _RISE_SAMPLES:
                _displayed_uv = current
                _rise_streak = 0
        elif current < _displayed_uv:
            _fall_streak += 1
            _rise_streak = 0
            if _fall_streak >= _FALL_SAMPLES:
                _displayed_uv = current
                _fall_streak = 0
        else:
            _rise_streak = 0
            _fall_streak = 0


def enrich_uv(data: dict) -> dict:  # type: ignore[type-arg]
    """Replace UV in the /current response with the hysteresis-filtered value.

    Operates on the ``/current`` response envelope shape::

        {
            "data": {"UV": <raw_value>, ...},
            "units": {...},
            ...
        }
    """
    obs = data.get("data")
    if not isinstance(obs, dict):
        return data

    with _lock:
        if _displayed_uv is not None:
            obs["UV"] = round(_displayed_uv, 1)

    return data


def get_smoothed_uv() -> float | None:
    """Return the current hysteresis-filtered UV value.

    Returns:
        The displayed UV value rounded to 1 decimal place, or None
        if insufficient samples have been received.
    """
    with _lock:
        if _displayed_uv is None:
            return None
        return round(_displayed_uv, 1)


def reset() -> None:
    """Clear all hysteresis state.  For test isolation only."""
    global _displayed_uv, _rise_streak, _fall_streak, _warmup_count
    with _lock:
        _displayed_uv = None
        _rise_streak = 0
        _fall_streak = 0
        _warmup_count = 0
