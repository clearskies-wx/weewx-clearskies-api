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

import logging

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from weewx_clearskies_api.sse.emitter import SSEEmitter

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sse")
async def sse_stream(request: Request) -> EventSourceResponse:
    """Stream loop packets to a single SSE client.

    Creates a dedicated subscriber queue for this connection, wraps the
    emitter's event generator in an EventSourceResponse, and unsubscribes
    the queue when the client disconnects.
    """
    emitter: SSEEmitter = request.app.state.sse_emitter
    q = emitter.subscribe()

    async def _generator():  # type: ignore[return]
        try:
            async for event in emitter.event_generator(q):
                yield event
        finally:
            emitter.unsubscribe(q)

    return EventSourceResponse(_generator())
