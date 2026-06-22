"""NWS-style text generation engine at standard and verbose verbosity levels (ADR-070 T7.3).

Produces rules-based weather text at two verbosity levels:
  - Standard: NWS one-sentence-per-component format
  - Verbose:  Full narrative paragraph

The terse level (weatherText) is NOT produced here — it remains in
conditions_text.build_weather_text() for backward compatibility.

Both public functions take an Observation instance (from observation_model)
and return a string or None when insufficient data is available.

GFE threshold tables are defined in US units (mph, °F) per AWIPS-II GFE
text formatter conventions.  Rendered output is currently US units only.

TODO: Unit conversion to operator's configured system (Metric, MetricWX)
is a future task.  When implemented, the GFE thresholds in mph/°F serve
as the reference; converted values are rendered in operator units throughout.
"""

from __future__ import annotations

from weewx_clearskies_api.sse.observation_model import Observation

# ---------------------------------------------------------------------------
# GFE sky coverage bucket thresholds (cloud cover %)
# Used only when obs.sky_label is None (fallback path).
# ---------------------------------------------------------------------------

_GFE_SKY_BUCKETS: list[tuple[float, str, str]] = [
    # (upper_bound_exclusive, day_label, night_label)
    (5.0,  "Sunny",         "Clear"),
    (25.0, "Mostly Sunny",  "Mostly Clear"),
    (50.0, "Partly Sunny",  "Partly Cloudy"),
    (69.0, "Mostly Cloudy", "Mostly Cloudy"),
    (87.0, "Cloudy",        "Cloudy"),
    (100.1, "Overcast",     "Overcast"),
]

# ---------------------------------------------------------------------------
# Day/night sky label mapping
# CAELUS labels use the neutral/night form; daytime variants map "Clear"→"Sunny"
# and "Mostly Clear"→"Mostly Sunny" to follow NWS day/night convention.
# "Partly Cloudy" at daytime becomes "Partly Sunny" per NWS convention.
# ---------------------------------------------------------------------------

_DAY_SKY_MAP: dict[str, str] = {
    "Clear":                          "Sunny",
    "Mostly Clear":                   "Mostly Sunny",
    "Clear, Scattered Clouds":        "Sunny",
    "Mostly Clear, Scattered Clouds": "Mostly Sunny",
    "Partly Cloudy":                  "Partly Sunny",
    # These pass through unchanged for both day and night:
    # "Mostly Cloudy", "Cloudy", "Overcast", "Heavy Overcast"
}

# ---------------------------------------------------------------------------
# 8-point compass wind direction sectors (22.5° each, centered on label)
# ---------------------------------------------------------------------------

_WIND_DIRECTIONS: list[tuple[float, str]] = [
    (22.5,  "North"),
    (67.5,  "Northeast"),
    (112.5, "East"),
    (157.5, "Southeast"),
    (202.5, "South"),
    (247.5, "Southwest"),
    (292.5, "West"),
    (337.5, "Northwest"),
    (360.0, "North"),   # wrap-around: 337.5–360
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _wind_direction_label(degrees: float | None) -> str | None:
    """Convert wind direction in degrees to 8-point compass label.

    Uses 22.5-degree sectors centered on each cardinal/intercardinal point.
    Returns None when degrees is None.
    """
    if degrees is None:
        return None
    # Normalise to [0, 360)
    d = float(degrees) % 360.0
    for upper, label in _WIND_DIRECTIONS:
        if d < upper:
            return label
    return "North"  # unreachable after wrap-around entry, but satisfies type checker


def _sky_label_for_output(obs: Observation) -> str | None:
    """Resolve the sky label to use for text output.

    Priority:
    1. obs.sky_label (from CAELUS classify()) — apply day/night mapping
    2. obs.cloud_cover_pct with GFE bucket table as fallback

    Returns None when neither source is available.
    """
    if obs.sky_label is not None:
        label = obs.sky_label
        if obs.is_daytime:
            label = _DAY_SKY_MAP.get(label, label)
        return label

    if obs.cloud_cover_pct is not None:
        pct = obs.cloud_cover_pct
        for upper, day_label, night_label in _GFE_SKY_BUCKETS:
            if pct < upper:
                return day_label if obs.is_daytime else night_label

    return None


def _sky_sentence_standard(obs: Observation) -> str | None:
    """Build the sky sentence for standard level.

    When precipitation is active it modifies the sky label with "with":
      "Mostly Cloudy with Light Rain."
    Otherwise just the sky label:
      "Sunny."

    Returns None when no sky data is available.
    """
    sky = _sky_label_for_output(obs)
    if sky is None:
        return None

    if obs.precipitation_label is not None:
        return f"{sky} with {obs.precipitation_label}."
    return f"{sky}."


def _present_weather_sentence(obs: Observation) -> str | None:
    """Build the present-weather sentence (haze / fog / mist) for standard level.

    NWS convention: haze and fog appear as SEPARATE sentences — not combined
    with the sky condition.  Priority: fog/mist over haze (consistent with
    observation_model present_weather ordering).

    Returns None when no present-weather phenomena are active.
    """
    if obs.fog_mist_state is not None:
        # "Foggy." or "Misty."
        return f"{obs.fog_mist_state}."
    if obs.haze_detected:
        return "Hazy."
    return None


def _temperature_sentence_standard(obs: Observation) -> str | None:
    """Build the temperature sentence for standard level.

    Format: "Temperature near {T}."
    T is rounded to the nearest integer (°F).

    Returns None when temperature is not available.
    """
    if obs.temperature is None:
        return None
    t = round(obs.temperature)
    return f"Temperature near {t}."


def _wind_sentence_standard(obs: Observation) -> str | None:
    """Build the wind sentence for standard level.

    Thresholds (mph, GFE):
      < 5  → "Calm winds."
      >= 5 → "{Direction} winds around {speed} mph."

    Returns None when wind data is not available.
    """
    if obs.wind_speed is None:
        return None

    speed = obs.wind_speed

    if speed < 5.0:
        return "Calm winds."

    direction = _wind_direction_label(obs.wind_direction)
    speed_int = round(speed)

    if direction is not None:
        return f"{direction} winds around {speed_int} mph."
    return f"Winds around {speed_int} mph."


def _sky_narrative_verbose(obs: Observation) -> str | None:
    """Build the sky/conditions opening for verbose level.

    Format: "Currently {T}°F under {sky description}." or
            "Currently {T}°F with {fog/mist description}."

    When temperature is None but sky is available, returns sky description alone.
    When both are None, returns None.
    """
    sky = _sky_label_for_output(obs)
    fog = obs.fog_mist_state
    haze = obs.haze_detected
    temp = obs.temperature
    precip = obs.precipitation_label

    # Build the sky/condition description clause
    if fog is not None:
        if fog == "Foggy":
            sky_clause = "with fog limiting visibility"
        else:
            sky_clause = "with mist"
    elif sky is not None:
        # Apply haze prefix for clear-ish conditions
        sky_lower = sky.lower()
        _haze_eligible = (
            "clear" in sky_lower
            or "sunny" in sky_lower
            or "scattered clouds" in sky_lower
            or "partly" in sky_lower
        )
        if haze and _haze_eligible:
            # "under hazy sunshine" (day) or "under hazy skies" (night)
            if obs.is_daytime and ("sunny" in sky_lower or "clear" in sky_lower):
                sky_clause = "under hazy sunshine"
            else:
                sky_clause = "under hazy skies"
        elif precip is not None:
            sky_clause = f"under {sky.lower()} skies with {precip.lower()}"
        elif "sunny" in sky_lower:
            sky_clause = f"under {sky.lower()} skies"
        elif "overcast" in sky_lower:
            sky_clause = f"under {sky.lower()} skies"
        else:
            sky_clause = f"under {sky.lower()} skies"
    elif haze:
        sky_clause = "under hazy conditions"
    elif precip is not None:
        sky_clause = f"with {precip.lower()}"
    else:
        sky_clause = None

    if temp is not None:
        t = round(temp)
        if sky_clause is not None:
            return f"Currently {t}°F {sky_clause}."
        return f"Currently {t}°F."

    if sky_clause is not None:
        return sky_clause.capitalize() + "."

    return None


def _dewpoint_sentence_verbose(obs: Observation) -> str | None:
    """Build the dew point sentence for verbose level.

    Format: "Dew point {Td}°F."
    Returns None when dewpoint is not available.
    """
    if obs.dewpoint is None:
        return None
    td = round(obs.dewpoint)
    return f"Dew point {td}°F."


def _wind_sentence_verbose(obs: Observation) -> str | None:
    """Build the wind sentence for verbose level.

    Thresholds (mph, GFE):
      < 5  → "Calm winds."
      >= 5 → "{Direction} winds around {speed} mph."
             with optional "with gusts up to {gust} mph."
             when gust > sustained + 10 mph

    Returns None when wind data is not available.
    """
    if obs.wind_speed is None:
        return None

    speed = obs.wind_speed

    if speed < 5.0:
        return "Calm winds."

    direction = _wind_direction_label(obs.wind_direction)
    speed_int = round(speed)

    # Build base wind phrase
    if direction is not None:
        base = f"{direction} winds around {speed_int} mph"
    else:
        base = f"Winds around {speed_int} mph"

    # Append gust qualifier when significant
    if obs.wind_gust is not None and obs.wind_gust > speed + 10.0:
        gust_int = round(obs.wind_gust)
        return f"{base} with gusts up to {gust_int} mph."

    return f"{base}."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_standard(obs: Observation) -> str | None:
    """NWS one-sentence-per-component format.

    Components in order:
      1. Sky condition sentence (with precipitation modifier if active)
      2. Present weather sentence (haze, fog, mist — separate sentence per NWS)
      3. Temperature sentence
      4. Wind sentence

    Note: Precipitation is handled as a modifier on the sky sentence
    ("Mostly Cloudy with Light Rain."), not as a standalone sentence.

    Returns None when all components produce None (insufficient data).
    """
    parts: list[str | None] = [
        _sky_sentence_standard(obs),
        _present_weather_sentence(obs),
        _temperature_sentence_standard(obs),
        _wind_sentence_standard(obs),
    ]

    sentences = [p for p in parts if p is not None]
    if not sentences:
        return None
    return " ".join(sentences)


def generate_verbose(obs: Observation) -> str | None:
    """Full narrative format.

    Components in order:
      1. Opening sentence: "Currently {T}°F under {sky description}."
      2. Dew point sentence: "Dew point {Td}°F."
      3. Wind sentence: "{Direction} winds around {speed} mph."

    Returns None when all components produce None (insufficient data).
    """
    parts: list[str | None] = [
        _sky_narrative_verbose(obs),
        _dewpoint_sentence_verbose(obs),
        _wind_sentence_verbose(obs),
    ]

    sentences = [p for p in parts if p is not None]
    if not sentences:
        return None
    return " ".join(sentences)
