"""NWS GFE Text Generation System with WorldCast Technology.

Ports the National Weather Service's Graphical Forecast Editor (GFE) text
formatter algorithms and threshold tables for use in the Clear Skies
forecast and current-conditions text engines (ADR-082).

Source: `Unidata/awips2` (https://github.com/Unidata/awips2), a public
domain US government work (17 USC S105). See
`docs/reference/nws-text-system/gfe-source-code-analysis.md` for the full
source analysis this package is ported from.

WorldCast refers to the i18n expansion of the GFE vocabulary beyond its
original French/Spanish support to 13 locales (not a legal brand).

Public API (T5.2): this module is the single import surface consumers use —
callers should not need to reach into `sse.gfe.composer` or
`sse.period_aggregator` directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from weewx_clearskies_api.sse.gfe.composer import (
    compose_current_text,
    compose_forecast_text,
    compose_nws_passthrough,
    configure,
)

if TYPE_CHECKING:
    from weewx_clearskies_api.models.responses import HourlyForecastPoint
    from weewx_clearskies_api.sse.forecast_model import ForecastPeriod
    from weewx_clearskies_api.sse.observation_model import Observation

__all__ = [
    "aggregate_periods",
    "compose_current_text",
    "compose_forecast_text",
    "compose_nws_passthrough",
    "configure",
    "generate_current_text",
    "generate_forecast_text",
]


def generate_forecast_text(period: ForecastPeriod, locale: str) -> str:
    """Generate the composed NWS-style forecast text for one period.

    Thin public wrapper over :func:`composer.compose_forecast_text` — the
    stable entry point downstream forecast-enrichment code should import.
    """
    return compose_forecast_text(period, locale)


def generate_current_text(obs: Observation, verbosity: str, locale: str) -> str:
    """Generate current-conditions text for one Observation.

    Thin public wrapper over :func:`composer.compose_current_text` — the
    stable entry point downstream current-conditions enrichment code
    (`sse/enrichment/weather_text.py`) should import. Current-conditions
    detection/composition systems are preserved per ADR-082 settled
    decision #12; only the standard/verbose temperature and wind phrase
    content is upgraded (see `composer.py` module docstring). The terse
    tier is not handled here — it remains
    `sse.conditions_text.build_weather_text()`.
    """
    return compose_current_text(obs, verbosity, locale)


def aggregate_periods(
    hourly_data: list[HourlyForecastPoint],
    sunrise: str | None,
    sunset: str | None,
    current_time: datetime,
    timezone: str,
    locale: str = "en",
) -> list[ForecastPeriod]:
    """Aggregate hourly forecast points into day/night ForecastPeriod instances.

    Thin public wrapper over :func:`sse.period_aggregator.aggregate_periods`
    — the stable entry point downstream forecast-enrichment code should
    import instead of reaching into the `period_aggregator` module directly.
    """
    from weewx_clearskies_api.sse.period_aggregator import (  # noqa: PLC0415
        aggregate_periods as _agg,
    )

    return _agg(hourly_data, sunrise, sunset, current_time, timezone, locale)
