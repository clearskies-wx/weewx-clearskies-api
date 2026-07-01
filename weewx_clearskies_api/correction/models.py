"""Pydantic models for forecast correction admin endpoints (ADR-079).

These models are used by the three admin endpoints in endpoints/setup.py
(Phase 5):
  GET  /setup/forecast-correction/status  → CorrectionStatusResponse
  POST /setup/forecast-correction/toggle  ← CorrectionToggleRequest
                                          → CorrectionToggleResponse
  POST /setup/forecast-correction/retrain → RetrainResponse

Conventions (per API-MANUAL.md §14 and §2):
- Request models use ConfigDict(extra="forbid") to reject unknown fields.
- Response models do not forbid extra (responses are producer-controlled).
- Setup endpoints omit freshness (admin actions, not cacheable data responses).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CorrectionStatusResponse(BaseModel):
    """Response body for GET /setup/forecast-correction/status."""

    #: Whether a trained model file is present and loadable.
    model_available: bool
    #: True when enabled=True AND a model is loaded (correction is actually running).
    is_active: bool
    #: Whether correction is enabled in settings (may be True with no model yet).
    enabled: bool
    #: Whether the background collector is accumulating pairs.
    collection_enabled: bool
    #: Configured retrain schedule: "weekly" | "daily" | "manual".
    retrain_schedule: str
    #: Total forecast-observation pairs stored in the correction DB.
    pair_count: int
    #: Earliest pair timestamp (Unix epoch), or None when no pairs exist.
    date_range_start: int | None
    #: Latest pair timestamp (Unix epoch), or None when no pairs exist.
    date_range_end: int | None
    #: ISO-8601 UTC string of the last training run, or None if never trained.
    last_trained: str | None
    #: Number of pairs used in the last training run.
    sample_count: int | None
    #: Mean absolute error of raw forecasts on the 30-day validation set.
    mae_raw: float | None
    #: Mean absolute error of corrected forecasts on the 30-day validation set.
    mae_corrected: float | None
    #: Provider Score = 100 − MAE_raw (higher = more accurate raw forecast).
    provider_score: float | None
    #: Correction Score = 100 − MAE_corrected (same scale as provider_score; higher = better).
    correction_score: float | None
    #: Current training state from model_metadata: "idle" | "training" | "failed".
    training_status: str | None


class CorrectionToggleRequest(BaseModel):
    """Request body for POST /setup/forecast-correction/toggle.

    At least one field should be provided; both may be set in one call.
    Passing neither field is a no-op (returns current state unchanged).
    """

    model_config = ConfigDict(extra="forbid")

    #: When provided, sets whether correction is applied to forecast temps.
    enabled: bool | None = None
    #: When provided, sets whether the background collector accumulates pairs.
    collection_enabled: bool | None = None


class CorrectionToggleResponse(BaseModel):
    """Response body for POST /setup/forecast-correction/toggle."""

    #: New value of the enabled flag after the toggle operation.
    enabled: bool
    #: New value of the collection_enabled flag after the toggle operation.
    collection_enabled: bool


class RetrainResponse(BaseModel):
    """Response body for POST /setup/forecast-correction/retrain.

    Returns success=False (not HTTP error) when the min_samples gate is not
    met — this is a normal operational state, not an error condition.
    """

    #: True when training completed successfully; False when gate not met or training failed.
    success: bool
    #: Human-readable message describing the outcome.
    message: str
    #: Number of pairs in the training set used for this run (None on failure).
    sample_count: int | None = None
    #: MAE of raw forecasts on the validation set after this training run.
    mae_raw: float | None = None
    #: MAE of corrected forecasts on the validation set after this training run.
    mae_corrected: float | None = None
    #: Provider Score = 100 − MAE_raw computed for this run.
    provider_score: float | None = None
    #: Correction Score = 100 − MAE_corrected for this run (same scale as provider_score).
    correction_score: float | None = None
