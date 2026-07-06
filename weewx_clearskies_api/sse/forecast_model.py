"""Structured forecast period model (ADR-070 T2.2).

Structured intermediate representation for one day/night forecast period,
analogous to observation_model.Observation for current conditions.  Populated
from the daily/hourly forecast enrichment pipeline before text generation.
All fields are nullable except period_label and is_daytime.

Module provides a single public data type: ForecastPeriod.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ForecastPeriod:
    """Structured snapshot of one day/night forecast period.

    Pure data carrier consumed by the phrase generators.  All fields are
    nullable except period_label and is_daytime, which are required inputs
    describing which period this instance represents.
    """

    # Period identity
    period_label: str                 # "Today", "Tonight", "Tomorrow", weekday name
    is_daytime: bool                  # True = day (6am-6pm), False = night

    # Temperature group. NOTE: NOT guaranteed °F, unlike Observation
    # (observation_model.py). Forecast providers fetch hourly/daily data
    # already converted to the operator's configured target_unit (see
    # providers/forecast/*.py), and period_aggregator.py aggregates those
    # values as-is with no conversion back -- so these fields carry
    # whatever unit the operator configured (°F for US, °C for
    # METRIC/METRICWX). sse/gfe/composer.py's compose_forecast_text()
    # accounts for this at render time (see its module docstring
    # "Forecast-side unit rendering").
    temp_high: float | None = None    # day periods
    temp_low: float | None = None     # night periods

    # Sky condition
    sky_label: str | None = None      # from 6-bucket table (e.g. "partly_cloudy")
    sky_percent: float | None = None  # 0-100 cloud cover

    # Precipitation group
    pop: float | None = None                  # 0-100 probability of precipitation
    precip_type: str | None = None            # "rain", "snow", "freezing-rain", etc.
    precip_coverage: str | None = None        # derived from PoP per ADR-082 table

    # Wind group. NOT guaranteed mph -- see the temperature group note
    # above; providers deliver windSpeed/windGust in the operator's
    # target_unit (mph/km-h/m-s). wind_direction is always degrees (0-360),
    # unaffected by target_unit.
    wind_speed_min: float | None = None
    wind_speed_max: float | None = None
    wind_gust: float | None = None
    wind_direction: float | None = None  # degrees (0-360)

    # Weather codes — union of hourly codes covering this period
    weather_codes: list[str] = field(default_factory=list)

    # Snow / ice accumulation. NOT guaranteed inches -- same target_unit
    # caveat as the temperature/wind groups above (mm for METRIC/METRICWX).
    # Unlike temperature/wind, sse/gfe/composer.py does NOT currently
    # correct for this -- see its module docstring "Forecast-side unit
    # rendering" known-gap note.
    snow_amount: float | None = None       # inches (US) / mm (METRIC, METRICWX)
    ice_accumulation: float | None = None  # inches (US) / mm (METRIC, METRICWX); from Xweather daily

    # Humidity group
    humidity_max: float | None = None  # %
    humidity_min: float | None = None  # %

    # Feels-like group (from hourly feelsLike). Same target_unit caveat as
    # the temperature group above.
    feels_like_max: float | None = None
    feels_like_min: float | None = None

    # Thunderstorm risk
    thunder_risk: float | None = None  # 0-100 or None

    # Temperature trend across the period
    temp_trend: str | None = None  # "falling" | "rising" | None
