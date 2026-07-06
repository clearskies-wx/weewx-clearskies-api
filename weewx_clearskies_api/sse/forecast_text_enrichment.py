"""Forecast text enrichment adapter for GET /api/v1/forecast (ADR-082 T7.1).

Bridges the GFE forecast text engine (``sse/gfe``) into the ``/forecast``
response pipeline. Aggregates hourly provider data into day/night
``ForecastPeriod`` instances, generates NWS-style narrative text per period
via :func:`sse.gfe.generate_forecast_text`, and attaches the composed text
to the corresponding ``DailyForecastPoint`` as ``forecastText``.

NWS pass-through (API-MANUAL.md SS15, PROVIDER-MANUAL.md SS4): NWS already
supplies its own human-written ``detailedForecast`` text, mapped into
``DailyForecastPoint.narrative`` by the NWS provider module at fetch time.
The GFE text engine is not invoked for that provider -- this adapter leaves
the bundle untouched when ``source == "nws"``.
"""

from __future__ import annotations

import logging
from typing import Any

from weewx_clearskies_api.sse.gfe import aggregate_periods, generate_forecast_text
from weewx_clearskies_api.sse.gfe import composer as _composer

logger = logging.getLogger(__name__)


def enrich_forecast_text(bundle: dict[str, Any], locale: str) -> dict[str, Any]:
    """Attach GFE-generated forecast text to a forecast response bundle.

    Args:
        bundle: A forecast bundle envelope carrying (at minimum):

            - ``source``: provider_id string (``"nws"``, ``"openmeteo"``, ...).
            - ``hourly``: ``list[HourlyForecastPoint]``, any order (re-sorted
              by :func:`sse.period_aggregator.aggregate_periods`).
            - ``daily``: ``list[DailyForecastPoint]``, chronological by
              ``validDate``; mutated in place to set ``forecastText``.
            - ``sunrise`` / ``sunset``: UTC ISO-8601 timestamps, or ``None``.
            - ``current_time``: aware ``datetime``, the "now" reference used
              for Today/Tonight/Tomorrow period labeling.
            - ``timezone``: IANA timezone name (e.g. ``"America/New_York"``).

        locale: Operator's configured ``default_locale`` (API-MANUAL.md SS6).

    Returns:
        The same *bundle* dict. Each daily point that has a corresponding
        GFE-composed period gets ``forecastText`` populated; others are left
        at their default ``None``. Never raises -- any failure is logged and
        the bundle is returned unchanged for the affected/all periods.

    NWS pass-through: when ``bundle["source"] == "nws"``, returns *bundle*
    unchanged. NWS's own ``detailedForecast`` text already lives in
    ``DailyForecastPoint.narrative`` -- the GFE engine is not invoked.

    Empty/missing hourly or daily data: returns *bundle* unchanged (no
    crash); ``forecastText`` stays at its default ``None`` on every point.

    Period-to-date matching: ``ForecastPeriod`` (``sse/forecast_model.py``)
    carries no date field -- only ``period_label`` ("Today"/"Tonight"/
    "Tomorrow"/weekday) and ``is_daytime``. Day-bucket periods
    (``is_daytime=True``) are taken in chronological order and matched
    *positionally* to the chronologically-ordered ``daily`` list (1st day
    period -> ``daily[0]``, 2nd -> ``daily[1]``, ...). When a matching
    night-bucket period exists at the same position, its text is appended
    after the day text, joined by ``"\\n"``, so ``forecastText`` carries
    both halves of that calendar date's forecast. Approved by the lead
    2026-07-05 (T7.1 scope round) as the pragmatic approach that avoids
    extending ``ForecastPeriod``/``period_aggregator.py`` with a date field.
    """
    try:
        if bundle.get("source") == "nws":
            return bundle

        hourly = bundle.get("hourly") or []
        daily = bundle.get("daily") or []
        if not hourly or not daily:
            return bundle

        current_time = bundle.get("current_time")
        timezone = bundle.get("timezone")
        if current_time is None or not timezone:
            logger.debug(
                "forecast_text_enrichment: missing current_time/timezone; skipping"
            )
            return bundle

        periods = aggregate_periods(
            hourly,
            bundle.get("sunrise"),
            bundle.get("sunset"),
            current_time,
            timezone,
            locale,
            unit_system=_composer._unit_system,
        )
        if not periods:
            return bundle

        day_periods = [p for p in periods if p.is_daytime]
        night_periods = [p for p in periods if not p.is_daytime]

        for index, daily_point in enumerate(daily):
            if index >= len(day_periods):
                break

            day_text = generate_forecast_text(day_periods[index], locale) or ""
            night_text = ""
            if index < len(night_periods):
                night_text = generate_forecast_text(night_periods[index], locale) or ""

            parts = [text for text in (day_text, night_text) if text]
            if parts:
                daily_point.forecastText = "\n".join(parts)

    except Exception:  # noqa: BLE001
        logger.exception("forecast_text_enrichment failed")

    return bundle
