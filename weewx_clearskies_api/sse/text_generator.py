"""NWS-style text generation engine at standard and verbose verbosity levels (ADR-070 T7.3).

Produces rules-based weather text at two verbosity levels:
  - Standard: NWS one-sentence-per-component format
  - Verbose:  Full narrative paragraph

The terse level (weatherText) is NOT produced here — it remains in
conditions_text.build_weather_text() for backward compatibility.

Both public functions take an Observation instance (from observation_model)
and return a string or None when insufficient data is available.

GFE threshold tables are defined in US units (mph, °F) per AWIPS-II GFE
text formatter conventions.  Rendered output is converted to the operator's
configured unit system (US / Metric / MetricWX) at the point of output
rendering only; all Observation values and internal comparisons remain in
US units throughout.

Unit system configuration:
  Call configure(unit_system) at startup (from __main__.py) to set the
  rendering unit system.  Valid values: "US", "METRIC", "METRICWX".
  Default is "US" (no conversion).
"""

from __future__ import annotations

from weewx_clearskies_api.sse.observation_model import Observation

# ---------------------------------------------------------------------------
# Unit system configuration
# ---------------------------------------------------------------------------

_unit_system: str = "US"


def configure(unit_system: str) -> None:
    """Set the rendering unit system for all text output.

    Must be called at startup before any text generation occurs.
    Valid values: "US", "METRIC", "METRICWX".

    Args:
        unit_system: The operator's configured unit system string.
            "US"       — °F, mph (no conversion)
            "METRIC"   — °C, km/h
            "METRICWX" — °C, m/s
    """
    global _unit_system  # noqa: PLW0603
    _unit_system = unit_system


# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------


def _f_to_c(f: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (f - 32.0) * 5.0 / 9.0


def _mph_to_kmh(mph: float) -> float:
    """Convert miles per hour to kilometres per hour."""
    return mph * 1.60934


def _mph_to_ms(mph: float) -> float:
    """Convert miles per hour to metres per second."""
    return mph * 0.44704


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

    Format:
      US:              "Temperature near {T}°F."
      METRIC/METRICWX: "Temperature near {T}°C."

    T is rounded to the nearest integer in the operator's configured unit.
    The unit label is always present so output is unambiguous regardless of
    which system is in use.

    Returns None when temperature is not available.
    """
    if obs.temperature is None:
        return None
    if _unit_system in ("METRIC", "METRICWX"):
        t = round(_f_to_c(obs.temperature))
        return f"Temperature near {t}°C."
    t = round(obs.temperature)
    return f"Temperature near {t}°F."


def _wind_sentence_standard(obs: Observation) -> str | None:
    """Build the wind sentence for standard level.

    Calm threshold (GFE): < 5 mph — compared in US units (input is always mph).

    Output by unit system:
      US:       "{Direction} winds around {speed} mph."
      METRIC:   "{Direction} winds around {speed} km/h."
      METRICWX: "{Direction} winds around {speed} m/s."

    Returns None when wind data is not available.
    """
    if obs.wind_speed is None:
        return None

    speed_mph = obs.wind_speed

    # Calm threshold comparison is always in mph (Observation values are US units).
    if speed_mph < 5.0:
        return "Calm winds."

    direction = _wind_direction_label(obs.wind_direction)

    if _unit_system == "METRIC":
        speed_int = round(_mph_to_kmh(speed_mph))
        unit_label = "km/h"
    elif _unit_system == "METRICWX":
        speed_int = round(_mph_to_ms(speed_mph))
        unit_label = "m/s"
    else:
        speed_int = round(speed_mph)
        unit_label = "mph"

    if direction is not None:
        return f"{direction} winds around {speed_int} {unit_label}."
    return f"Winds around {speed_int} {unit_label}."


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
        if _unit_system in ("METRIC", "METRICWX"):
            t = round(_f_to_c(temp))
            unit_label = "°C"
        else:
            t = round(temp)
            unit_label = "°F"
        if sky_clause is not None:
            return f"Currently {t}{unit_label} {sky_clause}."
        return f"Currently {t}{unit_label}."

    if sky_clause is not None:
        return sky_clause.capitalize() + "."

    return None


def _dewpoint_sentence_verbose(obs: Observation) -> str | None:
    """Build the dew point sentence for verbose level.

    Format:
      US:              "Dew point {Td}°F."
      METRIC/METRICWX: "Dew point {Td}°C."

    Returns None when dewpoint is not available.
    """
    if obs.dewpoint is None:
        return None
    if _unit_system in ("METRIC", "METRICWX"):
        td = round(_f_to_c(obs.dewpoint))
        return f"Dew point {td}°C."
    td = round(obs.dewpoint)
    return f"Dew point {td}°F."


def _wind_sentence_verbose(obs: Observation) -> str | None:
    """Build the wind sentence for verbose level.

    Calm threshold (GFE): < 5 mph — compared in US units (input is always mph).
    Gust threshold: gust > sustained + 10 mph — compared in US units.

    Output by unit system:
      US:       "{Direction} winds around {speed} mph [with gusts up to {gust} mph]."
      METRIC:   "{Direction} winds around {speed} km/h [with gusts up to {gust} km/h]."
      METRICWX: "{Direction} winds around {speed} m/s [with gusts up to {gust} m/s]."

    Returns None when wind data is not available.
    """
    if obs.wind_speed is None:
        return None

    speed_mph = obs.wind_speed

    # Calm threshold comparison is always in mph (Observation values are US units).
    if speed_mph < 5.0:
        return "Calm winds."

    direction = _wind_direction_label(obs.wind_direction)

    if _unit_system == "METRIC":
        speed_int = round(_mph_to_kmh(speed_mph))
        unit_label = "km/h"
        gust_convert = _mph_to_kmh
    elif _unit_system == "METRICWX":
        speed_int = round(_mph_to_ms(speed_mph))
        unit_label = "m/s"
        gust_convert = _mph_to_ms
    else:
        speed_int = round(speed_mph)
        unit_label = "mph"
        gust_convert = lambda x: x  # noqa: E731

    # Build base wind phrase
    if direction is not None:
        base = f"{direction} winds around {speed_int} {unit_label}"
    else:
        base = f"Winds around {speed_int} {unit_label}"

    # Append gust qualifier when significant (threshold comparison in mph).
    if obs.wind_gust is not None and obs.wind_gust > speed_mph + 10.0:
        gust_int = round(gust_convert(obs.wind_gust))
        return f"{base} with gusts up to {gust_int} {unit_label}."

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
