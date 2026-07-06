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

    # Temperature group
    temp_high: float | None = None    # °F, day periods
    temp_low: float | None = None     # °F, night periods

    # Sky condition
    sky_label: str | None = None      # from 6-bucket table (e.g. "partly_cloudy")
    sky_percent: float | None = None  # 0-100 cloud cover

    # Precipitation group
    pop: float | None = None                  # 0-100 probability of precipitation
    precip_type: str | None = None            # "rain", "snow", "freezing-rain", etc.
    precip_coverage: str | None = None        # derived from PoP per ADR-082 table

    # Wind group — all in US units (mph, degrees)
    wind_speed_min: float | None = None  # mph
    wind_speed_max: float | None = None  # mph
    wind_gust: float | None = None       # mph
    wind_direction: float | None = None  # degrees (0-360)

    # Weather codes — union of hourly codes covering this period
    weather_codes: list[str] = field(default_factory=list)

    # Snow / ice accumulation
    snow_amount: float | None = None       # inches
    ice_accumulation: float | None = None  # inches (from Xweather daily)

    # Humidity group
    humidity_max: float | None = None  # %
    humidity_min: float | None = None  # %

    # Feels-like group (from hourly feelsLike)
    feels_like_max: float | None = None  # °F
    feels_like_min: float | None = None  # °F

    # Thunderstorm risk
    thunder_risk: float | None = None  # 0-100 or None

    # Temperature trend across the period
    temp_trend: str | None = None  # "falling" | "rising" | None
