"""SSE endpoint — GET /sse.

Streams real-time weewx loop packets to connected clients as Server-Sent Events.

Path: /sse (root — no /api/v1 prefix per ADR-058; dashboard connects to /sse via Caddy).

Event format (matches realtime service format — dashboard needs no changes):
  event: loop
  data: <JSON-serialised loop-packet dict>

Keepalive comments are emitted every 15 s when no packet arrives (ADR-002).

No auth required — public endpoint, same posture as the former realtime service.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from weewx_clearskies_api.sse.emitter import SSEEmitter
from weewx_clearskies_api.units.response_conversion import _cardinal_for_degrees

logger = logging.getLogger(__name__)

router = APIRouter()


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
                        us_units = int(packet.get("usUnits", 1))
                        converted = transformer.transform_record(packet, us_units)

                        # Add derived fields (beaufort, comfortIndex) from
                        # the ConvertedValue dicts in the converted record.
                        transformer.add_derived_fields(converted)

                        # Inject cardinal codes from the ConvertedValue windDir.
                        wind_dir = converted.get("windDir")
                        wind_gust_dir = converted.get("windGustDir")
                        deg = wind_dir["value"] if isinstance(wind_dir, dict) and "value" in wind_dir else wind_dir
                        gust_deg = wind_gust_dir["value"] if isinstance(wind_gust_dir, dict) and "value" in wind_gust_dir else wind_gust_dir
                        converted["windDirCardinal"] = _cardinal_for_degrees(deg)
                        converted["windGustDirCardinal"] = _cardinal_for_degrees(gust_deg)

                        # Wrap remaining raw numeric fields in ConvertedValue
                        # format so the dashboard's isConvertedValue() check
                        # passes for every field (matches old BFF behavior).
                        for k, v in converted.items():
                            if isinstance(v, (int, float)) and v is not True and v is not False:
                                converted[k] = {"value": v, "label": "", "formatted": str(v)}

                        packet = converted

                    yield {"event": "loop", "data": json.dumps(packet, default=str)}

                except Exception:  # noqa: BLE001
                    logger.exception("SSE packet conversion failed; forwarding raw")
                    yield event
        finally:
            emitter.unsubscribe(q)

    return EventSourceResponse(_generator())
