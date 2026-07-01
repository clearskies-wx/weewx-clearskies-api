"""Forecast temperature corrector (ADR-079, Phase 4).

Applies a trained Random Forest bias-correction model to all hourly points in
a ForecastBundle at request time, after the bundle has been pulled from (or
written to) the forecast cache.

Module-level state is intentional — the API is a single-process service.
Call wire_corrector(settings) at startup to configure and optionally load the
model.  Call reload_model() after a successful retrain to hot-swap the model
file without restarting the process.

Public API
----------
wire_corrector(settings)    Configure from ForecastCorrectionSettings.
reload_model() -> bool      Hot-reload model from disk after retraining.
is_active() -> bool         True when enabled and a model is loaded.
set_enabled(enabled)        Runtime toggle (admin endpoint).
correct_bundle(bundle)      Apply corrections to bundle.hourly in-place.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import joblib
import numpy as np

from weewx_clearskies_api.correction.trainer import FEATURE_COLUMNS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_model = None
_enabled: bool = False
_model_path: str | None = None
_feature_medians: dict[str, float] | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_model_from_disk() -> bool:
    """Load the serialized model file from ``_model_path``.

    Reads the joblib payload, validates that its ``feature_columns`` key
    matches the canonical FEATURE_COLUMNS constant from trainer.py, and
    populates the module-level ``_model`` and ``_feature_medians`` globals.

    Returns:
        True on success, False on any failure (logged as a warning).
    """
    global _model, _feature_medians  # noqa: PLW0603

    if not _model_path:
        logger.warning("corrector: _load_model_from_disk called but _model_path is not set.")
        return False

    if not os.path.exists(_model_path):
        logger.warning(
            "corrector: model file does not exist at %s; correction disabled until retrain.",
            _model_path,
        )
        return False

    try:
        model_data = joblib.load(_model_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("corrector: failed to load model from %s: %s", _model_path, exc)
        return False

    # Validate the feature column ordering to catch a mismatch between a
    # model trained with a stale FEATURE_COLUMNS and the current definition.
    stored_columns = model_data.get("feature_columns")
    if stored_columns != FEATURE_COLUMNS:
        logger.warning(
            "corrector: model feature_columns mismatch. "
            "Stored: %s  Expected: %s  — correction disabled.",
            stored_columns,
            FEATURE_COLUMNS,
        )
        return False

    _model = model_data["model"]
    _feature_medians = model_data["feature_medians"]
    logger.info("corrector: model loaded from %s", _model_path)
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def wire_corrector(settings) -> None:
    """Configure the corrector from ForecastCorrectionSettings.

    Called once at startup (from __main__.py).  Sets ``_model_path`` and
    ``_enabled`` from the settings object.  If the model file already exists
    on disk, loads it immediately so the first forecast request benefits from
    correction without waiting for a retrain cycle.

    Args:
        settings: ForecastCorrectionSettings instance from config.
    """
    global _model_path, _enabled  # noqa: PLW0603

    _model_path = settings.model_path
    _enabled = settings.enabled

    if _enabled:
        loaded = _load_model_from_disk()
        if not loaded:
            logger.info(
                "corrector: enabled=True but no usable model found at startup. "
                "Correction will activate after the first successful retrain."
            )
    else:
        logger.debug("corrector: enabled=False at startup; skipping model load.")


def reload_model() -> bool:
    """Hot-reload the model from disk after a successful retrain.

    Called by BackgroundRetrainer (and the /setup/forecast-correction/retrain
    endpoint) after a new model file has been atomically written.  Replaces
    the in-memory model without restarting the process.

    Returns:
        True if the model was successfully loaded, False otherwise.
    """
    return _load_model_from_disk()


def is_active() -> bool:
    """Return True when correction is enabled and a model is loaded.

    Used as a fast gate in the forecast endpoint so the feature extraction
    loop is never entered unless there is something to apply.
    """
    return _enabled and _model is not None


def get_enabled() -> bool:
    """Return the current value of the enabled flag."""
    return _enabled


def set_enabled(enabled: bool) -> None:
    """Toggle correction on or off at runtime.

    Called by the admin enable/disable endpoint (ADR-079).  When re-enabled,
    does NOT automatically reload the model — ``reload_model()`` must be
    called separately if the model was not previously loaded.

    Args:
        enabled: True to enable, False to disable.
    """
    global _enabled  # noqa: PLW0603
    _enabled = enabled
    logger.info("corrector: enabled set to %s", enabled)


def _predict_bias(month: float, hour: float, fcst_temp: float, point=None) -> float:
    """Predict bias for a single temperature value.

    When ``point`` is provided, extracts windDir/humidity/cloudCover/windSpeed
    from it (hourly points carry these).  When ``point`` is None, uses stored
    medians for all four nullable features (daily points don't carry them).
    """
    features: list[float] = []
    for col in FEATURE_COLUMNS:
        if col == "month":
            features.append(month)
        elif col == "hour":
            features.append(hour)
        elif col == "fcst_temp":
            features.append(fcst_temp)
        elif col == "fcst_wind_dir":
            val = getattr(point, "windDir", None) if point else None
            features.append(
                float(val) if val is not None
                else _feature_medians.get("fcst_wind_dir", 0.0)  # type: ignore[union-attr]
            )
        elif col == "fcst_humidity":
            val = getattr(point, "outHumidity", None) if point else None
            features.append(
                float(val) if val is not None
                else _feature_medians.get("fcst_humidity", 0.0)  # type: ignore[union-attr]
            )
        elif col == "fcst_cloud_cover":
            val = getattr(point, "cloudCover", None) if point else None
            features.append(
                float(val) if val is not None
                else _feature_medians.get("fcst_cloud_cover", 0.0)  # type: ignore[union-attr]
            )
        elif col == "fcst_wind_speed":
            val = getattr(point, "windSpeed", None) if point else None
            features.append(
                float(val) if val is not None
                else _feature_medians.get("fcst_wind_speed", 0.0)  # type: ignore[union-attr]
            )

    X = np.array([features], dtype=np.float64)
    return float(_model.predict(X)[0])  # type: ignore[union-attr]


def correct_bundle(bundle):
    """Apply temperature correction to all forecast points (hourly + daily).

    **Hourly points:** For each point, extracts all 7 features from the point
    itself, predicts bias, applies ``outTemp += predicted_bias``.

    **Daily points:** For ``tempMax``, predicts bias at hour=14 (typical
    afternoon high timing).  For ``tempMin``, predicts bias at hour=5 (typical
    early morning low timing).  Weather features (wind, humidity, cloud, speed)
    use stored medians since daily points don't carry per-hour values.

    No-op when ``is_active()`` returns False.  The bundle is mutated in place —
    this function is called post-cache so the mutation does not affect the
    cached payload.

    Args:
        bundle: ForecastBundle instance (post-cache).

    Returns:
        The same bundle, mutated in place.
    """
    if not is_active():
        return bundle

    for point in bundle.hourly:
        if point.outTemp is None:
            continue

        try:
            iso_str = point.validTime.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
        except (ValueError, TypeError, AttributeError):
            logger.debug(
                "corrector: could not parse validTime %r; skipping point.", point.validTime
            )
            continue

        bias = _predict_bias(float(dt.month), float(dt.hour), float(point.outTemp), point)
        point.outTemp = round(point.outTemp + bias, 1)

    for day in bundle.daily:
        try:
            dt = datetime.strptime(day.validDate, "%Y-%m-%d")
        except (ValueError, TypeError, AttributeError):
            continue

        month = float(dt.month)

        if day.tempMax is not None:
            bias_high = _predict_bias(month, 14.0, float(day.tempMax))
            day.tempMax = round(day.tempMax + bias_high, 1)

        if day.tempMin is not None:
            bias_low = _predict_bias(month, 5.0, float(day.tempMin))
            day.tempMin = round(day.tempMin + bias_low, 1)

    return bundle
