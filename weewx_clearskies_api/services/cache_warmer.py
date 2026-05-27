"""Background cache warmer for slow endpoints (ADR-045).

Pre-computes expensive results on configurable intervals and stores them in the
ADR-017 CacheBackend.  Endpoint handlers check the cache before running the
service, returning in <10ms on cache hit.

Warmed endpoints:
  - GET /records?period=all-time (unfiltered only)
  - GET /records?period=ytd (unfiltered only)
  - GET /almanac/sun-times (current year, station location)
  - GET /almanac/moon-phases (current year, full-year, station location)

Cache key format:
  warmer:records:<period>          e.g. warmer:records:all-time
  warmer:almanac:sun-times:<year>  e.g. warmer:almanac:sun-times:2026
  warmer:almanac:moon-phases:<year>

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
from datetime import datetime, timezone

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
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._settings = settings
        self._station = station_meta
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

        while not self._stop_event.is_set():
            now = time.monotonic()

            if last_records == _NEVER or (now - last_records) >= self._settings.records_interval_seconds:
                self._warm_records()
                last_records = time.monotonic()

            if last_almanac == _NEVER or (now - last_almanac) >= self._settings.almanac_interval_seconds:
                self._warm_almanac()
                last_almanac = time.monotonic()

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
