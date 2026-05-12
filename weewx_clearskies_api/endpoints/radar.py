"""GET /radar/providers/{provider_id}/frames — radar frame index (ADR-015, 3b-14).

Behavior decision tree per brief §per-endpoint spec:

  1. provider_id not in radar dispatch table → 404 Problem.
  2. provider_id IS in dispatch but NOT in capability registry → 404 Problem
     (operator configured a different provider). Same HTTP status as #1;
     detail text distinguishes them.
  3. Provider configured + registered, fetch succeeds → 200 RadarFramesResponse.
  4. Frame-index fetch returns network failure / 5xx after retries → 502 ProviderProblem
     (TransientNetworkError).
  5. Frame-index fetch returns 429 → 503 ProviderProblem (QuotaExhausted) + Retry-After.
  6. Frame-index parse failure (JSON malformed / XML missing TIME dimension) → 502
     ProviderProblem (ProviderProtocolError).

No query parameters this round (future: ?since=<iso> to limit to recent frames).
No DB hit — radar frames come from the provider, not weewx archive.

No wire_radar_settings() needed (brief lead call 6):
  Provider id lives in the capability registry; no per-request settings
  (no min-magnitude-style filter, no station lat/lon dependency).

Dispatch:
  path-param provider_id → get_provider_module(domain="radar", provider_id=<id>)
  Each radar module exposes get_frames() -> RadarFrameList (NOT fetch()).

Caching (ADR-017):
  Handled entirely inside each provider module's get_frames() call.
  Cache key = SHA-256(provider_id, "frames"); TTL = 60 s.
  No cache logic needed in this endpoint.

Radar dispatch table (5 keyless providers, 3b-14):
  "rainviewer", "iem_nexrad", "noaa_mrms", "msc_geomet", "dwd_radolan"
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.models.responses import RadarFramesResponse, utc_isoformat
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.providers._common.dispatch import get_provider_module

logger = logging.getLogger(__name__)

router = APIRouter()

# Known radar provider ids (dispatch table keys).  Validated at request time
# so we can return 404 with a helpful message rather than a KeyError traceback.
_KNOWN_RADAR_PROVIDERS = frozenset(
    {"rainviewer", "iem_nexrad", "noaa_mrms", "msc_geomet", "dwd_radolan"}
)


@router.get(
    "/radar/providers/{provider_id}/frames",
    summary="Available radar frames (timestamps)",
    tags=["Radar"],
    response_model=RadarFramesResponse,
)
def get_radar_frames(provider_id: str) -> RadarFramesResponse:
    """Return the available radar frame timestamps for the given provider.

    Reads the capability registry at request time to confirm the operator has
    configured this radar provider.  Decision tree:

      1. provider_id not in known radar dispatch table → 404.
      2. provider_id in dispatch but not in registry → 404 (different detail).
      3. Provider registered → call get_frames(), return 200 RadarFramesResponse.
      4. get_frames() raises TransientNetworkError → FastAPI error handler → 502.
      5. get_frames() raises QuotaExhausted → FastAPI error handler → 503 + Retry-After.
      6. get_frames() raises ProviderProtocolError → FastAPI error handler → 502.
    """
    now_str = utc_isoformat(datetime.now(tz=UTC))

    # --- Decision tree branch 1: unknown provider_id (not in dispatch table) ---
    if provider_id not in _KNOWN_RADAR_PROVIDERS:
        logger.debug("Radar provider_id %r not in dispatch table", provider_id)
        raise HTTPException(
            status_code=404,
            detail=(
                f"Radar provider {provider_id!r} is not supported. "
                f"Known providers: {sorted(_KNOWN_RADAR_PROVIDERS)}"
            ),
        )

    # --- Decision tree branch 2: in dispatch but not registered ---
    provider_registry = get_provider_registry()
    radar_providers = {p.provider_id for p in provider_registry if p.domain == "radar"}

    if provider_id not in radar_providers:
        logger.debug(
            "Radar provider %r is in dispatch table but not in capability registry "
            "(operator configured a different provider)",
            provider_id,
        )
        raise HTTPException(
            status_code=404,
            detail=(
                f"Radar provider {provider_id!r} is not configured for this deployment. "
                "Check the [radar] section in api.conf."
            ),
        )

    # --- Decision tree branch 3: dispatch + fetch ---
    # KeyError would only fire here if _KNOWN_RADAR_PROVIDERS contains a key
    # missing from PROVIDER_MODULES — a programming error caught at startup.
    module = get_provider_module(domain="radar", provider_id=provider_id)

    # Each radar module exposes get_frames() -> RadarFrameList (not fetch()).
    # ProviderError subclasses (TransientNetworkError, QuotaExhausted,
    # ProviderProtocolError) propagate to the FastAPI error handler in errors.py,
    # which maps them to the correct HTTP status per ADR-018.
    frames_list = module.get_frames()  # type: ignore[attr-defined]

    return RadarFramesResponse(
        data=frames_list,
        generatedAt=now_str,
    )
