"""Unit group definitions, observation→group mapping, and standard unit systems.

All group names, unit names, and observation names match weewx 5.x exactly.
System codes: US=1, METRIC=16, METRICWX=17 — same as weewx's stdTypes.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Valid units per group
# ---------------------------------------------------------------------------

# pass-through groups: single unit, no conversion needed; not listed here but
# present in OBS_GROUP so field lookups still work.
VALID_UNITS: dict[str, set[str]] = {
    "group_temperature": {"degree_F", "degree_C", "degree_K", "degree_E"},
    "group_speed": {"mile_per_hour", "km_per_hour", "knot", "meter_per_second"},
    "group_speed2": {"mile_per_hour2", "km_per_hour2", "knot2", "meter_per_second2"},
    "group_pressure": {"inHg", "mbar", "hPa", "kPa"},
    "group_pressurerate": {"inHg_per_hour", "mbar_per_hour", "hPa_per_hour", "kPa_per_hour"},
    "group_rain": {"inch", "cm", "mm"},
    "group_rainrate": {"inch_per_hour", "cm_per_hour", "mm_per_hour"},
    "group_altitude": {"foot", "meter"},
    "group_distance": {"mile", "km"},
    "group_direction": {"degree_compass"},
    "group_radiation": {"watt_per_meter_squared"},
    "group_uv": {"uv_index"},
    "group_percent": {"percent"},
    "group_moisture": {"centibar"},
    "group_volt": {"volt"},
}

# ---------------------------------------------------------------------------
# Observation → group mapping (weewx 5.x canonical names)
# ---------------------------------------------------------------------------

OBS_GROUP: dict[str, str] = {
    "altimeter":          "group_pressure",
    "appTemp":            "group_temperature",
    "barometer":          "group_pressure",
    "barometerRate":      "group_pressurerate",
    "cloudbase":          "group_altitude",
    "cloudcover":         "group_percent",
    "cooldeg":            "group_degree_day",
    "dayRain":            "group_rain",
    "dewpoint":           "group_temperature",
    "ET":                 "group_rain",
    "extraHumid1":        "group_percent",
    "extraHumid2":        "group_percent",
    "extraTemp1":         "group_temperature",
    "extraTemp2":         "group_temperature",
    "extraTemp3":         "group_temperature",
    "gustdir":            "group_direction",
    "heatdeg":            "group_degree_day",
    "heatindex":          "group_temperature",
    "highOutTemp":        "group_temperature",
    "hourRain":           "group_rain",
    "humidex":            "group_temperature",
    "inDewpoint":         "group_temperature",
    "inHumidity":         "group_percent",
    "inTemp":             "group_temperature",
    "leafTemp1":          "group_temperature",
    "leafTemp2":          "group_temperature",
    "lightning_distance": "group_distance",
    "lowOutTemp":         "group_temperature",
    "maxSolarRad":        "group_radiation",
    "monthRain":          "group_rain",
    "outHumidity":        "group_percent",
    "outTemp":            "group_temperature",
    "pressure":           "group_pressure",
    "pressureRate":       "group_pressurerate",
    "radiation":          "group_radiation",
    "rain":               "group_rain",
    "rain24":             "group_rain",
    "rainRate":           "group_rainrate",
    "soilMoist1":         "group_moisture",
    "soilMoist2":         "group_moisture",
    "soilTemp1":          "group_temperature",
    "soilTemp2":          "group_temperature",
    "stormRain":          "group_rain",
    "UV":                 "group_uv",
    "windchill":          "group_temperature",
    "windDir":            "group_direction",
    "windGust":           "group_speed",
    "windGustDir":        "group_direction",
    "windrun":            "group_distance",
    "windSpeed":          "group_speed",
    "yearRain":           "group_rain",
}

# ---------------------------------------------------------------------------
# Standard unit system definitions (mirrors weewx stdTypes)
# ---------------------------------------------------------------------------

US_UNITS: dict[str, str] = {
    "group_altitude":    "foot",
    "group_direction":   "degree_compass",
    "group_distance":    "mile",
    "group_moisture":    "centibar",
    "group_percent":     "percent",
    "group_pressure":    "inHg",
    "group_pressurerate": "inHg_per_hour",
    "group_radiation":   "watt_per_meter_squared",
    "group_rain":        "inch",
    "group_rainrate":    "inch_per_hour",
    "group_speed":       "mile_per_hour",
    "group_speed2":      "mile_per_hour2",
    "group_temperature": "degree_F",
    "group_uv":          "uv_index",
    "group_volt":        "volt",
}

METRIC_UNITS: dict[str, str] = {
    "group_altitude":    "meter",
    "group_direction":   "degree_compass",
    "group_distance":    "km",
    "group_moisture":    "centibar",
    "group_percent":     "percent",
    "group_pressure":    "mbar",
    "group_pressurerate": "mbar_per_hour",
    "group_radiation":   "watt_per_meter_squared",
    "group_rain":        "cm",
    "group_rainrate":    "cm_per_hour",
    "group_speed":       "km_per_hour",
    "group_speed2":      "km_per_hour2",
    "group_temperature": "degree_C",
    "group_uv":          "uv_index",
    "group_volt":        "volt",
}

METRICWX_UNITS: dict[str, str] = {
    **METRIC_UNITS,
    "group_rain":        "mm",
    "group_rainrate":    "mm_per_hour",
    "group_speed":       "meter_per_second",
    "group_speed2":      "meter_per_second2",
}

# System-code → unit dict. Codes match weewx weewxd.py constants.
UNIT_SYSTEMS: dict[int, dict[str, str]] = {
    1:  US_UNITS,
    16: METRIC_UNITS,
    17: METRICWX_UNITS,
}


def get_source_unit(obs_name: str, us_units: int) -> str | None:
    """Return the source unit for *obs_name* in system *us_units*.

    Returns None if the observation is unknown or its group has no entry in
    the requested unit system (e.g. pass-through groups like group_uv).
    """
    group = OBS_GROUP.get(obs_name)
    if not group:
        return None
    system = UNIT_SYSTEMS.get(us_units)
    if not system:
        return None
    return system.get(group)
