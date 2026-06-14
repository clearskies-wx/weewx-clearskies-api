"""SSE endpoint — GET /sse.

Streams real-time weewx loop packets to connected clients as Server-Sent Events.

Path: /sse (root — no /api/v1 prefix per ADR-058; dashboard connects to /sse via Caddy).

Event format (matches realtime service format — dashboard needs no changes):
  event: loop
  data: <JSON-serialised loop-packet dict>

Keepalive comments are emitted every 15 s when no packet arrives (ADR-002).

No auth required — public endpoint, same posture as the former realtime service.
Rate limiting is applied at connection time by the existing RateLimitMiddleware.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from weewx_clearskies_api.sse.emitter import SSEEmitter
from weewx_clearskies_api.units.response_conversion import (
    _cardinal_for_degrees,
    _flatten_converted_value,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Fields injected by add_derived_fields() that are merged into the flattened
# SSE packet after conversion.  lightningStrikeHistory is a plain list and
# skips the ConvertedValue flattening step.
_DERIVED_FIELDS: tuple[str, ...] = (
    "beaufort",
    "comfortIndex",
    "weatherText",
    "windSpeedAvg10m",
    "windGustMax10m",
)
_DERIVED_LIST_FIELDS: tuple[str, ...] = ("lightningStrikeHistory",)


@router.get("/sse")
async def sse_stream(request: Request) -> EventSourceResponse:
    """Stream loop packets to a single SSE client.

    Creates a dedicated subscriber queue for this connection, wraps the
    emitter's event generator in an EventSourceResponse, and unsubscribes
    the queue when the client disconnects.

    Each packet is unit-converted and enriched with derived fields before
    serialisation.  The original packet dict (shared across all subscribers
    via the fan-out queue) is never mutated — transform_record() returns a
    new dict.
    """
    emitter: SSEEmitter = request.app.state.sse_emitter
    transformer = request.app.state.transformer
    try:
        q = emitter.subscribe()
    except RuntimeError:
        return JSONResponse(
            status_code=503,
            content={
                "type": "urn:clearskies:sse-capacity",
                "title": "Too many connections",
                "status": 503,
                "detail": "Maximum SSE subscriber limit reached. Try again later.",
            },
        )

    async def _generator():  # type: ignore[return]
        try:
            async for event in emitter.event_generator(q):
                # Pass keepalive comments through unchanged.
                if "comment" in event:
                    yield event
                    continue

                # event["data"] is a JSON string serialised by event_generator().
                # Deserialise, convert, re-serialise.
                try:
                    raw_str = event.get("data", "{}")
                    packet: dict = json.loads(raw_str)

                    if transformer is not None:
                        # 1. Determine unit system from the packet.
                        us_units = int(packet.get("usUnits", 1))

                        # 2. transform_record() converts all known fields;
                        #    returns a NEW dict (original packet is unchanged).
                        converted = transformer.transform_record(packet, us_units)

                        # 3. Flatten ConvertedValue dicts to display-precision
                        #    scalars (same as /current — not full-precision).
                        flattened: dict = {}
                        for key, val in converted.items():
                            if key == "extras" and isinstance(val, dict):
                                flattened_extras: dict = {}
                                for sub_key, sub_val in val.items():
                                    flattened_extras[sub_key] = _flatten_converted_value(sub_val)
                                flattened[key] = flattened_extras
                                continue
                            if not isinstance(val, dict) or "value" not in val:
                                flattened[key] = val
                                continue
                            flattened[key] = _flatten_converted_value(val)

                        # 4. Inject canonical 16-point cardinal wind direction
                        #    codes (null windDir → null windDirCardinal).
                        flattened["windDirCardinal"] = _cardinal_for_degrees(
                            flattened.get("windDir")
                        )
                        flattened["windGustDirCardinal"] = _cardinal_for_degrees(
                            flattened.get("windGustDir")
                        )

                        # 5. Add derived fields: beaufort, comfortIndex,
                        #    weatherText, windSpeedAvg10m, windGustMax10m,
                        #    lightningStrikeHistory.
                        #    add_derived_fields() expects ConvertedValue dicts
                        #    for windSpeed and outTemp, so pass the unconverted
                        #    `converted` dict (not `flattened`).
                        transformer.add_derived_fields(converted)

                        # 6. Merge derived scalar fields into flattened output.
                        for field in _DERIVED_FIELDS:
                            if field in converted:
                                flattened[field] = _flatten_converted_value(converted[field])

                        # 7. Merge list-type derived fields (no flattening needed).
                        for field in _DERIVED_LIST_FIELDS:
                            if field in converted:
                                flattened[field] = converted[field]

                        packet = flattened

                    yield {"event": "loop", "data": json.dumps(packet, default=str)}

                except Exception:  # noqa: BLE001
                    logger.exception("SSE packet conversion failed; forwarding raw")
                    yield event
        finally:
            emitter.unsubscribe(q)

    return EventSourceResponse(_generator())
