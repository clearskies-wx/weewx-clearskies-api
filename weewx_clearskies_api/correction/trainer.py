"""Forecast correction model trainer (ADR-079, Phase 3).

Trains a Random Forest regression model to predict the systematic temperature
bias at this weather station, then serializes it alongside the imputation
medians needed at prediction time.

Public API
----------
FEATURE_COLUMNS     Ordered list of 7 feature names (import this in corrector.py).
train_model(settings) -> dict
    Run a full training cycle: purge old data, load training/validation sets,
    check the min_samples gate, fit the model, compute TruScore metrics,
    atomically write the model file, update model_metadata in SQLite, and
    return a result dict.

The returned dict always has at minimum:
    success (bool)
    message (str)
    sample_count (int)

On success it also contains:
    mae_raw, mae_corrected, provider_score, correction_pct  (all float)

Error handling
--------------
Exceptions raised inside train_model() are caught, the training_status in
SQLite is set to "failed", and the exception is re-raised so the caller
(BackgroundRetrainer or the /setup/forecast-correction/retrain endpoint) can
log and surface it appropriately.

Atomic file write
-----------------
The model is written to a temp file in the same directory as the target path,
then moved into place with os.replace().  os.replace() is atomic on POSIX
(same-filesystem move) and also atomic on Windows for same-filesystem moves.
A concurrent forecast request reading the model file therefore either reads
the prior complete model or the new complete model — never a partial write.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import RandomForestRegressor

import joblib

from weewx_clearskies_api.correction import db as correction_db
from weewx_clearskies_api.config.settings import ForecastCorrectionSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature column ordering — MUST be identical between training and prediction.
# corrector.py imports this constant to guarantee the same ordering.
# ---------------------------------------------------------------------------

FEATURE_COLUMNS: list[str] = [
    "month",
    "hour",
    "fcst_temp",
    "fcst_wind_dir",
    "fcst_humidity",
    "fcst_cloud_cover",
    "fcst_wind_speed",
]

# Nullable features that require median imputation.
_NULLABLE_FEATURES: list[str] = [
    "fcst_wind_dir",
    "fcst_humidity",
    "fcst_cloud_cover",
    "fcst_wind_speed",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_medians(rows: list[dict]) -> dict[str, float]:
    """Compute per-column medians from a list of row dicts (training set only).

    Only the four nullable features are processed; the three non-nullable
    features (month, hour, fcst_temp) are never None in the DB schema.

    Args:
        rows: List of row dicts from get_training_data() or get_validation_data().

    Returns:
        Dict mapping column name -> float median.  Falls back to 0.0 when a
        column has no non-None values (e.g. a provider that never sends windDir).
    """
    medians: dict[str, float] = {}
    for col in _NULLABLE_FEATURES:
        values = [r[col] for r in rows if r[col] is not None]
        medians[col] = float(np.median(values)) if values else 0.0
    return medians


def _build_feature_matrix(
    rows: list[dict],
    medians: dict[str, float],
) -> np.ndarray:
    """Build a 2-D numpy feature matrix from a list of row dicts.

    Feature order follows FEATURE_COLUMNS exactly.  None values in nullable
    columns are replaced with the corresponding pre-computed median.

    Args:
        rows:    Row dicts (from DB CRUD helpers).
        medians: Median dict from _compute_medians() (computed on training set).

    Returns:
        numpy array of shape (len(rows), len(FEATURE_COLUMNS)).
    """
    matrix = []
    for r in rows:
        row_values = []
        for col in FEATURE_COLUMNS:
            val = r[col]
            if val is None:
                val = medians.get(col, 0.0)
            row_values.append(float(val))
        matrix.append(row_values)
    return np.array(matrix, dtype=np.float64)


def _build_target_vector(rows: list[dict]) -> np.ndarray:
    """Build target vector y = actual_temp - fcst_temp (bias) from row dicts.

    Args:
        rows: Row dicts from get_training_data() or get_validation_data().

    Returns:
        numpy array of shape (len(rows),).
    """
    return np.array(
        [float(r["actual_temp"]) - float(r["fcst_temp"]) for r in rows],
        dtype=np.float64,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def train_model(settings: ForecastCorrectionSettings) -> dict:
    """Run a full model training cycle.

    Steps (per plan Task 3.1):
    1. Purge records older than retention_years.
    2. Load training data (all pairs older than 30 days).
    3. Load validation data (pairs from the last 30 days).
    4. Check min_samples gate — return early if insufficient.
    5. Build feature matrix X and target vector y from training set.
    6. Compute median imputation values from training set.
    7. Fit RandomForestRegressor.
    8. Compute TruScore metrics on validation set.
    9. Atomically write model + medians to model_path via joblib.
    10. Update model_metadata in SQLite.
    11. Return success dict with all metrics.

    On any unhandled exception, training_status is set to "failed" in
    model_metadata and the exception is re-raised.

    Args:
        settings: ForecastCorrectionSettings instance from config.

    Returns:
        Dict with keys: success, message, sample_count, and (on success)
        mae_raw, mae_corrected, provider_score, correction_pct.
    """
    now = datetime.now(timezone.utc).timestamp()

    # ------------------------------------------------------------------
    # Step 1: Purge records older than retention_years
    # ------------------------------------------------------------------
    before_epoch = int(now - settings.retention_years * 365.25 * 86400)
    deleted = correction_db.purge_old_records(before_epoch)
    logger.info(
        "Training: purged %d records older than retention window (%d years)",
        deleted,
        settings.retention_years,
    )

    # ------------------------------------------------------------------
    # Step 2 & 3: Load training / validation data
    # ------------------------------------------------------------------
    cutoff_epoch = int(now - 30 * 86400)  # 30-day validation window
    training_data = correction_db.get_training_data(cutoff_epoch)
    validation_data = correction_db.get_validation_data(cutoff_epoch)

    logger.info(
        "Training: %d training pairs, %d validation pairs (cutoff epoch %d)",
        len(training_data),
        len(validation_data),
        cutoff_epoch,
    )

    # ------------------------------------------------------------------
    # Step 4: Bootstrap detection + min_samples gate
    # ------------------------------------------------------------------
    # A freshly deployed system has all pairs within the last 30 days,
    # so the training set is empty while the validation set has everything.
    # In this bootstrap phase, use all available data for both training
    # and validation so the operator can train a first model immediately.
    total_pairs = len(training_data) + len(validation_data)

    if not training_data and validation_data:
        logger.info(
            "Training: bootstrap mode — all %d pairs are within the last "
            "30 days. Using all data for both training and validation.",
            len(validation_data),
        )
        training_data = validation_data

    if not validation_data and training_data:
        logger.info(
            "Training: bootstrap mode — validation set is empty (all %d "
            "pairs older than 30 days). Using training data as validation.",
            len(training_data),
        )
        validation_data = training_data

    if len(training_data) < settings.min_samples:
        logger.warning(
            "Training: insufficient data (%d total pairs, need %d). Skipping.",
            total_pairs,
            settings.min_samples,
        )
        return {
            "success": False,
            "message": (
                f"Insufficient training data: {total_pairs} pairs, "
                f"need {settings.min_samples}"
            ),
            "sample_count": total_pairs,
        }

    try:
        # ------------------------------------------------------------------
        # Step 5 & 6: Feature matrix + median imputation
        # ------------------------------------------------------------------
        medians = _compute_medians(training_data)
        logger.debug("Training: feature medians = %s", medians)

        X_train = _build_feature_matrix(training_data, medians)
        y_train = _build_target_vector(training_data)

        X_val = _build_feature_matrix(validation_data, medians)
        # Actual temps and forecast temps for MAE computation.
        actual_val = np.array(
            [float(r["actual_temp"]) for r in validation_data], dtype=np.float64
        )
        fcst_val = np.array(
            [float(r["fcst_temp"]) for r in validation_data], dtype=np.float64
        )

        # ------------------------------------------------------------------
        # Step 7: Fit Random Forest
        # ------------------------------------------------------------------
        model = RandomForestRegressor(
            n_estimators=150,
            max_depth=6,
            random_state=42,
        )
        model.fit(X_train, y_train)
        logger.info(
            "Training: RandomForestRegressor fitted on %d pairs.", len(training_data)
        )

        # ------------------------------------------------------------------
        # Step 8: TruScore metrics on validation set
        # ------------------------------------------------------------------
        predicted_bias = model.predict(X_val)

        mae_raw = float(np.mean(np.abs(actual_val - fcst_val)))
        mae_corrected = float(
            np.mean(np.abs(actual_val - (fcst_val + predicted_bias)))
        )
        provider_score = 100.0 - mae_raw
        correction_pct = max(
            0.0,
            (mae_raw - mae_corrected) / mae_raw * 100.0 if mae_raw > 0.0 else 0.0,
        )

        logger.info(
            "Training: MAE_raw=%.4f, MAE_corrected=%.4f, "
            "ProviderScore=%.2f, CorrectionPct=%.2f%%",
            mae_raw,
            mae_corrected,
            provider_score,
            correction_pct,
        )

        # ------------------------------------------------------------------
        # Step 9: Atomic model serialization
        # ------------------------------------------------------------------
        model_data = {
            "model": model,
            "feature_medians": medians,
            "feature_columns": FEATURE_COLUMNS,
        }

        dir_name = os.path.dirname(settings.model_path)
        if dir_name and not os.path.isdir(dir_name):
            os.makedirs(dir_name, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=dir_name or ".", suffix=".pkl.tmp")
        os.close(fd)
        try:
            joblib.dump(model_data, tmp_path)
            os.replace(tmp_path, settings.model_path)
            logger.info("Training: model written atomically to %s", settings.model_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        # ------------------------------------------------------------------
        # Step 10: Persist metrics to model_metadata
        # ------------------------------------------------------------------
        correction_db.save_model_metadata(
            last_trained=datetime.now(timezone.utc).isoformat(),
            sample_count=len(training_data),
            mae_raw=round(mae_raw, 4),
            mae_corrected=round(mae_corrected, 4),
            provider_score=round(provider_score, 2),
            correction_pct=round(correction_pct, 2),
            model_path=settings.model_path,
            training_status="idle",
        )

        # ------------------------------------------------------------------
        # Step 11: Return success dict
        # ------------------------------------------------------------------
        return {
            "success": True,
            "message": "Model trained successfully",
            "sample_count": len(training_data),
            "mae_raw": round(mae_raw, 4),
            "mae_corrected": round(mae_corrected, 4),
            "provider_score": round(provider_score, 2),
            "correction_pct": round(correction_pct, 2),
        }

    except Exception:
        # Persist "failed" status so the admin status endpoint reflects the
        # failure without requiring the caller to know the DB schema.
        try:
            correction_db.save_model_metadata(
                last_trained=None,
                sample_count=len(training_data),
                mae_raw=None,
                mae_corrected=None,
                provider_score=None,
                correction_pct=None,
                model_path=settings.model_path,
                training_status="failed",
            )
        except Exception as meta_exc:  # noqa: BLE001 — best-effort metadata update
            logger.warning(
                "Training: could not persist 'failed' status to metadata: %s",
                meta_exc,
            )
        raise
