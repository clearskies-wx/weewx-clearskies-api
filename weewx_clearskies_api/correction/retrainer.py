"""Background scheduled retrainer for the forecast correction engine (ADR-079).

Runs as a daemon thread.  Checks once per hour whether it is time to retrain
the correction model based on the operator-configured schedule:

  weekly  — retrain on retrain_day (0=Mon…6=Sun) at 03:xx station time.
  daily   — retrain every day at 03:xx station time.
  manual  — never auto-retrains; operator triggers via
            POST /setup/forecast-correction/retrain.

Thread pattern: follows BackgroundCacheWarmer (services/cache_warmer.py)
exactly.  _stop_event.wait(timeout=3600) drives the hourly tick; stop()
sets the event so the thread exits cleanly at next tick.

Duplicate-run guard: _last_retrain_date tracks the station-local date of the
most recent retrain.  If the thread wakes during the same calendar day as the
last retrain (e.g. after a brief service restart) it skips the run.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, date

logger = logging.getLogger(__name__)


class BackgroundRetrainer:
    """Daemon thread that retrains the correction model on schedule (ADR-079).

    Checks once per hour whether it is time to retrain:
      - weekly: retrain_day at ~03:00 station time
      - daily: ~03:00 station time every day
      - manual: never auto-retrains (endpoint only)

    Args:
        settings: ForecastCorrectionSettings instance from config.
        station_timezone: IANA timezone string (e.g. "America/Chicago").
    """

    def __init__(self, settings: object, station_timezone: str) -> None:
        self._settings = settings
        self._station_tz = station_timezone
        self._stop_event = threading.Event()
        self._last_retrain_date: date | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background daemon thread."""
        t = threading.Thread(
            target=self._loop, daemon=True, name="forecast-retrainer"
        )
        t.start()
        logger.info(
            "Forecast retrainer: background thread started (schedule=%s)",
            self._settings.retrain_schedule,  # type: ignore[union-attr]
        )

    def stop(self) -> None:
        """Signal the background thread to exit at next tick."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop: wake once per hour, check whether a retrain is due."""
        while not self._stop_event.wait(timeout=3600):
            try:
                self._maybe_retrain()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Forecast retrainer: error during scheduled check",
                    exc_info=True,
                )

        logger.info("Forecast retrainer: background thread stopped")

    # ------------------------------------------------------------------
    # Retrain logic
    # ------------------------------------------------------------------

    def _maybe_retrain(self) -> None:
        """Check whether a retrain is due and run it if so."""
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        now_local = datetime.now(tz=ZoneInfo(self._station_tz))

        # Only retrain in the 03:xx hour (03:00–03:59) station time.
        if now_local.hour != 3:
            return

        today = now_local.date()
        if self._last_retrain_date == today:
            # Already retrained today — skip.
            return

        schedule = self._settings.retrain_schedule  # type: ignore[union-attr]
        if schedule == "manual":
            return
        elif schedule == "daily":
            pass  # always retrain at 03:xx
        elif schedule == "weekly":
            if now_local.weekday() != self._settings.retrain_day:  # type: ignore[union-attr]
                return
        else:
            logger.warning(
                "Forecast retrainer: unknown schedule %r; skipping retrain",
                schedule,
            )
            return

        # Time to retrain.
        from weewx_clearskies_api.correction.trainer import train_model  # noqa: PLC0415
        from weewx_clearskies_api.correction.corrector import reload_model  # noqa: PLC0415

        logger.info(
            "Forecast retrainer: starting scheduled retrain (schedule=%s)", schedule
        )
        try:
            result = train_model(self._settings)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.warning(
                "Forecast retrainer: train_model raised an exception; "
                "training_status set to 'failed' in DB",
                exc_info=True,
            )
            self._last_retrain_date = today
            return

        if result.get("success"):
            reload_model()
            logger.info(
                "Forecast retrainer: retrain complete — "
                "MAE_raw=%.2f, MAE_corrected=%.2f",
                result.get("mae_raw", 0),
                result.get("mae_corrected", 0),
            )
        else:
            logger.info(
                "Forecast retrainer: retrain skipped — %s",
                result.get("message", "unknown"),
            )

        self._last_retrain_date = today
