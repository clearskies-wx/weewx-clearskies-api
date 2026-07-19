"""GET /surf/{location_id}/profile — cross-shore beach profile transect (API-MANUAL §17, ADR-097, T5.1).

Returns the cross-shore transect for the current forecast timestep (closest to now).

Data source:
  SWAN cache under the ``"transect"`` key — a dict keyed by ISO time string,
  where each value is a list of point dicts:
    {distanceFromShore, depth, waveHeight, swellHeight,
     breakingFraction, breakingDissipation}

Logic:
  1. Look up location and verify it has "surf" in its activities.
  2. Fetch SWAN data: swan.fetch(spot_id=location_id).
  3. Get transect dict; find the timestep closest to now.
  4. Sort transect points from offshore to shore (distanceFromShore descending).
  5. Detect break points: local maxima of breakingFraction >= 0.25.
  6. Apply unit conversion to waveHeight, swellHeight, distanceFromShore,
     and depth (all converted to the operator's configured display units —
     foot for US, meter for METRIC/METRICWX).

Auth level: public (no auth — same as the surf endpoint).

wire_beach_profile_config(settings) stores the parsed MarineConfig module-level.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup — see module docstring)
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None


def wire_beach_profile_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by the beach profile endpoint.

    Accepted shapes mirror wire_surf_config / wire_beach_safety_config:
      - MarineConfig instance              -> stored directly.
      - object with a .marine_config attr that is a MarineConfig -> stored.
      - anything else (including None)    -> stored as None. Never raises.
    """
    global _marine_config  # noqa: PLW0603
    if isinstance(settings, MarineConfig):
        _marine_config = settings
    elif isinstance(getattr(settings, "marine_config", None), MarineConfig):
        _marine_config = settings.marine_config  # type: ignore[attr-defined]
    else:
        _marine_config = None


# ---------------------------------------------------------------------------
# Location lookup helpers
# ---------------------------------------------------------------------------


def _find_location(location_id: str) -> MarineLocation | None:
    if _marine_config is None:
        return None
    for loc in _marine_config.locations:
        if loc.id == location_id:
            return loc
    return None


# ---------------------------------------------------------------------------
# Marine unit resolution (API-MANUAL §16 "Marine unit groups")
# ---------------------------------------------------------------------------

_DISPLAY_LABEL: dict[str, str] = {
    "foot": "ft",
    "meter": "m",
}


def _wave_height_unit() -> tuple[str, str]:
    """Return (internal_unit_name, display_symbol) for wave height."""
    height_default = "foot" if get_target_unit() == "US" else "meter"
    unit = get_group_unit("group_wave_height", height_default)
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _distance_unit() -> tuple[str, str]:
    """Return (internal_unit_name, display_symbol) for transect distance and depth.

    Uses group_wave_height's foot/meter distinction — the same meter↔foot
    conversion appropriate for beach profile distances (0–1000 m from shore)
    and depths (0–20 m).  SWAN emits both quantities in meters; the API
    converts them to the operator's configured display unit so the dashboard
    renders them unit-neutral (ADR-042).
    """
    dist_default = "foot" if get_target_unit() == "US" else "meter"
    unit = get_group_unit("group_wave_height", dist_default)
    return (unit, _DISPLAY_LABEL.get(unit, unit))


# ---------------------------------------------------------------------------
# GET /surf/{location_id}/profile — beach cross-shore transect
# ---------------------------------------------------------------------------


@router.get(
    "/surf/{location_id}/profile",
    summary="Cross-shore beach profile transect",
    tags=["Marine"],
)
def get_beach_profile(location_id: str) -> dict:
    """Return the cross-shore transect for the current forecast timestep.

    Reads transect data from the SWAN cache.  The transect is the full
    cross-shore profile from offshore to shore for the timestep whose valid
    time is closest to now.

    Break points are QB peak locations: local maxima of breakingFraction
    that are >= 0.25 (threshold for meaningful wave breaking).  Multi-break
    spots (e.g. outer bar + inner bar) will have multiple entries.

    404 when:
    - The location does not exist or does not have "surf" enabled.
    - SWAN has not yet cached data for this location.
    - The SWAN cache has no transect data (CURVE output not configured).
    """
    location = _find_location(location_id)
    if location is None or "surf" not in location.activities:
        raise HTTPException(
            status_code=404,
            detail=f"Surf location {location_id!r} not found",
        )

    # --- SWAN: fetch cached nearshore data (ADR-093, read-only cache path) ---
    swan_result: dict | None = None
    try:
        from weewx_clearskies_api.providers.nearshore import swan  # noqa: PLC0415

        swan_result = swan.fetch(spot_id=location_id)
    except Exception:
        logger.warning(
            "beach_profile endpoint: SWAN fetch failed for %s",
            location_id,
            exc_info=True,
        )

    if swan_result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SWAN data cached for location {location_id!r}",
        )

    transect_data: dict[str, list[dict]] = swan_result.get("transect") or {}

    if not transect_data:
        raise HTTPException(
            status_code=404,
            detail=f"No transect data cached for location {location_id!r} — CURVE output may not be configured",
        )

    # --- Find the timestep closest to now ---
    now = datetime.now(UTC)
    closest_time: str | None = None
    closest_delta: float | None = None

    for time_iso in transect_data:
        try:
            ts_dt = datetime.fromisoformat(time_iso.replace("Z", "+00:00"))
            delta = abs((ts_dt - now).total_seconds())
            if closest_delta is None or delta < closest_delta:
                closest_delta = delta
                closest_time = time_iso
        except (ValueError, TypeError):
            logger.debug(
                "beach_profile: skipping unparseable timestep key %r for %s",
                time_iso,
                location_id,
            )

    if closest_time is None:
        raise HTTPException(
            status_code=404,
            detail=f"No parseable timestep keys in transect data for {location_id!r}",
        )

    points: list[dict] = transect_data[closest_time]

    # --- Sort from offshore to shore (distanceFromShore descending) ---
    sorted_points = sorted(
        points,
        key=lambda p: p.get("distanceFromShore") or 0,
        reverse=True,
    )

    # --- Detect break points: local maxima of breakingFraction >= 0.25 ---
    # Matches the algorithm in surf.py lines 502-529 (same QB peak detection).
    break_points_raw: list[dict] = []
    for idx, pt in enumerate(sorted_points):
        qb = pt.get("breakingFraction")
        if qb is None or qb < 0.25:
            continue
        prev_qb = (
            (sorted_points[idx - 1].get("breakingFraction") or 0.0) if idx > 0 else 0.0
        )
        next_qb = (
            (sorted_points[idx + 1].get("breakingFraction") or 0.0)
            if idx < len(sorted_points) - 1
            else 0.0
        )
        if qb >= prev_qb and qb >= next_qb:
            # Store raw meter values for now; all three fields are converted below.
            break_points_raw.append(
                {
                    "distanceFromShore": pt.get("distanceFromShore"),
                    "depth": pt.get("depth"),
                    "_waveHeight_m": pt.get("waveHeight"),
                }
            )

    # --- Unit conversion: distanceFromShore, depth, waveHeight, swellHeight -> display units ---
    # All four quantities are emitted by SWAN in meters.  Convert each to the
    # operator's configured display unit (foot for US, meter for METRIC/METRICWX).
    wave_height_internal, wave_height_symbol = _wave_height_unit()
    distance_internal, distance_symbol = _distance_unit()

    transect_out: list[dict] = []
    for pt in sorted_points:
        wave_height_raw = pt.get("waveHeight")
        swell_height_raw = pt.get("swellHeight")
        dist_raw = pt.get("distanceFromShore")
        depth_raw = pt.get("depth")
        transect_out.append(
            {
                "distanceFromShore": (
                    _convert_unit(float(dist_raw), "meter", distance_internal)
                    if dist_raw is not None
                    else None
                ),
                "depth": (
                    _convert_unit(float(depth_raw), "meter", distance_internal)
                    if depth_raw is not None
                    else None
                ),
                "waveHeight": (
                    _convert_unit(float(wave_height_raw), "meter", wave_height_internal)
                    if wave_height_raw is not None
                    else None
                ),
                "swellHeight": (
                    _convert_unit(float(swell_height_raw), "meter", wave_height_internal)
                    if swell_height_raw is not None
                    else None
                ),
                "breakingFraction": pt.get("breakingFraction"),
                "breakingDissipation": pt.get("breakingDissipation"),
            }
        )

    # Build final break_points list with all fields converted to display units.
    break_points: list[dict] = [
        {
            "distanceFromShore": (
                _convert_unit(float(bp["distanceFromShore"]), "meter", distance_internal)
                if bp["distanceFromShore"] is not None
                else None
            ),
            "depth": (
                _convert_unit(float(bp["depth"]), "meter", distance_internal)
                if bp["depth"] is not None
                else None
            ),
            "waveHeight": (
                _convert_unit(float(bp["_waveHeight_m"]), "meter", wave_height_internal)
                if bp["_waveHeight_m"] is not None
                else None
            ),
        }
        for bp in break_points_raw
    ]

    now_str = utc_isoformat(now)
    return {
        "data": {
            "locationId": location_id,
            "transect": transect_out,
            "breakPoints": break_points,
        },
        "units": {
            "distance": distance_symbol,
            "depth": distance_symbol,
            "waveHeight": wave_height_symbol,
        },
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("surf").model_dump(by_alias=True),
        "generatedAt": now_str,
    }
