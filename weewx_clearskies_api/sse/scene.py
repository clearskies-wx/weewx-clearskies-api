"""Scene descriptor builder for ADR-047 background system.

Computes scene = {sky, daytime, overlay} from provider conditions and almanac
sun position.  All three fields are server-side state: the dashboard reads the
descriptor and selects assets; it does not recompute weather logic.

Sky-bucket mapping (ADR-047 §2):
  Clear, Mostly Clear, Partly Cloudy,
    Clear/Scattered Clouds,
    Mostly Clear/Scattered Clouds     → "clear"
  Mostly Cloudy, Cloudy, Overcast,
    Heavy Overcast                    → "cloudy"
  Foggy                               → "cloudy"   (no fog photo)
  none / unknown / startup            → "clear"    (safe fallback)
  (provider) Thunderstorm             → "storm"

Overlay mapping (ADR-047 §4):
  precipType in {snow, sleet, freezing-rain}   → "snow"  (snow wins over rain)
  rainRate > 0 OR precipType = "rain"          → "rain"
  none / linger expired                        → null

Precip linger (ADR-047 §4): overlay is set on detection and cleared 15 minutes
after the *last* detection.  State lives server-side so it survives a dashboard
reload.  time.monotonic() is used for the timer (never goes backward, no
clock-adjustment issues across the process lifetime).

Daytime (ADR-047 §5 + brief resolved decision 1): computed from almanac
sunrise/sunset, NOT from the theme toggle or maxSolarRad proxy.  The caller
must call update_sun_times() with today's UTC ISO-8601 rise/set strings.

Module-level state is intentional; this is a single-process service.
Use reset() for test isolation.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

SkyBucket = Literal["clear", "cloudy", "storm"]
OverlayValue = Literal["rain", "snow"] | None

SceneDict = dict[str, object]  # {sky: str, daytime: bool, overlay: str|None}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Precip linger duration in seconds (ADR-047 §4: 15 minutes).
_LINGER_SECONDS: float = 900.0

# Substring looked for in provider conditions text to detect storm (case-insensitive).
# ADR-047 §3: "storm = provider current-conditions text = thunderstorm".
_THUNDERSTORM_MARKER: str = "thunderstorm"

# Canonical precipType values that map to "snow" overlay (ADR-047 §3).
_SNOW_PRECIP_TYPES: frozenset[str] = frozenset({"snow", "sleet", "freezing-rain"})

# Sky-label → bucket table (ADR-047 §2).
_SKY_LABEL_TO_BUCKET: dict[str, SkyBucket] = {
    "Clear": "clear",
    "Clear, Scattered Clouds": "clear",
    "Mostly Clear": "clear",
    "Mostly Clear, Scattered Clouds": "clear",
    "Partly Cloudy": "clear",
    "Mostly Cloudy": "cloudy",
    "Cloudy": "cloudy",
    "Overcast": "cloudy",
    "Heavy Overcast": "cloudy",
    "Foggy": "cloudy",
}

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Last timestamp (monotonic) when a precipitation overlay was detected.
# None means no precip has been detected since startup.
_last_precip_mono: float | None = None

# Most recently determined overlay type when precip was detected.
# "snow" or "rain" — holds the strongest overlay type seen at last detection time.
_last_precip_overlay: OverlayValue = None

# Almanac sun times for today (UTC epoch seconds).  Updated by update_sun_times().
# None = not yet set (falls back to never-daytime-assumed-false).
_sunrise_epoch: float | None = None
_sunset_epoch: float | None = None

# Cached date string ("YYYY-MM-DD") for the currently loaded sun times.
# Used to detect when the date has rolled over and new times are needed.
_sun_times_date: str | None = None

# Last provider-derived sky label (e.g. "Overcast", "Mostly Cloudy").
# Set by the REST scene enrichment from cloudcover; read by the SSE packet
# tap so both paths produce the same scene at night.
_provider_sky_label: str | None = None


# ---------------------------------------------------------------------------
# Public API — state updates
# ---------------------------------------------------------------------------


def update_sun_times(sunrise_utc: str | None, sunset_utc: str | None) -> None:
    """Cache today's almanac sunrise and sunset as epoch seconds.

    Args:
        sunrise_utc: UTC ISO-8601 string (e.g. "2026-05-30T11:23:00Z"), or None.
        sunset_utc:  UTC ISO-8601 string (e.g. "2026-05-31T01:45:00Z"), or None.

    Silently ignores None or unparseable inputs — daytime falls back to False
    when sun times are unavailable.  Called by the scene enrichment on each
    /current request.
    """
    global _sunrise_epoch, _sunset_epoch, _sun_times_date  # noqa: PLW0603

    try:
        if sunrise_utc is None or sunset_utc is None:
            return
        rise = _parse_utc_iso(sunrise_utc)
        sset = _parse_utc_iso(sunset_utc)
        if rise is None or sset is None:
            return
        _sunrise_epoch = rise
        _sunset_epoch = sset
        # Record the calendar date of the sunrise for cache-invalidation purposes.
        _sun_times_date = datetime.fromtimestamp(rise, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        pass


def detect_precip(
    precip_type: str | None,
    conditions_text: str | None,
    rain_rate: float | None,
) -> None:
    """Record a precipitation detection event and update the linger timer.

    Should be called whenever new provider conditions data is available (once
    per /current request).  Does NOT clear the timer — the timer is cleared
    automatically in build_scene() after 15 minutes of no calls with detectable
    precip.

    Args:
        precip_type:      Provider precipType: "rain" | "snow" | "sleet" |
                          "freezing-rain" | None.
        conditions_text:  Provider weatherText / current-conditions string.
                          Checked for "Thunderstorm" (case-insensitive).
        rain_rate:        Local rain-rate reading (station gauge, in/hr or any
                          unit — only the sign matters here).  > 0 = raining.
    """
    global _last_precip_mono, _last_precip_overlay  # noqa: PLW0603

    overlay = _compute_overlay(precip_type, conditions_text, rain_rate)

    if overlay is not None:
        _last_precip_mono = time.monotonic()
        _last_precip_overlay = overlay


def update_provider_sky(label: str | None) -> None:
    """Cache the provider-derived sky label for night-time fallback.

    Called by the REST scene enrichment with a label derived from cloudcover.
    The SSE packet tap reads this via build_scene()'s internal fallback so
    both paths produce the same scene at night.
    """
    global _provider_sky_label  # noqa: PLW0603
    _provider_sky_label = label


def build_scene(sky_label: str | None) -> SceneDict:
    """Build the scene descriptor from current server state.

    Args:
        sky_label: Sky condition string from sky_condition.classify() or
                   provider sky text.  May be None at startup or when no
                   conditions data is available.  When None, falls back to
                   the cached provider sky label (set via update_provider_sky).

    Returns:
        dict with keys:
          sky:     "clear" | "cloudy" | "storm"
          daytime: bool — True when current UTC time is between sunrise and sunset.
          overlay: "rain" | "snow" | None — None when no precip or linger expired.
    """
    effective_label = sky_label if sky_label is not None else _provider_sky_label
    sky: SkyBucket = _map_sky(effective_label)
    daytime: bool = _compute_daytime()
    overlay: OverlayValue = _get_lingering_overlay()

    return {"sky": sky, "daytime": daytime, "overlay": overlay}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_utc_iso(value: str) -> float | None:
    """Parse a UTC ISO-8601 string to epoch seconds, or return None on failure."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _map_sky(sky_label: str | None) -> SkyBucket:
    """Map a sky-condition label to a background bucket (ADR-047 §2 table).

    Unrecognised labels (including None and empty string) fall back to "clear"
    per the ADR's "none / unknown / startup → clear" rule.  "Thunderstorm" maps
    to "storm" — this handles the case where the caller passes provider
    conditions text as the sky_label.
    """
    if sky_label is None:
        return "clear"
    # Provider thunderstorm text — check as the label itself.
    if _THUNDERSTORM_MARKER in sky_label.lower():
        return "storm"
    return _SKY_LABEL_TO_BUCKET.get(sky_label, "clear")


def _is_thunderstorm(conditions_text: str | None) -> bool:
    """Return True when conditions_text contains 'thunderstorm' (case-insensitive)."""
    if not conditions_text:
        return False
    return _THUNDERSTORM_MARKER in conditions_text.lower()


def _compute_overlay(
    precip_type: str | None,
    conditions_text: str | None,
    rain_rate: float | None,
) -> OverlayValue:
    """Compute the instantaneous overlay type from provider and gauge data.

    Snow wins over rain (ADR-047 §4).

    Returns:
        "snow" | "rain" | None — the strongest active overlay type, or None
        when no precipitation is detected.
    """
    pt = (precip_type or "").strip().lower()

    # Snow wins (ADR-047 §4): precipType ∈ {snow, sleet, freezing-rain}.
    if pt in _SNOW_PRECIP_TYPES:
        return "snow"

    # Rain: precipType = "rain", or positive rain gauge, or thunderstorm text
    # (thunderstorm often has rain — treat as rain overlay even without a gauge).
    has_rain = (
        pt == "rain"
        or (rain_rate is not None and rain_rate > 0)
        or _is_thunderstorm(conditions_text)
    )
    if has_rain:
        return "rain"

    return None


def _get_lingering_overlay() -> OverlayValue:
    """Return the current overlay accounting for the 15-minute linger timer.

    Returns None when:
    - No precip has ever been detected (_last_precip_mono is None), or
    - More than _LINGER_SECONDS have elapsed since the last detection.
    """
    if _last_precip_mono is None:
        return None
    elapsed = time.monotonic() - _last_precip_mono
    if elapsed > _LINGER_SECONDS:
        return None
    return _last_precip_overlay


def _compute_daytime() -> bool:
    """Return True when the current UTC time is between sunrise and sunset.

    Uses almanac sun times loaded via update_sun_times().  Returns False when
    sun times are not yet available (startup / no upstream API configured).
    """
    if _sunrise_epoch is None or _sunset_epoch is None:
        return False
    now = time.time()
    return _sunrise_epoch <= now < _sunset_epoch


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def reset() -> None:
    """Clear all module-level state.  For test isolation only."""
    global _last_precip_mono, _last_precip_overlay  # noqa: PLW0603
    global _sunrise_epoch, _sunset_epoch, _sun_times_date  # noqa: PLW0603
    global _provider_sky_label  # noqa: PLW0603
    _last_precip_mono = None
    _last_precip_overlay = None
    _sunrise_epoch = None
    _sunset_epoch = None
    _sun_times_date = None
    _provider_sky_label = None


def get_sun_times_date() -> str | None:
    """Return the date string of the currently cached sun times, or None."""
    return _sun_times_date


def sun_times_need_refresh() -> bool:
    """Return True when the cached sun times should be refreshed.

    Refresh is needed when:
    - No times are cached yet (startup), OR
    - The current time is past the cached SUNRISE by more than 24 hours,
      meaning we've gone through a full day cycle and need the next day's times.

    NOT triggered by UTC date rollover (which can occur hours before local
    sunset, e.g. midnight UTC = 5 PM PDT but sunset is 8 PM PDT) and NOT
    triggered immediately at sunset (which would fetch the next day's sunrise,
    making _compute_daytime() think it's daytime again during the evening).
    """
    if _sunrise_epoch is None or _sunset_epoch is None:
        return True
    now = time.time()
    return now > _sunrise_epoch + 86400
