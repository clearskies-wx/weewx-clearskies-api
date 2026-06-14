"""Scene injection for SSE loop packets (ADR-047).

Called from the SSE pipeline after any unit conversion so the structured
``scene`` dict is not corrupted by float-parse passthrough for unknown fields.

Every loop packet broadcast over the SSE stream gains a ``scene`` field::

    {
        "sky":     "clear" | "cloudy" | "storm",
        "daytime": bool,
        "overlay": "rain" | "snow" | null,
    }

The scene module maintains server-side state (precip linger timer, almanac
sunrise/sunset).  This module reads that state via ``scene.build_scene()``
and injects it into the packet dict in-place.

Sky label comes from ``sky_condition.classify()`` (local solar analysis).
Night and startup produce None; ``scene._map_sky()`` maps that to "clear" per
ADR-047 §2 (none/unknown → clear fallback).
"""

from __future__ import annotations

from weewx_clearskies_api.sse import scene as scene_mod
from weewx_clearskies_api.sse import sky_condition


def inject_scene_into_packet(packet: dict) -> None:  # type: ignore[type-arg]
    """Inject the current scene descriptor into *packet* for SSE broadcast.

    Sets ``packet["scene"]`` unconditionally so SSE clients always receive the
    field (even the "clear/False/null" fallback at startup).  Failures are
    swallowed rather than raised so a scene-module bug never stalls the
    fan-out pipeline.
    """
    try:
        sky_label = sky_condition.classify()
        packet["scene"] = scene_mod.build_scene(sky_label)
    except Exception:  # noqa: BLE001 S110
        # A scene computation failure must not stall the SSE fan-out.
        packet["scene"] = {"sky": "clear", "daytime": False, "overlay": None}
