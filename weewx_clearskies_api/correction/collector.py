"""Forecast correction data collector (ADR-079, Phase 2).

Background daemon thread that collects forecast-observation pairs once per
archive interval.  Each tick queries the latest archive record for the actual
observed temperature, retrieves the current cached forecast bundle, finds the
hourly point closest to the archive timestamp, and writes the resulting pair
to the correction SQLite database via correction/db.py.

The collector is READ-ONLY toward the weewx archive DB — only SELECT queries
are issued.  All writes go to the separate correction DB managed by
correction/db.py.

Thread pattern: follows BackgroundCacheWarmer (services/cache_warmer.py L73-183)
exactly.  _stop_event is a threading.Event; _loop uses wait(timeout=...) so
stop() is responsive without busy-polling.

Provider dispatch: calls the configured provider's fetch() function directly.
fetch() is cache-first — if the cache is warm (cache warmer runs every 5 min),
no outbound API call is made.  Each provider has a different signature; this
module mirrors the dispatch in endpoints/forecast.py.

All SQL for archive queries uses text() with :param bind parameters per
coding.md §1.  No f-strings in queries.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from weewx_clearskies_api.correction import db as correction_db

logger = logging.getLogger(__name__)


class ForecastCollector:
    """Background daemon thread that collects forecast-observation pairs (ADR-079).

    Per tick (once per archive_interval):
    1. Query the latest archive record for outTemp + dateTime
    2. Get the current cached forecast bundle via the configured provider's fetch()
    3. Find the hourly point closest to the archive timestamp
    4. Extract features and write the pair to the correction DB

    Args:
        engine: SQLAlchemy Engine for weewx archive DB (read-only).
        settings: ForecastCorrectionSettings from api.conf [forecast_correction].
        forecast_settings: ForecastSettings (for provider_id and credentials).
        station_info: StationInfo (lat, lon, timezone, etc.).
        archive_interval: Archive interval in seconds (e.g. 300 = 5 min).
    """

    def __init__(
        self,
        engine: Engine,
        settings: object,       # ForecastCorrectionSettings — avoid circular import
        forecast_settings: object,  # ForecastSettings — avoid circular import
        station_info: object,   # StationInfo — avoid circular import
        archive_interval: int,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._forecast_settings = forecast_settings
        self._station = station_info
        self._interval = archive_interval
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background daemon thread."""
        t = threading.Thread(target=self._loop, daemon=True, name="forecast-collector")
        t.start()
        logger.info("Forecast collector: background thread started (interval=%ds)", self._interval)

    def stop(self) -> None:
        """Signal the background thread to exit at next tick."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop: collect once per archive_interval, or until stopped."""
        while not self._stop_event.wait(timeout=self._interval):
            try:
                self._collect_one()
            except Exception:
                # Broad catch: collector must never crash the daemon thread.
                # Each tick is independent — a failure here does not affect
                # the next tick or any other part of the API.
                logger.warning(
                    "Forecast collector: unexpected error during tick; skipping",
                    exc_info=True,
                )

        logger.info("Forecast collector: background thread stopped")

    # ------------------------------------------------------------------
    # Per-tick logic
    # ------------------------------------------------------------------

    def _collect_one(self) -> None:
        """Execute a single collection tick.

        Steps:
        1. Query latest archive record (outTemp + dateTime).
        2. Fetch cached forecast bundle for the configured provider.
        3. Find the hourly point closest to the archive timestamp.
        4. Extract feature columns and write pair to correction DB.
        """
        # Step 1: Query latest archive record (read-only SELECT).
        archive_ts, actual_temp = self._query_latest_archive()
        if archive_ts is None:
            logger.debug("Forecast collector: no archive records found; skipping tick")
            return
        if actual_temp is None:
            logger.debug(
                "Forecast collector: latest archive record has null outTemp at %d; skipping tick",
                archive_ts,
            )
            return

        # Step 2: Fetch forecast bundle (cache-first — no API call if cache is warm).
        bundle = self._fetch_forecast_bundle()
        if bundle is None:
            return
        if not bundle.hourly:
            logger.debug("Forecast collector: forecast bundle has no hourly points; skipping tick")
            return

        # Step 3: Find the hourly point whose validTime is closest to archive_ts.
        closest_point = self._find_closest_hourly(bundle.hourly, archive_ts)
        if closest_point is None:
            logger.debug("Forecast collector: no parseable hourly points; skipping tick")
            return

        fcst_temp = closest_point.outTemp
        if fcst_temp is None:
            logger.debug("Forecast collector: matched hourly point has null outTemp; skipping tick")
            return

        # Step 4: Extract time features in station timezone.
        archive_dt_utc = datetime.fromtimestamp(archive_ts, tz=timezone.utc)
        try:
            station_tz = ZoneInfo(self._station.timezone)
            archive_dt_local = archive_dt_utc.astimezone(station_tz)
        except Exception:
            # ZoneInfo failure is extremely unlikely (timezone validated at startup).
            # Fall back to UTC so we still write the pair.
            logger.warning(
                "Forecast collector: failed to apply station timezone %r; using UTC",
                self._station.timezone,
                exc_info=True,
            )
            archive_dt_local = archive_dt_utc

        month = archive_dt_local.month
        hour = archive_dt_local.hour
        day_of_year = archive_dt_local.timetuple().tm_yday

        provider_id = self._forecast_settings.provider  # guaranteed non-None by wiring guard

        # Step 5: Write pair to correction DB (INSERT OR IGNORE handles duplicates).
        correction_db.insert_pair(
            timestamp=archive_ts,
            provider_id=provider_id,
            month=month,
            hour=hour,
            day_of_year=day_of_year,
            fcst_temp=fcst_temp,
            fcst_wind_dir=closest_point.windDir,
            fcst_humidity=closest_point.outHumidity,
            fcst_cloud_cover=closest_point.cloudCover,
            fcst_wind_speed=closest_point.windSpeed,
            actual_temp=actual_temp,
        )
        logger.debug(
            "Forecast collector: stored pair ts=%d fcst=%.1f actual=%.1f provider=%s",
            archive_ts,
            fcst_temp,
            actual_temp,
            provider_id,
        )

    # ------------------------------------------------------------------
    # Archive query helper (READ-ONLY)
    # ------------------------------------------------------------------

    def _query_latest_archive(self) -> tuple[int | None, float | None]:
        """SELECT the most recent archive record.

        Returns:
            (dateTime as int epoch, outTemp as float|None).
            Returns (None, None) when the table is empty.

        All SQL is parameterised via text().  No f-strings in queries.
        The archive engine is opened read-only; no commits are issued here.
        """
        sql = text(
            "SELECT dateTime, outTemp FROM archive ORDER BY dateTime DESC LIMIT 1"
        )
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql)
                row = result.fetchone()
        except Exception:
            logger.warning(
                "Forecast collector: archive DB query failed; skipping tick",
                exc_info=True,
            )
            return (None, None)

        if row is None:
            return (None, None)

        archive_ts = int(row[0]) if row[0] is not None else None
        actual_temp = float(row[1]) if row[1] is not None else None
        return (archive_ts, actual_temp)

    # ------------------------------------------------------------------
    # Forecast bundle fetch helper
    # ------------------------------------------------------------------

    def _fetch_forecast_bundle(self) -> object | None:
        """Fetch the current forecast bundle via the configured provider.

        Uses the provider's fetch() function directly; fetch() is cache-first
        so if the cache warmer has populated the cache no outbound API call
        is made.

        Returns:
            ForecastBundle, or None if the fetch fails or provider is unknown.
        """
        provider_id = self._forecast_settings.provider
        lat = self._station.latitude
        lon = self._station.longitude
        tz = self._station.timezone
        # Correction engine works on raw values from the station's native unit
        # system, which is whatever the cache warmer/endpoint uses.  We match
        # the station's configured unit system so the cached bundle is reused.
        # station_info does not carry target_unit; use the station's unit_system
        # directly.  The unit_system attribute is "us" / "metric" / "metricwx"
        # (lowercase from weewx.conf) — map to canonical target_unit strings.
        raw_unit = getattr(self._station, "unit_system", "us").lower()
        _unit_map = {"us": "US", "metric": "METRIC", "metricwx": "METRICWX"}
        target_unit = _unit_map.get(raw_unit, "US")

        try:
            if provider_id == "openmeteo":
                from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

                return openmeteo.fetch(
                    lat=lat,
                    lon=lon,
                    target_unit=target_unit,
                    timezone=tz,
                )
            elif provider_id == "nws":
                from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415

                return forecast_nws.fetch(
                    lat=lat,
                    lon=lon,
                    target_unit=target_unit,
                    user_agent_contact=self._forecast_settings.nws_user_agent_contact,
                )
            elif provider_id == "aeris":
                from weewx_clearskies_api.providers.forecast import aeris  # noqa: PLC0415

                return aeris.fetch(
                    lat=lat,
                    lon=lon,
                    target_unit=target_unit,
                    client_id=self._forecast_settings.aeris_client_id,
                    client_secret=self._forecast_settings.aeris_client_secret,
                    forecast_model=self._forecast_settings.aeris_forecast_model,
                )
            elif provider_id == "openweathermap":
                from weewx_clearskies_api.providers.forecast import openweathermap  # noqa: PLC0415

                return openweathermap.fetch(
                    lat=lat,
                    lon=lon,
                    target_unit=target_unit,
                    appid=self._forecast_settings.openweathermap_appid,
                )
            elif provider_id == "wunderground":
                from weewx_clearskies_api.providers.forecast import wunderground  # noqa: PLC0415

                return wunderground.fetch(
                    lat=lat,
                    lon=lon,
                    target_unit=target_unit,
                    api_key=self._forecast_settings.wunderground_api_key,
                    pws_station_id=self._forecast_settings.wunderground_pws_station_id,
                )
            else:
                logger.warning(
                    "Forecast collector: unknown provider %r; cannot fetch bundle",
                    provider_id,
                )
                return None
        except Exception:
            logger.warning(
                "Forecast collector: provider fetch failed for %r; skipping tick",
                provider_id,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Closest-hourly-point helper
    # ------------------------------------------------------------------

    @staticmethod
    def _find_closest_hourly(hourly_points: list, archive_ts: int) -> object | None:
        """Find the hourly forecast point whose validTime is closest to archive_ts.

        validTime is a UTC ISO-8601 string with Z suffix (e.g. "2026-06-30T14:00:00Z").
        datetime.fromisoformat() handles the Z suffix in Python 3.11+; for 3.10
        compatibility the Z is replaced with +00:00 before parsing.

        Args:
            hourly_points: List of HourlyForecastPoint instances.
            archive_ts: Unix epoch integer from the archive record.

        Returns:
            The HourlyForecastPoint with the smallest |validTime_epoch - archive_ts|,
            or None if no points have a parseable validTime.
        """
        best_point = None
        best_delta = None

        for point in hourly_points:
            valid_time = point.validTime
            if not valid_time:
                continue
            try:
                # Replace Z suffix for Python 3.10 compatibility.
                iso_str = valid_time.replace("Z", "+00:00")
                point_dt = datetime.fromisoformat(iso_str)
                point_epoch = int(point_dt.timestamp())
            except (ValueError, TypeError):
                logger.debug(
                    "Forecast collector: could not parse validTime %r; skipping point",
                    valid_time,
                )
                continue

            delta = abs(point_epoch - archive_ts)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_point = point

        return best_point
