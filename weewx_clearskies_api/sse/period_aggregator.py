"""Hourly-to-period forecast aggregation (ADR-070 Phase 3 T3.1).

Aggregates canonical `HourlyForecastPoint` records into day/night
`ForecastPeriod` instances using the NWS Graphical Forecast Editor (GFE)
6am/6pm local-time period convention (settled decision #1). Consumed by the
Phase 3+ forecast text pipeline as the intermediate representation between
raw hourly forecast data and the phrase generators.

Period boundaries are fixed at 6am/6pm local time regardless of actual
sunrise/sunset. Sunrise/sunset are used ONLY to select day/night vocabulary
(e.g. "Sunny" vs "Clear" sky labels) via the `is_daytime` flag on each
period — never to move the period boundaries themselves.

Resolved divergences from the original task brief (confirmed with the lead
2026-07-05, T3.1/T3.2 round):
  - `precip_coverage` always uses `POP_TO_COVERAGE_TABLE`'s
    `pop_related_coverage` column. The brief's proposed
    `WX_POP_RELATED_TYPES` check (GFE codes: "R", "S", "T", ...) cannot
    apply — canonical `HourlyForecastPoint.precipType` is the word-enum
    "rain" | "snow" | "freezing-rain" | "sleet", never a GFE code, and none
    of those four values are areal (fog/haze/smoke) types, so
    `pop_related_coverage` is always the correct column at this layer.
  - `thunder_risk` is always `None` from this module. `HourlyForecastPoint`
    has no `thunderRisk` field (only `DailyForecastPoint` does), and this
    function intentionally only takes hourly input. If thunderstorm risk
    needs to flow into `ForecastPeriod` later, it must be merged in from
    daily data at a higher layer (e.g. `forecast_text_enrichment.py`), not
    reconstructed here.
  - No weather-code-based thunderstorm heuristic is used. `weatherCode` is
    documented as opaque to the API (raw WMO code, provider-specific) and
    this module does not interpret it.

Module provides a single public function: aggregate_periods().
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from weewx_clearskies_api.models.responses import HourlyForecastPoint
from weewx_clearskies_api.sse.forecast_model import ForecastPeriod
from weewx_clearskies_api.sse.gfe.sky_phrases import sky_key
from weewx_clearskies_api.sse.gfe.thresholds import (
    POP_TO_COVERAGE_TABLE,
    TEMP_TREND_THRESHOLD,
)

_DAY_START_HOUR = 6   # 6am local — start of the "day" period bucket
_DAY_END_HOUR = 18    # 6pm local — start of the "night" period bucket

_WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]


def _parse_utc_iso(value: str | None) -> datetime | None:
    """Parse a UTC ISO-8601 string (with trailing 'Z') to an aware datetime."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mode(values: list) -> object | None:  # type: ignore[type-arg]
    """Return the most frequent value, first-seen order breaking ties.

    Returns None when `values` is empty. Non-None inputs only — callers
    filter None out before calling this helper.
    """
    if not values:
        return None
    counts = Counter(values)
    best_count = max(counts.values())
    for v in values:
        if counts[v] == best_count:
            return v
    return None  # unreachable, kept for type-checker clarity


def _skip_none(values: list) -> list:  # type: ignore[type-arg]
    return [v for v in values if v is not None]


def _sky_label(sky_percent: float | None, is_daytime: bool) -> str | None:
    """Return the locale-independent sky-coverage key for *sky_percent*.

    Delegates the 6-bucket GFE lookup to
    :func:`weewx_clearskies_api.sse.gfe.sky_phrases.sky_key` — the single
    shared implementation of `SKY_VALUE_LIST` bucket resolution (FC-3
    audit finding). This module only adds the ``None``-passthrough for
    absent cloud cover data.
    """
    if sky_percent is None:
        return None
    return sky_key(sky_percent, is_daytime)


def _precip_coverage(pop: float | None) -> str | None:
    if pop is None:
        return None
    for band in POP_TO_COVERAGE_TABLE:
        if band.lower_pct <= pop <= band.upper_pct:
            return band.pop_related_coverage
    return None


def _round_to_45(deg: float) -> float:
    return (round(deg / 45.0) * 45.0) % 360.0


def _period_bucket(local_dt: datetime) -> tuple[date, bool]:
    """Return (period_date, is_day_bucket) for a local-time hourly point.

    Day bucket: 06:00-17:59 local, keyed on that calendar date.
    Night bucket: 18:00-05:59 local. Hours >= 18:00 key on that calendar
    date (the evening the night period starts); hours < 06:00 key on the
    PREVIOUS calendar date (they belong to the night period that started
    the evening before).
    """
    hour = local_dt.hour
    if _DAY_START_HOUR <= hour < _DAY_END_HOUR:
        return local_dt.date(), True
    if hour >= _DAY_END_HOUR:
        return local_dt.date(), False
    return local_dt.date() - timedelta(days=1), False


def _period_bounds(
    period_date: date, is_day_bucket: bool, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    if is_day_bucket:
        start = datetime.combine(period_date, time(_DAY_START_HOUR, 0), tzinfo=tz)
        end = datetime.combine(period_date, time(_DAY_END_HOUR, 0), tzinfo=tz)
    else:
        start = datetime.combine(period_date, time(_DAY_END_HOUR, 0), tzinfo=tz)
        end = datetime.combine(period_date + timedelta(days=1), time(_DAY_START_HOUR, 0), tzinfo=tz)
    return start, end


def _resolve_is_daytime(
    period_date: date,
    is_day_bucket: bool,
    period_start: datetime,
    period_end: datetime,
    sunrise_local: datetime | None,
    sunset_local: datetime | None,
) -> bool:
    """Resolve the vocabulary is_daytime flag for a period.

    Defaults to the period bucket type (day bucket -> True, night bucket ->
    False). When sunrise/sunset are available, overrides the default by
    checking whether more than half of the period's local-clock window
    overlaps actual daylight — using the given sunrise/sunset TIME-OF-DAY
    projected onto this period's own calendar date (the caller only
    supplies one sunrise/sunset pair per call, not one per forecast day).
    """
    default = is_day_bucket
    if sunrise_local is None or sunset_local is None:
        return default

    sunrise_time = sunrise_local.timetz()
    sunset_time = sunset_local.timetz()

    if is_day_bucket:
        day_sunrise = datetime.combine(period_date, sunrise_time)
        day_sunset = datetime.combine(period_date, sunset_time)
    else:
        # Night period: sunset falls on the evening it starts; sunrise
        # falls the following morning.
        day_sunset = datetime.combine(period_date, sunset_time)
        day_sunrise = datetime.combine(period_date + timedelta(days=1), sunrise_time)

    overlap_start = max(period_start, day_sunrise)
    overlap_end = min(period_end, day_sunset)
    overlap = max(timedelta(0), overlap_end - overlap_start)
    duration = period_end - period_start
    if duration.total_seconds() <= 0:
        return default
    return (overlap.total_seconds() / duration.total_seconds()) > 0.5


def _period_label(period_date: date, is_day_bucket: bool, current_local_date: date) -> str:
    delta_days = (period_date - current_local_date).days
    if delta_days == 0:
        return "Today" if is_day_bucket else "Tonight"
    if delta_days == 1:
        return "Tomorrow" if is_day_bucket else "Tomorrow Night"
    weekday = _WEEKDAYS[period_date.weekday()]
    return weekday if is_day_bucket else f"{weekday} Night"


def _temp_trend(
    outtemps_chronological: list[float | None],
    is_day_bucket: bool,
    extreme: float | None,
) -> str | None:
    if extreme is None:
        return None
    non_none = [(i, t) for i, t in enumerate(outtemps_chronological) if t is not None]
    if len(non_none) < 2:
        return None
    latter_half = non_none[len(non_none) // 2 :]
    latter_temps = [t for _, t in latter_half]
    if is_day_bucket:
        max_diff = max((extreme - t) for t in latter_temps)
        if max_diff > TEMP_TREND_THRESHOLD:
            return "falling"
    else:
        max_diff = max((t - extreme) for t in latter_temps)
        if max_diff > TEMP_TREND_THRESHOLD:
            return "rising"
    return None


def aggregate_periods(
    hourly: list[HourlyForecastPoint],
    sunrise: str | None,
    sunset: str | None,
    current_time: datetime,
    timezone: str,
    locale: str = "en",
) -> list[ForecastPeriod]:
    """Aggregate hourly forecast points into day/night ForecastPeriod instances.

    Args:
        hourly: Canonical hourly forecast points, any order (re-sorted here
            by validTime).
        sunrise: UTC ISO-8601 sunrise timestamp (one value; used only for
            the day/night vocabulary flag on periods sharing that calendar
            date), or None.
        sunset: UTC ISO-8601 sunset timestamp, or None.
        current_time: Aware datetime used as the "now" reference for
            Today/Tonight/Tomorrow labeling.
        timezone: IANA timezone name (e.g. "America/New_York") used to
            convert validTime/sunrise/sunset to local wall-clock time.
        locale: Reserved for future localized period labels; unused today
            (period labels are the literal English strings from the T3.1
            brief — "Today", "Tonight", "Tomorrow", weekday names).

    Returns:
        ForecastPeriod instances in chronological order, one per 6am/6pm
        local-time bucket spanned by `hourly`. Empty list when `hourly` is
        empty.
    """
    if not hourly:
        return []

    tz = ZoneInfo(timezone)
    sunrise_local = _parse_utc_iso(sunrise)
    sunset_local = _parse_utc_iso(sunset)
    if sunrise_local is not None:
        sunrise_local = sunrise_local.astimezone(tz)
    if sunset_local is not None:
        sunset_local = sunset_local.astimezone(tz)
    current_local_date = current_time.astimezone(tz).date()

    # Group hourly points by (period_date, is_day_bucket), preserving
    # chronological order within each group.
    sorted_hourly = sorted(hourly, key=lambda p: p.validTime)
    groups: dict[tuple[date, bool], list[HourlyForecastPoint]] = {}
    for point in sorted_hourly:
        parsed = _parse_utc_iso(point.validTime)
        if parsed is None:
            continue
        local_dt = parsed.astimezone(tz)
        key = _period_bucket(local_dt)
        groups.setdefault(key, []).append(point)

    periods: list[ForecastPeriod] = []
    for (period_date, is_day_bucket) in sorted(
        groups.keys(), key=lambda k: _period_bounds(k[0], k[1], tz)[0]
    ):
        points = groups[(period_date, is_day_bucket)]
        period_start, period_end = _period_bounds(period_date, is_day_bucket, tz)
        is_daytime = _resolve_is_daytime(
            period_date, is_day_bucket, period_start, period_end, sunrise_local, sunset_local
        )

        outtemps = [p.outTemp for p in points]
        outtemps_present = _skip_none(outtemps)
        temp_high = max(outtemps_present) if is_day_bucket and outtemps_present else None
        temp_low = min(outtemps_present) if not is_day_bucket and outtemps_present else None

        cloud_covers = _skip_none([p.cloudCover for p in points])
        sky_percent = mean(cloud_covers) if cloud_covers else None
        sky_label = _sky_label(sky_percent, is_daytime)

        pops = _skip_none([p.precipProbability for p in points])
        pop = max(pops) if pops else None
        precip_coverage = _precip_coverage(pop)

        precip_types = _skip_none([p.precipType for p in points])
        precip_type = _mode(precip_types)

        wind_speeds = _skip_none([p.windSpeed for p in points])
        wind_speed_min = min(wind_speeds) if wind_speeds else None
        wind_speed_max = max(wind_speeds) if wind_speeds else None

        wind_gusts = _skip_none([p.windGust for p in points])
        wind_gust = max(wind_gusts) if wind_gusts else None

        wind_dirs = _skip_none([p.windDir for p in points])
        wind_direction = (
            _mode([_round_to_45(d) for d in wind_dirs]) if wind_dirs else None
        )

        weather_codes = list(
            dict.fromkeys(p.weatherCode for p in points if p.weatherCode is not None)
        )

        snow_amounts = _skip_none(
            [p.precipAmount for p in points if p.precipType == "snow"]
        )
        snow_amount = sum(snow_amounts) if snow_amounts else None

        humidities = _skip_none([p.outHumidity for p in points])
        humidity_max = max(humidities) if humidities else None
        humidity_min = min(humidities) if humidities else None

        feels_likes = _skip_none([p.feelsLike for p in points])
        feels_like_max = max(feels_likes) if feels_likes else None
        feels_like_min = min(feels_likes) if feels_likes else None

        extreme = temp_high if is_day_bucket else temp_low
        temp_trend = _temp_trend(outtemps, is_day_bucket, extreme)

        label = _period_label(period_date, is_day_bucket, current_local_date)

        periods.append(
            ForecastPeriod(
                period_label=label,
                is_daytime=is_daytime,
                temp_high=temp_high,
                temp_low=temp_low,
                sky_label=sky_label,
                sky_percent=sky_percent,
                pop=pop,
                precip_type=precip_type,
                precip_coverage=precip_coverage,
                wind_speed_min=wind_speed_min,
                wind_speed_max=wind_speed_max,
                wind_gust=wind_gust,
                wind_direction=wind_direction,
                weather_codes=weather_codes,
                snow_amount=snow_amount,
                ice_accumulation=None,
                humidity_max=humidity_max,
                humidity_min=humidity_min,
                feels_like_max=feels_like_max,
                feels_like_min=feels_like_min,
                thunder_risk=None,
                temp_trend=temp_trend,
            )
        )

    return periods
