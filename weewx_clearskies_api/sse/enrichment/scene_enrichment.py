"""Scene enrichment for GET /api/v1/current (ADR-047).

Injects a ``scene`` descriptor into the /current response envelope's ``data``
sub-dict.  The descriptor has shape::

    scene: {
        "sky":     "clear" | "cloudy" | "storm",
        "daytime": bool,
        "overlay": "rain" | "snow" | null,
    }

This enrichment runs once per GET /current request and:

1. Fetches today's almanac sunrise/sunset from the internal almanac service.
2. Reads the resolved sky label from sky_condition.classify() (local solar analysis).
3. Calls scene.detect_precip() to update the server-side 15-minute linger timer.
4. Calls scene.build_scene() to build the current descriptor and injects it into
   ``data["data"]``.

precipType is populated from the forecast provider via _fill_cloudcover_from_provider()
and passed to scene.detect_precip() for accurate snow/rain overlay detection.

The SSE stream carries the same ``scene`` field: a packet-tap processor
(scene_packet_tap.py) reads the current scene state from the module and injects
it into every loop packet before fan-out.

Never raises: all errors are caught and logged; ``scene`` is set to a default
safe value (clear/false/null) so the dashboard is never left without a background.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from weewx_clearskies_api.sse import scene as scene_mod
from weewx_clearskies_api.sse import sky_condition

logger = logging.getLogger(__name__)


def _cloud_pct_to_sky(pct: float | None) -> str | None:
    """Map cloud cover percentage to a sky-condition label (WMO okta thresholds).

    Same thresholds as weather_text._cloud_pct_to_sky — duplicated here to
    avoid a circular import and keep the scene enrichment self-contained.
    """
    if pct is None or not isinstance(pct, (int, float)):
        return None
    if pct <= 10:
        return "Clear"
    if pct <= 25:
        return "Mostly Clear"
    if pct <= 50:
        return "Partly Cloudy"
    if pct <= 85:
        return "Mostly Cloudy"
    return "Overcast"


# Fallback scene emitted when the enrichment cannot build a real descriptor.
_FALLBACK_SCENE: dict[str, object] = {
    "sky": "clear",
    "daytime": False,
    "overlay": None,
}


def _update_sun_times() -> None:
    """Fetch today's almanac sunrise/sunset and cache them in scene module.

    Re-fetches only when the cached sunset has passed — NOT on UTC date
    rollover.  A UTC date rollover can occur hours before the local sunset
    (e.g. midnight UTC = 5 PM PDT, but sunset is 8 PM PDT).  Using the
    sunset as the invalidation signal ensures daytime stays True until the
    actual sunset.

    Uses compute_almanac() + get_station_info() internally (no HTTP call).
    """
    if not scene_mod.sun_times_need_refresh():
        return  # Cached times still valid

    try:
        from weewx_clearskies_api.services.almanac import compute_almanac  # noqa: PLC0415
        from weewx_clearskies_api.services.station import get_station_info  # noqa: PLC0415

        info = get_station_info()
        now_utc = datetime.now(tz=UTC)
        today_str = now_utc.strftime("%Y-%m-%d")

        # Parse today as date object for compute_almanac.
        from datetime import date as _date  # noqa: PLC0415
        today = _date.fromisoformat(today_str)

        almanac = compute_almanac(
            today,
            info.latitude,
            info.longitude,
            info.altitude,
            station_tz=info.timezone,
        )

        rise = almanac.sun.rise
        sset = almanac.sun.set

        if rise is None or sset is None:
            return

        # If today's sunrise is still in the future, we're in the pre-sunrise
        # hours of the UTC day — but it may still be yesterday's evening locally.
        # Fetch yesterday's almanac and use those times instead.
        rise_dt = datetime.fromisoformat(rise.replace("Z", "+00:00"))
        if rise_dt > now_utc:
            yesterday = today - timedelta(days=1)
            yesterday_almanac = compute_almanac(
                yesterday,
                info.latitude,
                info.longitude,
                info.altitude,
                station_tz=info.timezone,
            )
            if yesterday_almanac.sun.rise is not None and yesterday_almanac.sun.set is not None:
                rise = yesterday_almanac.sun.rise
                sset = yesterday_almanac.sun.set

        scene_mod.update_sun_times(rise, sset)

    except Exception:  # noqa: BLE001
        logger.debug("almanac fetch failed — daytime unavailable", exc_info=True)


def enrich_scene(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``scene`` into a /current response envelope.

    Reads cloud/sky from the sky_condition module (local solar), falls back to
    provider cloudcover percentage for sky and storm detection.  precipType is
    NOT available (no internal forecast cache API), so precipitation detection
    relies on rain_rate and conditions text.  Updates the server-side linger
    state then serialises.

    Never raises.  ``data["data"]["scene"]`` is always set on return.
    """
    obs = data.get("data")
    if not isinstance(obs, dict):
        # Non-observation shape — inject at top level and return.
        data["scene"] = dict(_FALLBACK_SCENE)
        return data

    try:
        _update_sun_times()

        # Sky label: prefer local solar classifier, fall back to provider
        # cloud cover percentage at night.  Do NOT read weatherText — by this
        # point the weather_text enrichment has already composed it into a full
        # sentence (e.g. "Pleasant and Humid, with Overcast") that won't match
        # the scene module's sky-label→bucket lookup table.
        sky_label: str | None = sky_condition.classify()
        if sky_label is None:
            cloud_pct = _extract_float(obs.get("cloudcover"))
            sky_label = _cloud_pct_to_sky(cloud_pct)

        # Storm override: "thunderstorm" in conditions text triggers the
        # storm sky bucket regardless of cloud cover (ADR-047 §3).
        conditions_text = str(obs.get("weatherText") or "")
        if "thunderstorm" in conditions_text.lower():
            sky_label = "Thunderstorm"

        # Fog override: fog is a visibility phenomenon independent of cloud
        # cover — 0% clouds + fog is common (marine layer, radiation fog).
        # Map to "Foggy" which scene.py routes to the "cloudy" bucket.
        if "fog" in conditions_text.lower() and sky_label not in ("Thunderstorm",):
            sky_label = "Foggy"

        # Cache the provider-derived sky label so the SSE packet tap (which
        # has no access to cloudcover) can produce the same scene at night.
        scene_mod.update_provider_sky(sky_label)

        # Local rain rate (sign only — used as a presence check, not threshold).
        rain_rate = _extract_float(obs.get("rainRate"))

        precip_type = obs.get("precipType") if obs else None
        if isinstance(precip_type, dict):
            precip_type = precip_type.get("value")
        precip_type = str(precip_type) if precip_type is not None else None

        scene_mod.detect_precip(
            precip_type=precip_type,
            conditions_text=conditions_text,
            rain_rate=rain_rate,
        )

        descriptor = scene_mod.build_scene(sky_label)

    except Exception:  # noqa: BLE001
        logger.exception("scene enrichment failed — using fallback")
        descriptor = dict(_FALLBACK_SCENE)

    obs["scene"] = descriptor
    data["scene"] = descriptor
    return data


def _extract_float(raw: object) -> float | None:
    """Extract a float from a raw observation field value or None."""
    if raw is None:
        return None
    value = raw.get("value") if isinstance(raw, dict) else raw  # type: ignore[union-attr]
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
