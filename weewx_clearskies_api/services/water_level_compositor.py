"""Water level compositor — CO-OPS prediction + OFS non-tidal residual (ADR-091).

Combines CO-OPS harmonic tide predictions (accurate for astronomical tides)
with the OFS non-tidal residual (meteorological signal: storm surge, wind
setup, atmospheric pressure) to produce a composite total water level
forecast.

Algorithm (TIDE-ACCURACY-BRIEF.md):
  1. Observed residual = CO-OPS observation - interpolated CO-OPS prediction
  2. Current residual = most recent observed residual
  3. Forecast residual:
     - OFS available: ofs_residual = ofs_zeta - coops_prediction_at_same_time
       bias_corrected = ofs_residual + (current_observed_residual - ofs_residual_now)
     - OFS unavailable: persistence fallback with exponential decay (tau=12h)
  4. Total water level = prediction + corrected_residual

Storm surge classification per absolute residual:
  < 0.15 ft: null (normal)
  0.15-0.5 ft: "elevated" or "depressed"
  0.5-1.0 ft: "significant"
  > 1.0 ft: "storm_surge"
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from weewx_clearskies_api.units.conversion import convert

logger = logging.getLogger(__name__)

_PERSISTENCE_TAU_HOURS = 12.0

_SURGE_THRESHOLDS_FT = {
    "minor": 0.15,
    "moderate": 0.5,
    "major": 1.0,
}


def compute_composite(
    predictions: list[dict[str, Any]],
    observations: list[dict[str, Any]] | None,
    ofs_water_levels: list[dict[str, Any]] | None,
    now: datetime,
    target_unit: str = "foot",
) -> dict[str, Any]:
    """Compute composite water level from predictions + observations + OFS.

    Args:
        predictions: CO-OPS harmonic predictions [{time: iso, height: float (meters), ...}]
        observations: CO-OPS observed water levels [{time: iso, height: float (meters), ...}]
        ofs_water_levels: OFS model total water level [{time: iso, height: float (meters)}]
        now: Current UTC time.
        target_unit: Operator display unit for water level heights.

    Returns dict with:
        currentResidual: {value, quality, source, description} or None
        totalWaterLevelForecast: [{time, height, residual}] or None
        stormSurgeLevel: str or None
        residualForecastSource: "ofs:{model}" or "persistence" or "unavailable"
    """
    result: dict[str, Any] = {
        "currentResidual": None,
        "totalWaterLevelForecast": None,
        "stormSurgeLevel": None,
        "residualForecastSource": "unavailable",
    }

    if not predictions:
        return result

    # Step 1: Compute observed residuals
    observed_residuals: list[tuple[datetime, float]] = []
    if observations:
        for obs in observations:
            obs_time = _parse_time(obs.get("time"))
            obs_height = obs.get("height")
            if obs_time is None or obs_height is None:
                continue
            pred_at_obs = _interpolate_prediction(predictions, obs_time)
            if pred_at_obs is not None:
                residual = obs_height - pred_at_obs
                observed_residuals.append((obs_time, residual))

    # Step 2: Current residual (most recent observed)
    current_residual_m: float | None = None
    residual_quality = "unavailable"
    if observed_residuals:
        observed_residuals.sort(key=lambda x: x[0], reverse=True)
        most_recent_time, most_recent_residual = observed_residuals[0]
        age_hours = (now - most_recent_time).total_seconds() / 3600
        if age_hours <= 6:
            current_residual_m = most_recent_residual
            residual_quality = "good" if age_hours <= 1 else "stale"

    if current_residual_m is not None:
        residual_ft = _meters_to_target(current_residual_m, target_unit)
        sign = "+" if residual_ft >= 0 else ""
        result["currentResidual"] = {
            "value": round(residual_ft, 2),
            "quality": residual_quality,
            "source": "coops_observed",
            "description": f"{sign}{residual_ft:.2f} {_unit_label(target_unit)} vs prediction",
        }

        # Storm surge classification
        result["stormSurgeLevel"] = _classify_surge(abs(residual_ft), residual_ft)

    # Step 3 + 4: Forecast residual + total water level
    forecast_points: list[dict[str, Any]] = []
    forecast_source = "unavailable"

    if ofs_water_levels and current_residual_m is not None:
        # Bias-corrected OFS forecast residual
        ofs_residual_now = _compute_ofs_residual_at_time(
            ofs_water_levels, predictions, now
        )
        bias = (current_residual_m - ofs_residual_now) if ofs_residual_now is not None else 0.0

        for ofs_point in ofs_water_levels:
            ofs_time = _parse_time(ofs_point.get("time"))
            ofs_height = ofs_point.get("height")
            if ofs_time is None or ofs_height is None:
                continue
            if ofs_time <= now:
                continue

            pred_at_ofs = _interpolate_prediction(predictions, ofs_time)
            if pred_at_ofs is None:
                continue

            ofs_residual = ofs_height - pred_at_ofs
            corrected_residual = ofs_residual + bias
            total = pred_at_ofs + corrected_residual

            forecast_points.append({
                "time": ofs_time.isoformat(),
                "height": round(_meters_to_target(total, target_unit), 2),
                "residual": round(_meters_to_target(corrected_residual, target_unit), 2),
            })
        forecast_source = "ofs"

    elif current_residual_m is not None:
        # Persistence fallback: decay current residual exponentially
        for pred in predictions:
            pred_time = _parse_time(pred.get("time"))
            pred_height = pred.get("height")
            if pred_time is None or pred_height is None or pred_time <= now:
                continue

            dt_hours = (pred_time - now).total_seconds() / 3600
            decayed_residual = current_residual_m * math.exp(-dt_hours / _PERSISTENCE_TAU_HOURS)
            total = pred_height + decayed_residual

            forecast_points.append({
                "time": pred_time.isoformat(),
                "height": round(_meters_to_target(total, target_unit), 2),
                "residual": round(_meters_to_target(decayed_residual, target_unit), 2),
            })
        forecast_source = "persistence"

    if forecast_points:
        result["totalWaterLevelForecast"] = forecast_points
        result["residualForecastSource"] = forecast_source

    return result


def _parse_time(time_str: str | None) -> datetime | None:
    if not time_str:
        return None
    try:
        from datetime import timezone

        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        return datetime.fromisoformat(time_str).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _interpolate_prediction(
    predictions: list[dict[str, Any]], target_time: datetime
) -> float | None:
    """Linear interpolation of CO-OPS prediction heights at an arbitrary time."""
    parsed: list[tuple[datetime, float]] = []
    for p in predictions:
        t = _parse_time(p.get("time"))
        h = p.get("height")
        if t is not None and h is not None:
            parsed.append((t, h))

    if not parsed:
        return None

    parsed.sort(key=lambda x: x[0])

    if target_time <= parsed[0][0]:
        return parsed[0][1]
    if target_time >= parsed[-1][0]:
        return parsed[-1][1]

    for i in range(len(parsed) - 1):
        t0, h0 = parsed[i]
        t1, h1 = parsed[i + 1]
        if t0 <= target_time <= t1:
            frac = (target_time - t0).total_seconds() / (t1 - t0).total_seconds()
            return h0 + frac * (h1 - h0)

    return None


def _compute_ofs_residual_at_time(
    ofs_levels: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    target_time: datetime,
) -> float | None:
    """Compute OFS non-tidal residual at a specific time."""
    closest_ofs: tuple[float, float] | None = None
    min_dt = float("inf")

    for point in ofs_levels:
        t = _parse_time(point.get("time"))
        h = point.get("height")
        if t is None or h is None:
            continue
        dt = abs((t - target_time).total_seconds())
        if dt < min_dt:
            min_dt = dt
            closest_ofs = (dt, h)

    if closest_ofs is None or closest_ofs[0] > 7200:
        return None

    ofs_height = closest_ofs[1]
    pred_height = _interpolate_prediction(predictions, target_time)
    if pred_height is None:
        return None

    return ofs_height - pred_height


def _meters_to_target(value_m: float, target_unit: str) -> float:
    if target_unit == "meter":
        return value_m
    try:
        return convert(value_m, "meter", target_unit)
    except Exception:
        return value_m


def _unit_label(unit: str) -> str:
    return {"foot": "ft", "meter": "m"}.get(unit, unit)


def _classify_surge(abs_residual_ft: float, signed_residual_ft: float) -> str | None:
    if abs_residual_ft < _SURGE_THRESHOLDS_FT["minor"]:
        return None
    if abs_residual_ft < _SURGE_THRESHOLDS_FT["moderate"]:
        return "elevated" if signed_residual_ft > 0 else "depressed"
    if abs_residual_ft < _SURGE_THRESHOLDS_FT["major"]:
        return "significant"
    return "storm_surge"
