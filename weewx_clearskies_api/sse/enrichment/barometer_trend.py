"""Barometer trend enrichment for GET /api/v1/current.

Computes a pressure trend by comparing the current barometer reading against
the archive record nearest to 3 hours ago.  Two fields are injected into the
/current response envelope:

* ``barometerTrend`` — numeric delta (current minus historical, rounded 3 dp)
  in whatever unit the barometer values carry.  Kept as-is for backwards
  compatibility.
* ``barometerTrendDirection`` — direction string: one of ``"rising"``,
  ``"falling"``, ``"steady"``, or ``null`` when the trend is null or the
  source unit cannot be resolved to inHg for threshold comparison.  The
  classification threshold is ±0.01 inHg applied to the delta after converting
  it to inHg using the project's unit-conversion helper (ADR-042).

Positive ``barometerTrend`` values indicate rising pressure; negative values
indicate falling.  Both fields are ``null`` when the current reading is absent,
the archive query fails, or no historical record is found within the grace
window.

Source-unit resolution order (lead-approved, 2026-05-29):
1. The transformer's configured ``group_pressure`` target unit — accessed via
   the module-level ``_transformer._targets`` when a transformer is configured.
   This is the authoritative unit because it is validated against
   ``VALID_UNITS["group_pressure"]`` at transformer construction time.
2. Fallback: ``data["units"]["barometer"]`` label stripped from the upstream
   response.  Accepted only when it is a member of
   ``_VALID_PRESSURE_UNITS`` (handles operator ``[[Labels]]`` override
   — a customised label like ``"mb"`` would not be in the valid set and is
   rejected rather than passed to convert()).
3. If neither source yields a known valid unit, ``barometerTrendDirection`` is
   ``null`` and a DEBUG log line explains why.  ``barometerTrend`` is still
   emitted.  The function never raises.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session as _Session

from weewx_clearskies_api.db.session import get_engine

logger = logging.getLogger(__name__)

# Pressure units known to the conversion registry.  Used to validate a
# candidate unit string before passing it to convert().
_VALID_PRESSURE_UNITS: frozenset[str] = frozenset({"inHg", "mbar", "hPa", "kPa"})

# Threshold in inHg for rising/falling classification (matches historical
# dashboard semantics — barometer.ts ±0.01 inHg).
_DIRECTION_THRESHOLD_INHG: float = 0.01

# Module-level transformer reference (set by configure() at startup).
_transformer = None

# Configurable time-window parameters (set by configure() at startup).
_trend_time_delta: int = 10800  # default 3 hours
_trend_time_grace: int = 300    # default 5 minutes


def configure(
    transformer: object,
    trend_time_delta: int = 10800,
    trend_time_grace: int = 300,
) -> None:
    """Set the UnitTransformer reference and trend time-window parameters.

    Called once at startup from __main__.py.

    Args:
        transformer: UnitTransformer instance for group_pressure unit resolution.
        trend_time_delta: How far back (seconds) to look for the historical reading.
        trend_time_grace: Accept a record within ±grace seconds of the target.
    """
    global _transformer, _trend_time_delta, _trend_time_grace  # noqa: PLW0603
    _transformer = transformer
    _trend_time_delta = trend_time_delta
    _trend_time_grace = trend_time_grace


def _fetch_historical_barometer(ts_from: int, ts_to: int) -> tuple[float, int] | None:
    """Get barometer reading closest to the target time window.

    Queries the archive table directly via SQLAlchemy (sync) rather than
    making an HTTP call to the upstream /archive endpoint.

    Args:
        ts_from: Lower bound of the target window (epoch seconds).
        ts_to:   Upper bound of the target window (epoch seconds).

    Returns:
        (barometer_value, timestamp_epoch) tuple, or None if no record found.
    """
    try:
        with _Session(get_engine()) as session:
            row = session.execute(
                text(
                    "SELECT barometer, dateTime FROM archive "
                    "WHERE dateTime BETWEEN :ts_from AND :ts_to "
                    "ORDER BY dateTime DESC LIMIT 1"
                ),
                {"ts_from": ts_from, "ts_to": ts_to},
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0]), int(row[1])
    except Exception:  # noqa: BLE001
        logger.warning(
            "barometer_trend: DB query failed (ts_from=%d, ts_to=%d)",
            ts_from,
            ts_to,
            exc_info=True,
        )
    return None


def _resolve_pressure_unit(data: dict[str, Any]) -> str | None:
    """Return the authoritative pressure unit for the barometer delta.

    Resolution order (per lead approval 2026-05-29):
    1. ``_transformer._targets["group_pressure"]`` — the configured
       display unit, already validated against VALID_UNITS at construction.
    2. ``data["units"]["barometer"]`` label — accepted only when it is a
       member of ``_VALID_PRESSURE_UNITS`` (rejects operator label overrides
       like ``"mb"`` that would cause convert() to raise).

    Returns a unit string (e.g. ``"inHg"``, ``"mbar"``) or ``None`` when the
    unit cannot be determined safely.  Never raises.
    """
    # Option 1: transformer's configured group_pressure target unit.
    if _transformer is not None:
        try:
            targets = getattr(_transformer, "_targets", {})
            unit = targets.get("group_pressure")
            if unit and unit in _VALID_PRESSURE_UNITS:
                return unit
        except Exception:  # noqa: BLE001
            pass

    # Option 2: units-block label from the upstream response.
    try:
        units_block = data.get("units")
        if isinstance(units_block, dict):
            label = str(units_block.get("barometer", "")).strip()
            if label in _VALID_PRESSURE_UNITS:
                return label
    except Exception:  # noqa: BLE001
        pass

    return None


def _classify_direction(trend: float, source_unit: str | None) -> str | None:
    """Return ``"rising"``, ``"falling"``, or ``"steady"`` for *trend*.

    Converts *trend* from *source_unit* to inHg before comparing against
    ``_DIRECTION_THRESHOLD_INHG``.  Returns ``None`` when *source_unit* is
    ``None`` (unit unknown) or when the conversion fails.  Never raises.
    """
    if source_unit is None:
        return None
    try:
        from weewx_clearskies_api.units.conversion import convert  # noqa: PLC0415

        trend_inhg = convert(trend, source_unit, "inHg")
        if trend_inhg is None:
            return None
        if trend_inhg > _DIRECTION_THRESHOLD_INHG:
            return "rising"
        if trend_inhg < -_DIRECTION_THRESHOLD_INHG:
            return "falling"
        return "steady"
    except Exception:  # noqa: BLE001
        return None


def _iso_to_epoch(value: str) -> int:
    """Parse a UTC ISO-8601 string (``...Z``) to integer epoch seconds."""
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _epoch_to_iso(epoch: int) -> str:
    """Format integer epoch seconds as a UTC ISO-8601 string (``...Z``)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enrich_barometer_trend(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``barometerTrend`` and ``barometerTrendDirection`` into a /current response.

    Reads the current ``barometer`` and ``timestamp`` from the observation
    sub-dict (``data["data"]``), queries the archive DB directly for the
    record nearest to 3 hours ago, computes the numeric delta, and classifies
    the direction by converting that delta to inHg before thresholding.

    Any failure (missing fields, unparseable timestamp, DB error, no record in
    grace window) results in both fields being ``null``.  The function never
    raises — GET /current must not break because of this enrichment.
    """
    # The /current response envelope shape: {data: {...obs...}, units: {...}, ...}
    obs = data.get("data")
    if not isinstance(obs, dict):
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    current_barometer = obs.get("barometer")
    timestamp = obs.get("timestamp")

    if current_barometer is None or timestamp is None:
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    try:
        current_barometer = float(current_barometer)
        ts_current = _iso_to_epoch(str(timestamp))
    except (TypeError, ValueError):
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    # Operator-configurable look-back window.  Falls back to module constants
    # (10800 / 300) when configure() has not been called.
    time_delta = _trend_time_delta
    time_grace = _trend_time_grace

    ts_historical = ts_current - time_delta

    # Query the archive DB for the record nearest to the historical target.
    # Bound the query to ±grace around the target timestamp.
    result = _fetch_historical_barometer(
        ts_from=ts_historical - time_grace,
        ts_to=ts_historical + time_grace,
    )

    if result is None:
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    historical_barometer, historical_ts = result

    # Grace-period check: reject the record if it is too far from the target.
    if abs(historical_ts - ts_historical) > time_grace:
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    trend = round(current_barometer - historical_barometer, 3)
    data["barometerTrend"] = trend

    # Classify direction: convert the raw delta to inHg before comparing to
    # the ±0.01 inHg threshold so the classification is unit-agnostic.
    source_unit = _resolve_pressure_unit(data)
    if source_unit is None:
        logger.debug(
            "barometer_trend: pressure unit unknown — barometerTrendDirection null",
            extra={"units_block": data.get("units")},
        )
    data["barometerTrendDirection"] = _classify_direction(trend, source_unit)
    return data
