"""Background cache warmer for slow endpoints (ADR-045).

Pre-computes expensive results on configurable intervals and stores them in the
ADR-017 CacheBackend.  Endpoint handlers check the cache before running the
service, returning in <10ms on cache hit.

Warmed endpoints:
  - GET /records?period=all-time (unfiltered only)
  - GET /records?period=ytd (unfiltered only)
  - GET /almanac/sun-times (current year, station location)
  - GET /almanac/moon-phases (current year, full-year, station location)
  - GET /climatology/monthly
  - GET /almanac/planets (today's date, station location)
  - GET /almanac/eclipses (rolling 1-year window from today)
  - GET /almanac/meteor-showers (rolling 1-year window from today, station location)
  - GET /earthquakes/faults (station location, configured radius)

Cache key format:
  warmer:records:<period>                e.g. warmer:records:all-time
  warmer:almanac:sun-times:<year>        e.g. warmer:almanac:sun-times:2026
  warmer:almanac:moon-phases:<year>
  warmer:climatology:monthly
  warmer:almanac:planets:<date>          e.g. warmer:almanac:planets:2026-05-27
  warmer:almanac:eclipses:<date>         e.g. warmer:almanac:eclipses:2026-05-27
  warmer:almanac:meteor-showers:<date>   e.g. warmer:almanac:meteor-showers:2026-05-27
  warmer:earthquakes:faults

Cached values are plain dicts (JSON-safe) so both MemoryCache and RedisCache
backends work correctly.  RecordsBundle.model_dump() serialises the Pydantic
model; dataclasses.asdict() serialises SunDay/MoonDay.  The endpoint handlers
reconstruct the appropriate objects from the cached dicts.

Thread safety:
  _loop() runs in a single daemon thread.  Each warm call holds a fresh
  SQLAlchemy Session (not shared with request threads).  The CacheBackend
  set() / get() implementations are already thread-safe (MemoryCache uses
  cachetools.TTLCache which is not thread-safe; however, individual dict
  assignments are atomic in CPython.  RedisCache uses the redis-py client
  which is thread-safe).

  WARNING: cachetools.TTLCache is not thread-safe per its docs; a future
  revision should add a threading.Lock around MemoryCache operations if
  multi-threaded writes become a concern.  For the single-writer pattern
  here (only the warmer writes these keys; requests only read) the risk of
  data corruption is negligible in CPython due to the GIL.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from sqlalchemy import Engine

from weewx_clearskies_api.providers._common.cache import get_cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Warm interval sleep granularity (seconds).  The loop wakes every N seconds
# to check whether any function is overdue.  Smaller = more responsive to
# stop(); larger = less CPU overhead.  10 s is a good balance.
# ---------------------------------------------------------------------------
_SLEEP_TICK_SECONDS = 10

# Sentinel value meaning "never run".
_NEVER: float = 0.0


class BackgroundCacheWarmer:
    """Pre-computes slow endpoint results and writes them to the cache.

    Args:
        engine: SQLAlchemy Engine used to create per-warm Sessions.
        registry: ColumnRegistry from schema reflection (needed by get_records).
        settings: CacheWarmerSettings from api.conf [cache_warmer].
        station_meta: Dict with station identity keys required by almanac:
            lat (float), lon (float), alt_m (float), station_tz (str).
    """

    def __init__(
        self,
        engine: Engine,
        registry: object,
        settings: object,  # CacheWarmerSettings — avoid circular import
        station_meta: dict,
        seeing_settings: object | None = None,  # SeeingSettings — optional, avoids circular import
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._settings = settings
        self._station = station_meta
        self._seeing_settings = seeing_settings
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initial_warm(self) -> None:
        """Synchronous first warm — called before the server starts.

        Runs all warm functions once.  Failures are logged as WARNING and
        do not prevent startup (non-fatal per the brief).
        """
        logger.info("Cache warmer: initial warm starting")
        self._warm_records()
        self._warm_almanac()
        self._warm_almanac_snapshot()
        self._warm_moon_names()
        self._warm_climatology()
        self._warm_planets()
        self._warm_eclipses()
        self._warm_solar_eclipses()
        self._warm_meteor_showers()
        self._warm_faults()
        self._warm_seeing_forecast()
        logger.info("Cache warmer: initial warm complete")

    def start(self) -> None:
        """Launch the background daemon thread."""
        t = threading.Thread(target=self._loop, daemon=True, name="cache-warmer")
        t.start()
        logger.info("Cache warmer: background thread started")

    def stop(self) -> None:
        """Signal the background thread to exit at next tick."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop: wake every _SLEEP_TICK_SECONDS, run overdue functions."""
        last_records: float = _NEVER
        last_almanac: float = _NEVER
        last_climatology: float = _NEVER
        last_planets: float = _NEVER
        last_eclipses: float = _NEVER
        last_meteor_showers: float = _NEVER
        last_faults: float = _NEVER
        last_seeing: float = _NEVER

        while not self._stop_event.is_set():
            now = time.monotonic()

            if last_records == _NEVER or (now - last_records) >= self._settings.records_interval_seconds:
                self._warm_records()
                last_records = time.monotonic()

            if last_almanac == _NEVER or (now - last_almanac) >= self._settings.almanac_interval_seconds:
                self._warm_almanac()
                self._warm_almanac_snapshot()
                self._warm_moon_names()
                last_almanac = time.monotonic()

            if last_climatology == _NEVER or (now - last_climatology) >= self._settings.climatology_interval_seconds:
                self._warm_climatology()
                last_climatology = time.monotonic()

            if last_planets == _NEVER or (now - last_planets) >= self._settings.planets_interval_seconds:
                self._warm_planets()
                last_planets = time.monotonic()

            if last_eclipses == _NEVER or (now - last_eclipses) >= self._settings.eclipses_interval_seconds:
                self._warm_eclipses()
                self._warm_solar_eclipses()
                last_eclipses = time.monotonic()

            if last_meteor_showers == _NEVER or (now - last_meteor_showers) >= self._settings.meteor_showers_interval_seconds:
                self._warm_meteor_showers()
                last_meteor_showers = time.monotonic()

            if last_faults == _NEVER or (now - last_faults) >= self._settings.faults_interval_seconds:
                self._warm_faults()
                last_faults = time.monotonic()

            if last_seeing == _NEVER or (now - last_seeing) >= self._settings.seeing_interval_seconds:
                self._warm_seeing_forecast()
                last_seeing = time.monotonic()

            # Sleep in small ticks so stop() is responsive.
            self._stop_event.wait(timeout=_SLEEP_TICK_SECONDS)

        logger.info("Cache warmer: background thread stopped")

    # ------------------------------------------------------------------
    # Warm functions
    # ------------------------------------------------------------------

    def _warm_records(self) -> None:
        """Warm GET /records for 'all-time' and 'ytd' periods (unfiltered)."""
        try:
            from weewx_clearskies_api.services.records import get_records

            cache = get_cache()
            with Session(self._engine) as db:
                for period in ("all-time", "ytd"):
                    bundle = get_records(db, self._registry, period, section_filter=None)
                    # model_dump() produces a plain dict that json.dumps can handle,
                    # making this compatible with both MemoryCache and RedisCache.
                    cache.set(
                        f"warmer:records:{period}",
                        bundle.model_dump(),
                        self._settings.records_interval_seconds,
                    )
            logger.info("Cache warmer: records refreshed (all-time + ytd)")
        except Exception:
            logger.warning("Cache warmer: records warm failed", exc_info=True)

    def _warm_almanac(self) -> None:
        """Warm GET /almanac/sun-times and GET /almanac/moon-phases for the current year."""
        try:
            from weewx_clearskies_api.services.almanac import (
                compute_sun_times_year,
                compute_moon_phases,
            )

            cache = get_cache()
            year = datetime.now(timezone.utc).year
            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]
            station_tz = self._station["station_tz"]

            # Sun times — list[SunDay] (Python dataclasses).
            sun_data = compute_sun_times_year(year, lat, lon, alt_m, station_tz)
            cache.set(
                f"warmer:almanac:sun-times:{year}",
                [dataclasses.asdict(d) for d in sun_data],
                self._settings.almanac_interval_seconds,
            )

            # Moon phases (full year, month=None) — list[MoonDay] (Python dataclasses).
            moon_data = compute_moon_phases(year, lat, lon, month=None, station_tz=station_tz)
            cache.set(
                f"warmer:almanac:moon-phases:{year}",
                [dataclasses.asdict(d) for d in moon_data],
                self._settings.almanac_interval_seconds,
            )

            logger.info("Cache warmer: almanac refreshed for year %d", year)
        except Exception:
            logger.warning("Cache warmer: almanac warm failed", exc_info=True)

    def _warm_almanac_snapshot(self) -> None:
        """Warm GET /almanac (daily snapshot for today and tomorrow).

        Uses station-local date so the cache key matches the endpoint's default
        (which also uses station-local date).  Near UTC midnight the UTC date
        and station-local date can diverge, causing a cache miss.

        Tomorrow is pre-warmed so the dashboard's date-switching feature gets a
        cache hit when the user pages forward to the next day.
        """
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            from weewx_clearskies_api.services.almanac import compute_almanac

            cache = get_cache()
            station_tz = self._station["station_tz"]
            try:
                zi = ZoneInfo(station_tz)
                today = datetime.now(tz=zi).date()
            except (ZoneInfoNotFoundError, KeyError):
                today = datetime.now(timezone.utc).date()
            tomorrow = today + timedelta(days=1)
            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]

            day_today = compute_almanac(today, lat, lon, alt_m, station_tz=station_tz)
            cache.set(
                f"warmer:almanac:snapshot:{today.isoformat()}",
                dataclasses.asdict(day_today),
                self._settings.almanac_interval_seconds,
            )

            # Warm tomorrow so dashboard date-switching gets a cache hit.
            day_tomorrow = compute_almanac(tomorrow, lat, lon, alt_m, station_tz=station_tz)
            cache.set(
                f"warmer:almanac:snapshot:{tomorrow.isoformat()}",
                dataclasses.asdict(day_tomorrow),
                self._settings.almanac_interval_seconds,
            )
            logger.info("Cache warmer: almanac snapshot refreshed for %s and %s", today, tomorrow)
        except Exception:
            logger.warning("Cache warmer: almanac snapshot warm failed", exc_info=True)

    def _warm_moon_names(self) -> None:
        """Warm GET /almanac/moon-names for the current year."""
        try:
            from weewx_clearskies_api.services.almanac import compute_special_moon_names

            cache = get_cache()
            year = datetime.now(timezone.utc).year
            moons = compute_special_moon_names(year)
            cache.set(
                f"warmer:almanac:moon-names:{year}",
                moons,
                self._settings.almanac_interval_seconds,
            )
            logger.info("Cache warmer: moon names refreshed for year %d", year)
        except Exception:
            logger.warning("Cache warmer: moon names warm failed", exc_info=True)

    def _warm_climatology(self) -> None:
        """Warm GET /climatology/monthly."""
        try:
            from weewx_clearskies_api.services.climatology import get_monthly_climatology

            cache = get_cache()
            with Session(self._engine) as db:
                clim_data = get_monthly_climatology(db, self._registry)
            cache.set(
                "warmer:climatology:monthly",
                clim_data,
                self._settings.climatology_interval_seconds,
            )
            logger.info("Cache warmer: climatology refreshed")
        except Exception:
            logger.warning("Cache warmer: climatology warm failed", exc_info=True)

    def _warm_planets(self) -> None:
        """Warm GET /almanac/planets for today's date at the station location."""
        try:
            from weewx_clearskies_api.services.almanac import compute_planets

            cache = get_cache()
            today = datetime.now(timezone.utc).date()
            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]
            station_tz = self._station["station_tz"]

            planets_data = compute_planets(today, lat, lon, alt_m, station_tz)
            cache.set(
                f"warmer:almanac:planets:{today.isoformat()}",
                planets_data,
                self._settings.planets_interval_seconds,
            )
            logger.info("Cache warmer: planets refreshed for %s", today.isoformat())
        except Exception:
            logger.warning("Cache warmer: planets warm failed", exc_info=True)

    def _warm_eclipses(self) -> None:
        """Warm GET /almanac/eclipses for the rolling 1-year window from today."""
        try:
            from datetime import date, timedelta
            from weewx_clearskies_api.services.almanac import compute_lunar_eclipses

            cache = get_cache()
            today = date.today()
            to_date = today + timedelta(days=365)

            eclipses_data = compute_lunar_eclipses(from_date=today, to_date=to_date)
            cache.set(
                f"warmer:almanac:eclipses:{today.isoformat()}",
                eclipses_data,
                self._settings.eclipses_interval_seconds,
            )
            logger.info("Cache warmer: eclipses refreshed (from %s)", today.isoformat())
        except Exception:
            logger.warning("Cache warmer: eclipses warm failed", exc_info=True)

    def _warm_solar_eclipses(self) -> None:
        """Warm GET /almanac/eclipses/solar from AstronomyAPI.com."""
        try:
            from datetime import date, timedelta

            import os

            from weewx_clearskies_api.services.astronomyapi_client import AstronomyApiClient

            app_id = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_ID", "").strip()
            app_secret = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_SECRET", "").strip()
            if not app_id or not app_secret:
                return  # No credentials configured — skip

            cache = get_cache()
            today = date.today()
            to_date = today + timedelta(days=1461)
            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]

            with AstronomyApiClient(app_id, app_secret) as client:
                solar_data = client.get_solar_eclipses(lat, lon, alt_m, today, to_date)

            cache.set(
                f"warmer:almanac:solar-eclipses:{today.isoformat()}",
                solar_data,
                self._settings.eclipses_interval_seconds,
            )
            logger.info("Cache warmer: solar eclipses refreshed (%d events)", len(solar_data))
        except Exception:
            logger.warning("Cache warmer: solar eclipses warm failed", exc_info=True)

    def _warm_meteor_showers(self) -> None:
        """Warm GET /almanac/meteor-showers for the rolling 1-year window from today."""
        try:
            from datetime import date, timedelta
            from weewx_clearskies_api.services.almanac import compute_meteor_showers

            cache = get_cache()
            today = date.today()
            to_date = today + timedelta(days=365)
            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]
            station_tz = self._station["station_tz"]

            showers_data = compute_meteor_showers(
                lat, lon, alt_m, station_tz,
                from_date=today, to_date=to_date,
            )
            cache.set(
                f"warmer:almanac:meteor-showers:{today.isoformat()}",
                showers_data,
                self._settings.meteor_showers_interval_seconds,
            )
            logger.info("Cache warmer: meteor showers refreshed (from %s)", today.isoformat())
        except Exception:
            logger.warning("Cache warmer: meteor showers warm failed", exc_info=True)

    def _warm_faults(self) -> None:
        """Warm GET /earthquakes/faults using the station location and configured radius."""
        try:
            from weewx_clearskies_api.services.faults import get_faults_within_radius
            from weewx_clearskies_api.endpoints.earthquakes import _default_radius_km

            cache = get_cache()
            lat = self._station["lat"]
            lon = self._station["lon"]

            faults_data = get_faults_within_radius(lat, lon, _default_radius_km)
            cache.set(
                "warmer:earthquakes:faults",
                faults_data,
                self._settings.faults_interval_seconds,
            )
            logger.info("Cache warmer: faults refreshed (radius %.1f km)", _default_radius_km)
        except Exception:
            logger.warning("Cache warmer: faults warm failed", exc_info=True)

    def _warm_seeing_forecast(self) -> None:
        """Warm GET /almanac/seeing-forecast from 7Timer."""
        if self._seeing_settings is None or self._seeing_settings.provider is None:
            return  # Seeing provider disabled or not wired

        try:
            from datetime import timedelta

            from weewx_clearskies_api.providers.seeing.seven_timer import SevenTimerProvider

            lat = self._station["lat"]
            lon = self._station["lon"]

            with SevenTimerProvider(
                base_url=self._seeing_settings.base_url,
                timeout_seconds=self._seeing_settings.timeout_seconds,
            ) as provider:
                points = provider.fetch_forecast(lat, lon)

            if not points:
                logger.info("Seeing forecast returned no data; skipping cache update")
                return

            # Derive init_time from the first point's valid_time minus 3 h.
            # 7Timer timepoints start at +3 h; the first point's valid_time - 3 h
            # gives the model initialization time.
            first_valid = points[0].valid_time
            init_time = first_valid - timedelta(hours=3)
            init_time_str = init_time.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Serialize as a plain dict for cache storage
            # (compatible with both MemoryCache and RedisCache backends).
            payload = {
                "init_time": init_time_str,
                "points": [p.model_dump(mode="json") for p in points],
            }
            cache = get_cache()
            cache.set(
                "warmer:seeing-forecast",
                payload,
                self._settings.seeing_interval_seconds,
            )
            logger.info("Cache warmer: seeing forecast refreshed (%d points)", len(points))
        except Exception:
            logger.warning("Cache warmer: seeing forecast warm failed", exc_info=True)
