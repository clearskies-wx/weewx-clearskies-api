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


def correct_bundle(bundle):
    """Apply temperature correction to all hourly forecast points.

    Iterates ``bundle.hourly``.  For each point:
    1. Skips points where ``outTemp`` is None.
    2. Parses ``validTime`` to extract month and hour features.
    3. Extracts all 7 features in FEATURE_COLUMNS order, substituting stored
       medians for any None values in the four nullable feature columns.
    4. Calls ``model.predict()`` to get the predicted temperature bias.
    5. Applies: ``point.outTemp = round(point.outTemp + predicted_bias, 1)``.

    Daily points are untouched.  No-op when ``is_active()`` returns False.
    The bundle is mutated in place — this function is called post-cache so the
    mutation does not affect the cached payload.

    Performance: <5 ms for 168 hourly points (single sklearn RF predict call
    per point; numpy array construction is vectorised per-point).

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

        # Parse validTime (UTC ISO-8601 with Z) for month/hour features.
        try:
            iso_str = point.validTime.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
        except (ValueError, TypeError, AttributeError):
            logger.debug(
                "corrector: could not parse validTime %r; skipping point.", point.validTime
            )
            continue

        # Build feature vector in FEATURE_COLUMNS order.
        features: list[float] = []
        for col in FEATURE_COLUMNS:
            if col == "month":
                features.append(float(dt.month))
            elif col == "hour":
                features.append(float(dt.hour))
            elif col == "fcst_temp":
                features.append(float(point.outTemp))
            elif col == "fcst_wind_dir":
                val = point.windDir
                features.append(
                    float(val) if val is not None
                    else _feature_medians.get("fcst_wind_dir", 0.0)  # type: ignore[union-attr]
                )
            elif col == "fcst_humidity":
                val = point.outHumidity
                features.append(
                    float(val) if val is not None
                    else _feature_medians.get("fcst_humidity", 0.0)  # type: ignore[union-attr]
                )
            elif col == "fcst_cloud_cover":
                val = point.cloudCover
                features.append(
                    float(val) if val is not None
                    else _feature_medians.get("fcst_cloud_cover", 0.0)  # type: ignore[union-attr]
                )
            elif col == "fcst_wind_speed":
                val = point.windSpeed
                features.append(
                    float(val) if val is not None
                    else _feature_medians.get("fcst_wind_speed", 0.0)  # type: ignore[union-attr]
                )

        # Predict bias and apply correction.
        X = np.array([features], dtype=np.float64)
        predicted_bias = _model.predict(X)[0]  # type: ignore[union-attr]
        point.outTemp = round(point.outTemp + predicted_bias, 1)

    return bundle
