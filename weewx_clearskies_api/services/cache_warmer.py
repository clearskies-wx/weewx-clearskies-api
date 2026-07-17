"""Background cache warmer for slow endpoints (ADR-045).

Pre-computes expensive results on configurable intervals and stores them in the
ADR-017 CacheBackend.  Endpoint handlers check the cache before running the
service, returning in <10ms on cache hit.

Warmed endpoints:
  - GET /records?period=all-time (unfiltered only)
  - GET /records?period=ytd (unfiltered only)
  - GET /almanac/sun-times (current year, station location)
  - GET /almanac/moon-phases (current year, full-year, station location)
  - GET /almanac/planets (today's date, station location)
  - GET /almanac/eclipses (rolling 1-year window from today)
  - GET /almanac/meteor-showers (rolling 1-year window from today, station location)
  - GET /earthquakes/faults (station location, configured radius)
  - Marine provider data for configured [marine] locations (Marine
    Remediation Plan T2.1/T2.2): NDBC buoy observations, CO-OPS tide
    predictions/water levels, NWS marine zone text, WaveWatch III wave
    forecasts, NWS Surf Zone Forecast (SRF), NWPS nearshore wave model.
    Deduplicated across locations by station id / grid group where the
    provider's own cache key allows it; NWS SRF and NWPS are fetched once
    per location (see `_warm_marine` docstring for why).
  - Forecast provider current conditions for marine locations, one fetch
    per unique grid group (Marine Card Data Source Fix T1.1): feeds
    `services/marine_weather_cache.py` so `/marine` card summaries have
    airTemp/windSpeed/windDirection/weatherCode/isDay for locations not
    served directly by the weewx station (see `_warm_marine_weather`
    docstring).
  - GET /forecast (station location, configured forecast provider): calls
    the provider's own fetch() so the endpoint's own call gets a cache hit
    (FIX-24; see `_warm_forecast` docstring).
  - GET /current (station location, configured forecast provider's
    fetch_current_conditions(), used for cloudcover/snow/snowRate
    blending): calls the provider's own fetch_current_conditions() so the
    endpoint's own call gets a cache hit (FIX-24; see
    `_warm_current_conditions` docstring).
  - HRRR wind field for configured marine locations (SWAN+TruShore plan
    T1.3): pre-fetches the HRRR 10m AGL wind forecast from NOMADS Grib
    Filter for a bounding box derived from all configured [marine]
    locations.  Active only when the [nearshore] pip extra is installed
    (eccodes or pygrib present, GRIB_AVAILABLE == True).  Fires at startup
    and on the hourly schedule matching the HRRR cycle cadence (3600 s).
    The HRRR provider caches internally (ADR-017, TTL 3300 s); the SWAN
    runner reads from that cache.  See `_warm_hrrr_wind` docstring.
  - SWAN+TruShore nearshore wave model (SWAN-TRUSHORE-PLAN T2.5): runs the
    SWAN wave model for all configured surf spot locations after HRRR wind
    data is warm.  Active only when the [nearshore] pip extra is installed.
    Fires at startup (after HRRR warm) and on the same hourly schedule,
    sequential after _warm_hrrr_wind() completes (never parallel).  On
    failure: logs ERROR, last-good cache is preserved indefinitely.  See
    `_warm_swan` docstring.

Cache key format:
  warmer:records:<period>                e.g. warmer:records:all-time
  warmer:almanac:sun-times:<year>        e.g. warmer:almanac:sun-times:2026
  warmer:almanac:moon-phases:<year>
  warmer:almanac:planets:<date>          e.g. warmer:almanac:planets:2026-05-27
  warmer:almanac:eclipses:<date>         e.g. warmer:almanac:eclipses:2026-05-27
  warmer:almanac:meteor-showers:<date>   e.g. warmer:almanac:meteor-showers:2026-05-27
  warmer:earthquakes:faults

  Marine warming does NOT write warmer:marine:* keys.  It calls provider
  .fetch() functions directly, which populate each provider's own internal
  ADR-017 cache (keyed per-provider — see providers/buoy/ndbc.py,
  providers/tides/coops.py, providers/marine/nws_marine.py,
  providers/marine/wavewatch.py, providers/marine/nws_srf.py,
  providers/marine/nwps.py for their respective cache key shapes).  The
  marine endpoints call the same .fetch() functions and get cache hits.

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        marine_config: object | None = None,  # MarineConfig — optional, avoids circular import
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._settings = settings
        self._station = station_meta
        self._seeing_settings = seeing_settings
        self._marine_config = marine_config
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initial_warm(self) -> None:
        """First warm — runs in the background thread, NOT blocking startup.

        Runs all warm functions once. Failures are logged as WARNING and
        do not prevent startup (non-fatal per the brief).
        """
        logger.info("Cache warmer: initial warm starting (background)")
        self._warm_records()
        self._warm_almanac()
        self._warm_almanac_snapshot()
        self._warm_moon_names()
        self._warm_planets()
        self._warm_eclipses()
        self._warm_solar_eclipses()
        self._warm_meteor_showers()
        self._warm_faults()
        self._warm_seeing_forecast()
        self._warm_marine()
        self._warm_hrrr_wind()
        self._warm_swan()   # sequential after HRRR (SWAN-TRUSHORE-PLAN T2.5)
        self._warm_forecast()
        self._warm_current_conditions()
        logger.info("Cache warmer: initial warm complete")

    def start(self) -> None:
        """Launch the background daemon thread.

        Runs initial_warm() as the first action in the thread, then enters
        the periodic loop.  The server accepts requests immediately — cache
        misses are handled gracefully by all endpoints.
        """
        t = threading.Thread(target=self._run, daemon=True, name="cache-warmer")
        t.start()
        logger.info("Cache warmer: background thread started (server accepting requests now)")

    def stop(self) -> None:
        """Signal the background thread to exit at next tick."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Thread entry point: initial warm then periodic loop."""
        self.initial_warm()
        self._loop()

    def _loop(self) -> None:
        """Main loop: wake every _SLEEP_TICK_SECONDS, run overdue functions."""
        last_records: float = _NEVER
        last_almanac: float = _NEVER
        last_planets: float = _NEVER
        last_eclipses: float = _NEVER
        last_meteor_showers: float = _NEVER
        last_faults: float = _NEVER
        last_seeing: float = _NEVER
        last_marine: float = _NEVER
        last_hrrr_wind: float = _NEVER
        last_swan: float = _NEVER  # SWAN-TRUSHORE-PLAN T2.5 — always fires after HRRR
        last_forecast: float = _NEVER
        last_current_conditions: float = _NEVER

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

            if last_marine == _NEVER or (now - last_marine) >= 1800:
                self._warm_marine()
                last_marine = time.monotonic()

            # 3600 s = HRRR cycle cadence (one model run per hour).  The HRRR
            # provider caches internally with a 3300 s TTL (PROVIDER-MANUAL
            # §14.14), so this fires just after the previous cache entry has
            # been refreshed with a new cycle.  Active only when [nearshore]
            # extra is installed; _warm_hrrr_wind() returns silently otherwise.
            # SWAN runs sequentially after HRRR (SWAN-TRUSHORE-PLAN T2.5 lead
            # call #6): never parallel, always after HRRR warm completes.
            if last_hrrr_wind == _NEVER or (now - last_hrrr_wind) >= 3600:
                self._warm_hrrr_wind()
                last_hrrr_wind = time.monotonic()
                self._warm_swan()   # sequential — HRRR must be warm first
                last_swan = time.monotonic()

            # 1500 s < the 1800 s /forecast TTL (ADR-017), leaving margin so
            # a warm cycle always lands before the cached bundle expires.
            if last_forecast == _NEVER or (now - last_forecast) >= 1500:
                self._warm_forecast()
                last_forecast = time.monotonic()

            # 240 s < the 300 s /current provider-conditions TTL (ADR-017),
            # leaving margin so a warm cycle always lands before expiry.
            if (
                last_current_conditions == _NEVER
                or (now - last_current_conditions) >= 240
            ):
                self._warm_current_conditions()
                last_current_conditions = time.monotonic()

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

    def _warm_planets(self) -> None:
        """Warm GET /almanac/planets for today's date at the station location."""
        try:
            from weewx_clearskies_api.services.almanac import compute_planets

            cache = get_cache()
            station_tz = self._station["station_tz"]
            try:
                zi = ZoneInfo(station_tz)
                today = datetime.now(tz=zi).date()
            except (ZoneInfoNotFoundError, KeyError):
                today = datetime.now(timezone.utc).date()
            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]

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
        """Warm GET /almanac/eclipses for the rolling 1-year window from today.

        Skyfield computes eclipse dates and types.  AstronomyAPI.com enriches each
        eclipse with contact times, obscuration, and visibility (same logic as the
        cache-miss path in the endpoint).  If AstronomyAPI credentials are absent or
        the enrichment call fails, the unenriched Skyfield data is cached instead so
        the endpoint always has *something* to return.
        """
        import os  # noqa: PLC0415 — deferred import to keep startup fast

        try:
            from datetime import date, timedelta
            from weewx_clearskies_api.services.almanac import compute_lunar_eclipses

            today = date.today()
            to_date = today + timedelta(days=3652)

            eclipses_data = compute_lunar_eclipses(from_date=today, to_date=to_date)
        except Exception:
            logger.warning("Cache warmer: eclipses warm failed", exc_info=True)
            return

        cache = get_cache()

        # ------------------------------------------------------------------
        # AstronomyAPI enrichment — contact times + local visibility.
        # Mirrors the endpoint's enrichment logic (almanac.py lines 569-633).
        # Failures fall back to caching unenriched Skyfield data.
        # ------------------------------------------------------------------
        app_id = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_ID", "").strip()
        app_secret = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            # Credentials not configured — cache unenriched Skyfield data.
            cache.set(
                f"warmer:almanac:eclipses:{today.isoformat()}",
                eclipses_data,
                self._settings.eclipses_interval_seconds,
            )
            logger.info(
                "Cache warmer: eclipses refreshed (from %s, no AstronomyAPI credentials)",
                today.isoformat(),
            )
            return

        try:
            from datetime import timedelta as _td
            from weewx_clearskies_api.services.astronomyapi_client import AstronomyApiClient

            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]

            with AstronomyApiClient(app_id, app_secret) as client:
                api_eclipses = client.get_lunar_eclipses(lat, lon, alt_m, today, to_date)

            # Build a contact_map keyed by date string (±1 day to handle
            # timezone-induced date offset between Skyfield UTC dates and
            # AstronomyAPI local dates).
            contact_map: dict[str, dict] = {}
            for ae in api_eclipses:
                contact_map[ae["date"]] = ae
                try:
                    d = date.fromisoformat(ae["date"])
                    contact_map[(d - _td(days=1)).isoformat()] = ae
                    contact_map[(d + _td(days=1)).isoformat()] = ae
                except (ValueError, KeyError):
                    pass

            # Enrich each eclipse dict with contactTimes, obscuration, visibility.
            enriched: list[dict] = []
            for e in eclipses_data:
                api_data = contact_map.get(e["date"])
                if api_data:
                    raw_ct = api_data.get("contactTimes")
                    contact_times: dict | None = None
                    if isinstance(raw_ct, dict):
                        contact_times = {
                            k: v
                            if isinstance(v, dict) and "date" in v and "altitude" in v
                            else None
                            for k, v in raw_ct.items()
                        }
                    obscuration = api_data.get("obscuration")
                    # Compute visibility per ADR-053 tiers (same as endpoint).
                    peak = (api_data.get("contactTimes") or {}).get("peak")
                    peak_alt = peak["altitude"] if isinstance(peak, dict) else None
                    if peak_alt is None or peak_alt <= 0:
                        visibility: str | None = "Not Visible"
                    elif peak_alt <= 5:
                        visibility = "Barely Visible"
                    elif peak_alt <= 15:
                        visibility = "Low in Sky"
                    else:
                        all_above = all(
                            (ct or {}).get("altitude", -1) > 0
                            for ct in (api_data.get("contactTimes") or {}).values()
                            if ct is not None
                        )
                        visibility = "Visible All Night" if all_above else "Mostly Visible"
                    enriched.append({
                        "date": e["date"],
                        "type": e["type"],
                        "contactTimes": contact_times,
                        "obscuration": obscuration,
                        "visibility": visibility,
                    })
                else:
                    enriched.append({
                        "date": e["date"],
                        "type": e["type"],
                        "contactTimes": None,
                        "obscuration": None,
                        "visibility": None,
                    })

            cache.set(
                f"warmer:almanac:eclipses:{today.isoformat()}",
                enriched,
                self._settings.eclipses_interval_seconds,
            )
            logger.info(
                "Cache warmer: eclipses refreshed with AstronomyAPI enrichment (from %s, %d events)",
                today.isoformat(),
                len(enriched),
            )
        except Exception:
            # AstronomyAPI enrichment failed — fall back to unenriched Skyfield data.
            logger.warning(
                "Cache warmer: AstronomyAPI lunar eclipse enrichment failed; "
                "caching unenriched Skyfield data",
                exc_info=True,
            )
            cache.set(
                f"warmer:almanac:eclipses:{today.isoformat()}",
                eclipses_data,
                self._settings.eclipses_interval_seconds,
            )

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
            to_date = today + timedelta(days=3652)
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
            station_tz = self._station["station_tz"]
            try:
                zi = ZoneInfo(station_tz)
                today = datetime.now(tz=zi).date()
            except (ZoneInfoNotFoundError, KeyError):
                today = date.today()
            to_date = today + timedelta(days=365)
            lat = self._station["lat"]
            lon = self._station["lon"]
            alt_m = self._station["alt_m"]

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

    def _warm_marine(self) -> None:
        """Pre-fetch marine provider data for configured locations (Marine
        Remediation Plan T2.1/T2.2).

        Unlike the almanac/records/faults warm functions above (which write
        to ``warmer:`` cache keys that the corresponding endpoint reads
        back), marine warming works differently: it calls the marine
        providers' own .fetch() functions, which populate each provider's
        own internal ADR-017 cache.  The marine endpoints call those same
        .fetch() functions and get cache hits — there is no separate
        ``warmer:marine:*`` cache layer to read or write.

        Deduplicates fetches across all configured [marine] locations where
        the provider's cache key allows it, so a station shared by two
        locations (or a WaveWatch III grid group shared by two nearby
        locations) is only fetched once per warm cycle.

        NWS SRF and NWPS are the exception: both require lat/lon (not just
        a WFO) because each resolves location-specific data from the
        coordinates — nws_srf resolves a per-county forecast zone (a WFO
        can cover multiple zones, so a single representative fetch would
        under-warm the other zones), and nwps bilinear-interpolates the
        wave grid at the exact point. The real endpoints
        (endpoints/beach_safety.py, endpoints/surf.py) call both providers
        per-location with the location's own lat/lon, so warming mirrors
        that exactly to guarantee a cache hit. Redundant calls for
        locations sharing a zone/WFO are cheap: both providers cache
        internally, so only the first location per zone/WFO does real
        network work.
        """
        if self._marine_config is None:
            return
        try:
            # Deduplicate: collect unique identifiers across all locations.
            ndbc_stations: set[str] = set()
            coops_stations: set[str] = set()
            nws_zones: set[str] = set()
            nwps_locations: list = []
            nwps_wfos: set[str] = set()

            for loc in self._marine_config.locations:
                ndbc_stations.update(loc.ndbc_station_ids or [])
                coops_stations.update(loc.coops_station_ids or [])
                if loc.nws_marine_zone_id:
                    nws_zones.add(loc.nws_marine_zone_id)
                if loc.nwps_wfo:
                    nwps_locations.append(loc)
                    nwps_wfos.add(loc.nwps_wfo)

            # NDBC buoy observations — one fetch per unique station.
            for station_id in ndbc_stations:
                try:
                    from weewx_clearskies_api.providers.buoy import ndbc

                    ndbc.fetch(station_id=station_id)
                    logger.info("Cache warmer: NDBC %s refreshed", station_id)
                except Exception:
                    logger.warning("Cache warmer: NDBC %s warm failed", station_id, exc_info=True)

            # CO-OPS tide predictions + water levels — one fetch per unique station.
            for station_id in coops_stations:
                try:
                    from weewx_clearskies_api.providers.tides import coops

                    coops.fetch(station_id=station_id, products=("predictions", "water_level"))
                    logger.info("Cache warmer: CO-OPS %s refreshed", station_id)
                except Exception:
                    logger.warning("Cache warmer: CO-OPS %s warm failed", station_id, exc_info=True)

            # NWS marine text — one fetch per unique zone.
            for zone_id in nws_zones:
                try:
                    from weewx_clearskies_api.providers.marine import nws_marine

                    nws_marine.fetch(zone_id=zone_id)
                    logger.info("Cache warmer: NWS marine text %s refreshed", zone_id)
                except Exception:
                    logger.warning("Cache warmer: NWS marine text %s warm failed", zone_id, exc_info=True)

            # WaveWatch III — one fetch per unique grid group (spatial dedup via
            # marine_location_resolver; locations without a resolved group are
            # skipped for this warm cycle and picked up once the resolver runs).
            from weewx_clearskies_api.services.marine_location_resolver import get_grid_group

            fetched_groups: set[str] = set()
            for loc in self._marine_config.locations:
                group = get_grid_group(loc.id)
                if group and group not in fetched_groups:
                    try:
                        from weewx_clearskies_api.providers.marine import wavewatch

                        wavewatch.fetch(lat=loc.lat, lon=loc.lon)
                        fetched_groups.add(group)
                        logger.info("Cache warmer: WaveWatch III %s refreshed", group)
                    except Exception:
                        logger.warning(
                            "Cache warmer: WaveWatch III warm failed for group %s", group, exc_info=True
                        )

            # NWS SRF (surf zone forecast) — one fetch per location with
            # nwps_wfo configured (zone resolution needs lat/lon; see
            # docstring above). Mirrors endpoints/beach_safety.py and
            # endpoints/surf.py's nws_srf.fetch(lat=..., lon=...) call.
            for loc in nwps_locations:
                try:
                    from weewx_clearskies_api.providers.marine import nws_srf

                    nws_srf.fetch(lat=loc.lat, lon=loc.lon)
                    logger.info("Cache warmer: NWS SRF refreshed for %s (wfo=%s)", loc.id, loc.nwps_wfo)
                except Exception:
                    logger.warning("Cache warmer: NWS SRF warm failed for %s", loc.id, exc_info=True)

            # NWPS nearshore wave model — one fetch per location with
            # nwps_wfo configured (grid interpolation needs lat/lon; see
            # docstring above). Mirrors endpoints/beach_safety.py and
            # endpoints/surf.py's nwps.fetch(lat=..., lon=..., wfo_override=...)
            # call. wfo_override skips re-resolving the WFO via NWS /points
            # since it's already known from config.
            for loc in nwps_locations:
                try:
                    from weewx_clearskies_api.providers.marine import nwps

                    nwps.fetch(lat=loc.lat, lon=loc.lon, wfo_override=loc.nwps_wfo)
                    logger.info("Cache warmer: NWPS refreshed for %s (wfo=%s)", loc.id, loc.nwps_wfo)
                except Exception:
                    logger.warning("Cache warmer: NWPS warm failed for %s", loc.id, exc_info=True)

            logger.info(
                "Cache warmer: marine data refreshed (%d NDBC, %d CO-OPS, %d NWS zones, "
                "%d WW3 groups, %d SRF/NWPS locations across %d WFOs)",
                len(ndbc_stations),
                len(coops_stations),
                len(nws_zones),
                len(fetched_groups),
                len(nwps_locations),
                len(nwps_wfos),
            )
        except Exception:
            logger.warning("Cache warmer: marine warm failed", exc_info=True)

        # Forecast provider current conditions -> marine weather cache (T1.1).
        # Separate try/except so a failure here never blocks the provider
        # warming above (which already completed by this point).
        self._warm_marine_weather()

        # Ocean data resolver warm (T3.7) — pre-fetch OFS/ERDDAP ocean data
        # so card waterTemp comes from the resolver cache, not a live call.
        self._warm_ocean_data()

    def _warm_marine_weather(self) -> None:
        """Pre-fetch forecast-provider current conditions for marine locations
        not directly served by the weewx station (Marine Card Data Source
        Fix, T1.1).

        `endpoints/marine.py`'s `_location_summary()` sources
        `airTemp`/`windSpeed`/`windDirection`/`weatherCode`/`isDay` for a
        marine location either from the weewx archive (when
        `marine_location_resolver.is_station_served()` is True) or from this
        cache (`services/marine_weather_cache.py`).  Without this warm
        function the cache is never populated (`put_weather()` had zero
        callers), so `get_current_conditions()` always returned None and
        those five card fields were always null.

        Fetches once per unique grid group (`marine_location_resolver`'s
        distance-based dedup, same spatial dedup the WaveWatch III warm loop
        above uses) rather than once per location, so two nearby marine
        locations share a single provider call.

        Provider-agnostic by construction: it discovers whichever module is
        registered for the "forecast" domain via the capability registry
        (`get_provider_registry()`) and dispatches to that provider's
        `fetch_current_conditions()` with the provider-specific keyword
        arguments it requires -- the same per-provider dispatch pattern (and
        the same module-level credential state from `endpoints/forecast.py`)
        used by `endpoints/observations.py`'s `_fill_cloudcover_from_provider()`.
        No provider name is hardcoded as "the" configured provider; the
        branch taken depends entirely on what the operator has configured.
        """
        if self._marine_config is None:
            return
        try:
            from weewx_clearskies_api.providers._common.capability import (
                get_provider_registry,
            )
            from weewx_clearskies_api.providers._common.dispatch import get_provider_module
            from weewx_clearskies_api.services.marine_location_resolver import get_grid_group
            from weewx_clearskies_api.services.marine_weather_cache import (
                get_marine_weather_cache,
            )
            from weewx_clearskies_api.services.units import get_target_unit

            weather_cache = get_marine_weather_cache()
            if weather_cache is None:
                return  # Marine weather cache not configured -- nothing to warm.

            forecast_providers = [
                p for p in get_provider_registry() if p.domain == "forecast"
            ]
            if not forecast_providers:
                return  # No forecast provider configured/registered.

            provider_id = forecast_providers[0].provider_id
            try:
                provider_module = get_provider_module(domain="forecast", provider_id=provider_id)
            except KeyError:
                logger.warning(
                    "Cache warmer: unknown forecast provider %r; skipping marine weather warm",
                    provider_id,
                )
                return

            try:
                target_unit = get_target_unit()
            except RuntimeError:
                target_unit = "US"

            # Deferred import mirrors _fill_cloudcover_from_provider()'s pattern
            # for reading operator credential state populated at startup by
            # wire_forecast_settings().
            import weewx_clearskies_api.endpoints.forecast as _forecast_ep

            station_tz = self._station.get("station_tz", "UTC")

            fetched_groups: set[str] = set()
            for loc in self._marine_config.locations:
                group = get_grid_group(loc.id) or f"{round(loc.lat, 2)}_{round(loc.lon, 2)}"
                if group in fetched_groups:
                    continue
                fetched_groups.add(group)

                try:
                    if provider_id == "openmeteo":
                        conditions = provider_module.fetch_current_conditions(
                            lat=loc.lat,
                            lon=loc.lon,
                            target_unit=target_unit,
                            timezone=station_tz,
                        )
                    elif provider_id == "nws":
                        conditions = provider_module.fetch_current_conditions(
                            lat=loc.lat,
                            lon=loc.lon,
                            target_unit=target_unit,
                            user_agent_contact=_forecast_ep._nws_user_agent_contact,
                        )
                    elif provider_id == "aeris":
                        conditions = provider_module.fetch_current_conditions(
                            lat=loc.lat,
                            lon=loc.lon,
                            target_unit=target_unit,
                            client_id=_forecast_ep._aeris_client_id,
                            client_secret=_forecast_ep._aeris_client_secret,
                        )
                    elif provider_id == "openweathermap":
                        conditions = provider_module.fetch_current_conditions(
                            lat=loc.lat,
                            lon=loc.lon,
                            target_unit=target_unit,
                            appid=_forecast_ep._openweathermap_appid,
                        )
                    else:
                        logger.debug(
                            "Cache warmer: no fetch_current_conditions dispatch for "
                            "provider %r; skipping marine weather warm",
                            provider_id,
                        )
                        continue

                    if conditions is None:
                        continue

                    weather_code: int | None = None
                    if conditions.weatherCode is not None:
                        try:
                            weather_code = int(conditions.weatherCode)
                        except (TypeError, ValueError):
                            weather_code = None

                    weather_cache.put_weather(
                        loc.lat,
                        loc.lon,
                        {
                            "airTemp": conditions.temperature,
                            "windSpeed": conditions.windSpeed,
                            "windDirection": conditions.windDir,
                            "weatherCode": weather_code,
                            "isDay": conditions.isDay,
                            "skyCondition": None,
                            # T9.2: ProviderConditions (models/responses.py) carries
                            # no UV field today — none of the four forecast
                            # providers' fetch_current_conditions() populate one.
                            # Always None here; wired through so a future provider
                            # that does supply UV needs no cache-shape change.
                            "uvIndex": None,
                        },
                    )
                    logger.info(
                        "Cache warmer: marine weather cache updated for group %s (provider=%s)",
                        group,
                        provider_id,
                    )
                except Exception:
                    logger.warning(
                        "Cache warmer: marine weather warm failed for group %s",
                        group,
                        exc_info=True,
                    )
        except Exception:
            logger.warning("Cache warmer: marine weather warm failed", exc_info=True)

    def _warm_ocean_data(self) -> None:
        """Pre-fetch ocean data (waterTemp, currents, salinity) for marine
        locations via the ocean data resolver (T3.7, ADR-091).

        Deduplicates by OFS model — one OFS fetch per model, not per location.
        Locations without an ofs_model still get warmed (resolver falls to
        RTOFS/MUR SST).
        """
        if self._marine_config is None:
            return
        try:
            from weewx_clearskies_api.services.ocean_data_resolver import (
                resolve as resolve_ocean,
            )

            warmed_models: set[str] = set()
            for loc in self._marine_config.locations:
                ofs_model = getattr(loc, "ofs_model", None)
                dedup_key = ofs_model or f"{loc.lat:.3f}_{loc.lon:.3f}"
                if dedup_key in warmed_models:
                    continue
                warmed_models.add(dedup_key)

                try:
                    resolve_ocean(
                        lat=loc.lat,
                        lon=loc.lon,
                        location_config={
                            "ofs_model": ofs_model,
                            "ofs_fallback": getattr(loc, "ofs_fallback", None),
                            "ofs_region": getattr(loc, "ofs_region", None),
                        },
                        needs="surface",
                    )
                    logger.info(
                        "Cache warmer: ocean data warmed for %s (model=%s)",
                        loc.id,
                        ofs_model or "fallback",
                    )
                except Exception:
                    logger.warning(
                        "Cache warmer: ocean data warm failed for %s", loc.id, exc_info=True
                    )
        except Exception:
            logger.warning("Cache warmer: ocean data warm failed", exc_info=True)

    def _warm_hrrr_wind(self) -> None:
        """Pre-fetch HRRR wind field for configured marine location bbox.

        Active only when the [nearshore] pip extra is installed (eccodes or
        pygrib available for GRIB2 processing; GRIB_AVAILABLE == True in
        providers/wind/hrrr.py).  Returns silently when the extra is absent
        so this method is zero-cost on standard installations.

        The bounding box is derived from all configured marine locations:
        the extreme lat/lon values across all locations expanded by 1 degree
        on each side (~111 km) to cover the surrounding coastal domain and
        provide sufficient wind fetch for SWAN nearshore physics.

        Fires at startup (inside the background daemon thread, non-blocking
        for the API process — the API accepts requests immediately while the
        first HRRR fetch runs) and on the hourly schedule thereafter,
        matching the HRRR cycle cadence.

        The HRRR provider caches the result internally per ADR-017 with a
        3300 s TTL (PROVIDER-MANUAL §14.14); the SWAN runner (TruShore,
        T2.x) reads from that cache.  This warm call ensures the cache is
        primed before the SWAN runner's first cycle.

        Failure is logged at WARNING — the API continues normally and the
        SWAN runner will retry on the next hourly cycle.
        """
        if self._marine_config is None or not self._marine_config.locations:
            return

        # Guard: [nearshore] extra required.  try-import is defensive; the
        # module exists when T1.1 is deployed but GRIB_AVAILABLE may still be
        # False when eccodes/pygrib are not installed on this host.
        try:
            from weewx_clearskies_api.providers.wind.hrrr import GRIB_AVAILABLE
        except ImportError:
            return  # [nearshore] module absent (should not happen post-T1.1)

        if not GRIB_AVAILABLE:
            return  # eccodes and pygrib both absent — skip silently

        try:
            from weewx_clearskies_api.providers.wind import hrrr as _hrrr

            lats = [loc.lat for loc in self._marine_config.locations]
            lons = [loc.lon for loc in self._marine_config.locations]

            # 1-degree margin (~111 km per side) covers the offshore approach
            # cells that drive nearshore wave propagation in SWAN.
            _MARGIN = 1.0
            bbox: tuple[float, float, float, float] = (
                min(lons) - _MARGIN,  # lon_min (west)
                min(lats) - _MARGIN,  # lat_min (south)
                max(lons) + _MARGIN,  # lon_max (east)
                max(lats) + _MARGIN,  # lat_max (north)
            )
            _hrrr.fetch(bbox=bbox)
            logger.info("Cache warmer: HRRR wind field refreshed (bbox=%s)", bbox)
        except Exception:
            logger.warning("Cache warmer: HRRR wind warm failed", exc_info=True)

    def _warm_swan(self) -> None:
        """Run SWAN+TruShore nearshore wave model for configured surf spots.

        Fires sequentially after _warm_hrrr_wind() completes — HRRR wind data
        must be in cache before SWAN can run.  Active only when the [nearshore]
        pip extra is installed; returns silently when the extra is absent so
        this method is zero-cost on standard installations.

        On failure: logs ERROR, last-good per-spot cache is preserved
        indefinitely (stale TruShore data is always preferred to no data per
        PROVIDER-MANUAL §14.15).  Failure here does NOT affect the API's
        ability to serve other endpoints.

        SWAN runs are deduplicated per HRRR cycle: if SWAN already completed
        for the current HRRR cycle (within the 55-min run marker TTL), the
        run is skipped without error.  Operators can force a re-run by
        flushing the cache.

        Reference: SWAN-TRUSHORE-PLAN.md T2.5, PROVIDER-MANUAL §14.15.
        """
        if self._marine_config is None or not getattr(
            self._marine_config, "surf_spots", {}
        ):
            return  # No surf spots configured — nothing to warm.

        # Guard: [nearshore] extra required.
        try:
            from weewx_clearskies_api.providers.wind.hrrr import GRIB_AVAILABLE
        except ImportError:
            return  # [nearshore] module absent

        if not GRIB_AVAILABLE:
            return  # eccodes and pygrib both absent — skip silently

        try:
            from weewx_clearskies_api.providers.nearshore import trushore

            trushore.run_all_spots(self._marine_config)
        except Exception:
            logger.error(
                "Cache warmer: SWAN warm failed unexpectedly; "
                "last-good cache preserved",
                exc_info=True,
            )

    def _warm_forecast(self) -> None:
        """Pre-fetch GET /forecast's provider bundle (FIX-24).

        /forecast is a cold-call outlier: 5 sequential NWS HTTP calls,
        ~11 s, with a 30-min TTL (ADR-017).  Every TTL expiry makes the
        next visitor pay the full cold-call cost.  This warm function
        avoids that by calling the provider's own fetch() ahead of time on
        a 1500 s cycle (comfortably inside the 1800 s TTL).

        Mirrors `endpoints/forecast.py`'s `get_forecast()` dispatch
        exactly: discovers whichever provider is registered for the
        "forecast" domain via `get_provider_registry()` (provider-agnostic
        by construction -- no provider name is hardcoded as "the"
        configured provider) and calls that provider's `fetch()` with
        station lat/lon and the provider-specific kwargs it requires.
        `fetch()` populates the provider's own internal ADR-017 cache; the
        endpoint calls the same `fetch()` and gets a cache hit.  No
        separate `warmer:forecast` cache key is written (same pattern as
        `_warm_marine_weather` / `_warm_marine`).

        Credential state (NWS user-agent contact, Aeris client id/secret +
        forecast model, OWM appid) is read from `endpoints/forecast.py`
        module-level variables populated at startup by
        `wire_forecast_settings()` -- the same deferred-import pattern
        `_warm_marine_weather()` uses.
        """
        try:
            from weewx_clearskies_api.providers._common.capability import (
                get_provider_registry,
            )
            from weewx_clearskies_api.services.station import get_station_info
            from weewx_clearskies_api.services.units import get_target_unit

            forecast_providers = [
                p for p in get_provider_registry() if p.domain == "forecast"
            ]
            if not forecast_providers:
                return  # No forecast provider configured/registered.

            provider_id = forecast_providers[0].provider_id

            try:
                station = get_station_info()
            except RuntimeError:
                logger.debug(
                    "Cache warmer: station metadata not available; skipping forecast warm"
                )
                return

            try:
                target_unit = get_target_unit()
            except RuntimeError:
                logger.debug(
                    "Cache warmer: target unit not available; skipping forecast warm"
                )
                return

            # Deferred import mirrors _fill_cloudcover_from_provider()'s pattern
            # for reading operator credential state populated at startup by
            # wire_forecast_settings().
            import weewx_clearskies_api.endpoints.forecast as _forecast_ep

            if provider_id == "openmeteo":
                from weewx_clearskies_api.providers.forecast import openmeteo

                openmeteo.fetch(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    timezone=station.timezone,
                )
            elif provider_id == "nws":
                from weewx_clearskies_api.providers.forecast import nws as forecast_nws

                forecast_nws.fetch(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    user_agent_contact=_forecast_ep._nws_user_agent_contact,
                )
            elif provider_id == "aeris":
                from weewx_clearskies_api.providers.forecast import aeris

                aeris.fetch(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    client_id=_forecast_ep._aeris_client_id,
                    client_secret=_forecast_ep._aeris_client_secret,
                    forecast_model=_forecast_ep._aeris_forecast_model,
                )
            elif provider_id == "openweathermap":
                from weewx_clearskies_api.providers.forecast import openweathermap

                openweathermap.fetch(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    appid=_forecast_ep._openweathermap_appid,
                )
            else:
                logger.debug(
                    "Cache warmer: no fetch dispatch for forecast provider %r; "
                    "skipping forecast warm",
                    provider_id,
                )
                return

            logger.info("Cache warmer: forecast refreshed (provider=%s)", provider_id)
        except Exception:
            logger.warning("Cache warmer: forecast warm failed", exc_info=True)

    def _warm_current_conditions(self) -> None:
        """Pre-fetch GET /current's provider-conditions blend (FIX-24).

        /current calls the configured forecast provider's
        `fetch_current_conditions()` to blend cloudcover/snow/snowRate when
        the archive row has those fields null (see
        `endpoints/observations.py`'s `_fill_cloudcover_from_provider()`).
        That blend call is a cold-call outlier: 3 NWS HTTP calls, ~6.2 s,
        with a 5-min TTL (ADR-017).  This warm function avoids that by
        calling `fetch_current_conditions()` ahead of time on a 240 s cycle
        (comfortably inside the 300 s TTL).

        Mirrors `_fill_cloudcover_from_provider()`'s dispatch exactly:
        discovers whichever provider is registered for the "forecast"
        domain via `get_provider_registry()` (provider-agnostic by
        construction) and calls that provider's `fetch_current_conditions()`
        with station lat/lon and the provider-specific kwargs it requires.
        `fetch_current_conditions()` populates the provider's own internal
        ADR-017 cache; the endpoint calls the same function and gets a
        cache hit.  No separate `warmer:current` cache key is written (same
        pattern as `_warm_marine_weather` / `_warm_marine`).
        """
        try:
            from weewx_clearskies_api.providers._common.capability import (
                get_provider_registry,
            )
            from weewx_clearskies_api.providers._common.dispatch import (
                get_provider_module,
            )
            from weewx_clearskies_api.services.station import get_station_info
            from weewx_clearskies_api.services.units import get_target_unit

            forecast_providers = [
                p for p in get_provider_registry() if p.domain == "forecast"
            ]
            if not forecast_providers:
                return  # No forecast provider configured/registered.

            provider_id = forecast_providers[0].provider_id

            try:
                station = get_station_info()
            except RuntimeError:
                logger.debug(
                    "Cache warmer: station metadata not available; "
                    "skipping current conditions warm"
                )
                return

            try:
                target_unit = get_target_unit()
            except RuntimeError:
                logger.debug(
                    "Cache warmer: target unit not available; "
                    "skipping current conditions warm"
                )
                return

            try:
                provider_module = get_provider_module(
                    domain="forecast", provider_id=provider_id
                )
            except KeyError:
                logger.warning(
                    "Cache warmer: unknown forecast provider %r; "
                    "skipping current conditions warm",
                    provider_id,
                )
                return

            # Deferred import mirrors _fill_cloudcover_from_provider()'s pattern
            # for reading operator credential state populated at startup by
            # wire_forecast_settings().
            import weewx_clearskies_api.endpoints.forecast as _forecast_ep

            if provider_id == "openmeteo":
                provider_module.fetch_current_conditions(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    timezone=station.timezone,
                )
            elif provider_id == "nws":
                provider_module.fetch_current_conditions(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    user_agent_contact=_forecast_ep._nws_user_agent_contact,
                )
            elif provider_id == "aeris":
                provider_module.fetch_current_conditions(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    client_id=_forecast_ep._aeris_client_id,
                    client_secret=_forecast_ep._aeris_client_secret,
                )
            elif provider_id == "openweathermap":
                provider_module.fetch_current_conditions(
                    lat=station.latitude,
                    lon=station.longitude,
                    target_unit=target_unit,
                    appid=_forecast_ep._openweathermap_appid,
                )
            else:
                logger.debug(
                    "Cache warmer: no fetch_current_conditions dispatch for "
                    "provider %r; skipping current conditions warm",
                    provider_id,
                )
                return

            logger.info(
                "Cache warmer: current conditions refreshed (provider=%s)", provider_id
            )
        except Exception:
            logger.warning("Cache warmer: current conditions warm failed", exc_info=True)
