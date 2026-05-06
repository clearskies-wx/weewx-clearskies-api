"""Station metadata endpoint.

PLACEHOLDER — returns a hardcoded StationResponse envelope matching
docs/contracts/openapi-v1.yaml::StationResponse + ::StationMetadata.
The purpose is to prove the middleware chain works end-to-end.

Phase 2 Task 3 will replace this with a real DB-backed response assembled
from weewx archive reflection (ADR-035) and api.conf values (ADR-027).

Response shape (openapi-v1.yaml lines 1502-1508, 1090-1117):
  StationResponse envelope:
    data      → StationMetadata (required: stationId, name, latitude,
                longitude, altitude [number], timezone, unitSystem)
    units     → UnitsBlock (additionalProperties: string — field→unit map)
    generatedAt → ISO-8601 UTC string
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get(
    "/station",
    summary="Station metadata",
    tags=["station"],
    # PLACEHOLDER: no real DB query. Replace in Phase 2 Task 3.
)
async def get_station() -> dict[str, object]:
    """Return station metadata wrapped in a StationResponse envelope.

    PLACEHOLDER: returns hardcoded values to prove the middleware chain
    works end-to-end. Phase 2 Task 3 wires real DB-backed data.
    """
    # Hardcoded placeholder.
    # Shape matches StationResponse from openapi-v1.yaml (envelope wraps StationMetadata).
    # StationMetadata required fields per spec lines 1090-1117:
    #   stationId (string), name (string), latitude (number, -90..90),
    #   longitude (number, -180..180), altitude (number — meters, NOT an object),
    #   timezone (IANA string), unitSystem (enum: US | METRIC | METRICWX).
    # UnitsBlock (lines 794-803): additionalProperties: string — field→unit string map.
    return {
        "data": {
            "stationId": "placeholder",
            "name": "My Weather Station (placeholder)",
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0,
            "timezone": "UTC",
            "unitSystem": "METRIC",
            "timezoneOffsetMinutes": 0,
            "firstRecord": None,
            "lastRecord": None,
            "hardware": None,
            "_placeholder": True,
        },
        "units": {
            "outTemp": "°C",
            "outHumidity": "%",
            "windSpeed": "m/s",
            "barometer": "hPa",
            "rain": "mm",
        },
        "generatedAt": "1970-01-01T00:00:00Z",
    }
