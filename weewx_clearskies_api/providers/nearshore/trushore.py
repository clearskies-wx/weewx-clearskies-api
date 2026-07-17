"""TruShore nearshore wave model provider (PROVIDER-MANUAL §14.15, ADR-093).

Not a network provider.  This module is a thin provider wrapper around
``services/swan_runner.py``.  It follows the existing provider interface pattern
but runs a local SWAN subprocess instead of making an HTTP request.

Five responsibilities per PROVIDER-MANUAL §1:
  1. Not an outbound API call — orchestrates a SWAN subprocess using inputs
     already in cache (HRRR wind, WW3 boundary conditions, CUDEM bathymetry).
  2. Response parsing — done inside ``services/swan_runner.py`` / swan_formats.py;
     this module converts SWANRunner output dicts into cacheable payloads.
  3. Canonical field translation — MarineForecastPoint carries waveHeight (Hs),
     wavePeriod (Tm01), waveDirection (MWD) in SI units.  No unit conversion
     needed here (SWAN outputs metres and seconds natively).
  4. Capability declaration — CAPABILITY symbol; None when the [nearshore]
     extra is absent.
  5. Error handling — SWANRunError is caught at the run_all_spots() level;
     last-good cache is preserved indefinitely on failure.

Key design decisions:
  - Two cache tiers per spot:
      last_good_key (7-day TTL): always stores the most recent successful run.
        fetch() reads this and computes data_age_seconds.  Stale data is always
        preferred to no data — this key is never invalidated on failure.
      run_marker_key (55-min TTL, same as HRRR): marks that a SWAN run
        completed for the current HRRR cycle, preventing duplicate runs when
        the warmer ticks more than once before HRRR data updates.
  - Temp directory lifecycle: TrushoreProvider creates the tmpdir itself (via
    _SWANRunnerWithCleanup) and deletes it on success.  On SWANRunError the
    tmpdir is preserved and its path is logged at ERROR level.
  - CUDEM bathymetry: passed as an empty dict when no CUDEM provider data is
    available; cudem_to_swan_bottom() defaults to a uniform 15m ocean depth.
  - WW3 boundary: wavewatch.fetch() called at domain center for scalar TPAR
    parametric boundary conditions.
  - HRRR wind: hrrr.fetch() called with the same 1-degree-margin bbox the
    cache warmer's _warm_hrrr_wind() uses; this guarantees a cache hit.

Active only when [nearshore] pip extra is installed (GRIB_AVAILABLE == True in
providers/wind/hrrr.py).  CAPABILITY is None otherwise so the provider is not
registered.

References:
  - PROVIDER-MANUAL §14.15 (SWAN+TruShore runner specification)
  - SWAN-TRUSHORE-PLAN.md T2.5
  - services/swan_runner.py (SWANRunner.run() interface)
  - ADR-093 (SWAN+TruShore nearshore model)
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.services.swan_runner import SWANRunner, SWANRunError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "trushore"
DOMAIN = "nearshore"

_API_VERSION = "0.1.0"

# Cache TTLs
_CACHE_TTL_SECONDS = 3300        # 55 min per-cycle run marker (matches HRRR TTL)
_LAST_GOOD_TTL_SECONDS = 604800  # 7 days — "stale is always preferred to no data"

# SWAN grid defaults (configurable via [marine] section in future T4.x)
_DEFAULT_SWAN_BINARY = "/usr/local/bin/swan"
_DEFAULT_GRID_RESOLUTION_M = 200.0
_DEFAULT_COMPUTE_DT_MIN = 10
_DEFAULT_OUTPUT_INTERVAL_HR = 1.0
_DEFAULT_SWAN_TIMEOUT_S = 900

# Bounding-box expansion margins (degrees) used to derive the SWAN domain
# and HRRR wind bbox from configured surf spot coordinates.

# ---------------------------------------------------------------------------
# Remote mode state (T4.2 / T4.3 — set when [trushore] service_url is active)
# ---------------------------------------------------------------------------

#: URL of the standalone TruShore service (None = bundled mode).
_remote_url: str | None = None

#: Number of consecutive health check failures since last success.
_remote_consecutive_failures: int = 0

#: True when the remote service is considered healthy (or bundled mode active).
_remote_healthy: bool = True

#: True after we have logged the "unreachable" ERROR (avoid log spam).
_remote_warned_unreachable: bool = False

#: Lock protecting lazy remote-mode initialisation in run_all_spots().
_remote_init_lock = threading.Lock()

#: Background health check thread (started by configure_remote_mode()).
_remote_health_thread: threading.Thread | None = None

#: Health check interval in seconds (T4.3).
_REMOTE_HEALTH_INTERVAL_S: int = 60

#: Consecutive failure threshold before ERROR is logged and stale cache is served.
_REMOTE_FAILURE_THRESHOLD: int = 3


# ---------------------------------------------------------------------------
# [nearshore] extra availability guard
# ---------------------------------------------------------------------------

try:
    from weewx_clearskies_api.providers.wind.hrrr import GRIB_AVAILABLE as _GRIB_AVAILABLE
except ImportError:
    _GRIB_AVAILABLE = False

_NEARSHORE_AVAILABLE: bool = _GRIB_AVAILABLE


# ---------------------------------------------------------------------------
# Capability declaration (PROVIDER-MANUAL §1, §14.15)
# ---------------------------------------------------------------------------

if _NEARSHORE_AVAILABLE:
    CAPABILITY: ProviderCapability | None = ProviderCapability(
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
        supplied_canonical_fields=(
            "waveHeight",
            "wavePeriod",
            "waveDirection",
        ),
        geographic_coverage="configured_marine_surf_locations",
        auth_required=(),
        default_poll_interval_seconds=_CACHE_TTL_SECONDS,
        operator_notes=(
            "SWAN+TruShore locally-run nearshore wave model (PROVIDER-MANUAL §14.15). "
            "Requires SWAN binary on PATH (default: /usr/local/bin/swan) and the "
            "[nearshore] pip extra (eccodes or pygrib). "
            "Runs hourly on the HRRR cycle — not a network provider. "
            "No API key required."
        ),
        refresh_interval=_CACHE_TTL_SECONDS,
        attribution=ProviderAttribution(
            attribution_required=False,
            display_name="SWAN+TruShore",
            attribution_text=(
                "Nearshore wave forecast powered by SWAN+TruShore (Clear Skies)"
            ),
            text_prefix="Powered by",
            text_provider_name="SWAN+TruShore",
            url="https://swanmodel.sourceforge.io/",
        ),
    )
else:
    CAPABILITY = None


# ---------------------------------------------------------------------------
# Cache key construction (PROVIDER-MANUAL §14.15)
# ---------------------------------------------------------------------------


def _build_last_good_key(spot_id: str) -> str:
    """SHA-256 key for last-good SWAN output per surf spot.

    This key has a 7-day TTL and is overwritten on every successful SWAN run.
    It provides the stale-preferred-to-nothing fallback that the surf endpoint
    reads.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "spot_id": spot_id,
            "type": "last_good",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_run_marker_key(hrrr_cycle_time: str) -> str:
    """SHA-256 key for a per-HRRR-cycle SWAN run-completion marker.

    Stored with TTL=_CACHE_TTL_SECONDS (55 min) after a successful SWAN run.
    run_all_spots() checks this key to avoid re-running SWAN for the same
    HRRR cycle when the warmer fires more than once before the cycle updates.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "type": "run_marker",
            "hrrr_cycle_time": hrrr_cycle_time,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# _SWANRunnerWithCleanup — subclass exposing tmpdir for caller lifecycle
# ---------------------------------------------------------------------------


class _SWANRunnerWithCleanup(SWANRunner):
    """SWANRunner variant that returns the tmpdir path for caller-managed cleanup.

    SWANRunner.run() creates a tmpdir internally and does not expose it — the
    caller has no path to clean up.  This subclass calls the same private
    implementation methods (_write_input_files, _spawn_swan, _parse_output)
    with a caller-supplied tmpdir so TrushoreProvider can delete on success
    and preserve-plus-log on failure per PROVIDER-MANUAL §14.15.
    """

    def run_with_tmpdir(
        self,
        hrrr_wind_field: dict[str, Any],
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
    ) -> tuple[dict[str, list], Path]:
        """Run SWAN and return (results, tmpdir).

        The caller is responsible for deleting tmpdir on success and for
        preserving + logging tmpdir on failure.

        Raises:
            SWANRunError: SWAN exited non-zero, timed out, or binary not found.
            ValueError: Required config keys missing or input data empty.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="swan_run_"))
        grid_info = self._write_input_files(
            tmpdir, hrrr_wind_field, ww3_boundary, cudem_bathymetry
        )
        self._spawn_swan(tmpdir)
        results = self._parse_output(tmpdir, grid_info)
        return results, tmpdir


# ---------------------------------------------------------------------------
# Remote mode — health check thread and startup probe (T4.2 / T4.3)
# ---------------------------------------------------------------------------


def _remote_health_loop(service_url: str, spot_ids: list[str]) -> None:
    """Background daemon thread: poll remote /health every 60 s (T4.3).

    On each successful health check:
      - Resets _remote_consecutive_failures to 0.
      - Fetches fresh forecast data for each configured spot from the remote
        service's /surf/{spot_id}/forecast endpoint.
      - Stores results in the local last-good cache (7-day TTL, same cache
        keys as bundled mode) so fetch() can serve them from the request path.

    On consecutive failures:
      - Increments _remote_consecutive_failures.
      - After _REMOTE_FAILURE_THRESHOLD (3) consecutive failures: logs ERROR
        once, sets _remote_healthy = False.  Subsequent failures are logged
        at DEBUG to avoid log spam.
      - fetch() continues to serve the last-good cache (stale indefinitely per
        PROVIDER-MANUAL §14.15 — stale is always preferred to no data).

    Recovery:
      - When the remote service becomes reachable again: resets state, logs
        INFO "TruShore remote service recovered", resumes fresh data.

    Args:
        service_url: Base URL of the standalone TruShore service (e.g.
                     ``http://192.168.1.50:8767``).
        spot_ids: Surf spot IDs from marine config.  The health response may
                  supply an updated spot list; we merge both to avoid missing
                  spots that were added to the remote config later.
    """
    global _remote_consecutive_failures, _remote_healthy, _remote_warned_unreachable  # noqa: PLW0603

    while True:
        try:
            resp = httpx.get(f"{service_url}/health", timeout=10.0)
            resp.raise_for_status()
            health_data: dict[str, Any] = resp.json()

            # Recovery path: reset failure state.
            recovered = _remote_consecutive_failures >= _REMOTE_FAILURE_THRESHOLD or not _remote_healthy
            _remote_consecutive_failures = 0
            if recovered:
                _remote_healthy = True
                _remote_warned_unreachable = False
                logger.info(
                    "TruShore remote service recovered: %s (last_run=%s)",
                    service_url,
                    health_data.get("last_run"),
                )

            # Merge spot IDs from config and from health response.
            remote_spots: list[str] = health_data.get("spots") or []
            all_spots = list(dict.fromkeys(spot_ids + remote_spots))  # preserve order, deduplicate

            # Refresh per-spot forecast data in local last-good cache.
            cache = get_cache()
            for spot_id in all_spots:
                try:
                    forecast_resp = httpx.get(
                        f"{service_url}/surf/{spot_id}/forecast",
                        timeout=30.0,
                    )
                    if forecast_resp.status_code == 200:
                        data: dict[str, Any] = forecast_resp.json()
                        cache.set(
                            _build_last_good_key(spot_id),
                            {
                                "forecast": data.get("forecast", []),
                                "run_time": data.get("run_time"),
                                "hrrr_cycle_time": data.get("hrrr_cycle_time", ""),
                            },
                            _LAST_GOOD_TTL_SECONDS,
                        )
                        logger.debug(
                            "TruShore remote: refreshed spot %r (run_time=%s)",
                            spot_id,
                            data.get("run_time"),
                        )
                    elif forecast_resp.status_code == 503:
                        # No data yet — first SWAN run may be in progress.
                        logger.debug(
                            "TruShore remote: spot %r has no data yet (503)", spot_id
                        )
                    else:
                        logger.warning(
                            "TruShore remote: /surf/%s/forecast returned %d; skipping",
                            spot_id,
                            forecast_resp.status_code,
                        )
                except Exception:
                    logger.warning(
                        "TruShore remote: failed to refresh spot %r",
                        spot_id,
                        exc_info=True,
                    )

        except Exception:
            _remote_consecutive_failures += 1
            failures = _remote_consecutive_failures

            if failures < _REMOTE_FAILURE_THRESHOLD:
                logger.warning(
                    "TruShore remote health check failed (%d/%d): %s",
                    failures,
                    _REMOTE_FAILURE_THRESHOLD,
                    service_url,
                    exc_info=True,
                )
            elif failures == _REMOTE_FAILURE_THRESHOLD:
                _remote_healthy = False
                _remote_warned_unreachable = True
                logger.error(
                    "TruShore remote service unreachable after %d consecutive "
                    "health check failures: %s — serving stale cache indefinitely",
                    _REMOTE_FAILURE_THRESHOLD,
                    service_url,
                )
            else:
                # Already logged ERROR; stay quiet to avoid log spam.
                logger.debug(
                    "TruShore remote health check still failing (%d failures): %s",
                    failures,
                    service_url,
                )

        time.sleep(_REMOTE_HEALTH_INTERVAL_S)


def configure_remote_mode(service_url: str, marine_config: Any) -> bool:
    """Attempt to activate remote TruShore mode and start the health check thread.

    Called lazily from run_all_spots() on the first invocation when
    ``marine_config.trushore.is_remote`` is True (T4.2).  Protected by
    ``_remote_init_lock`` so concurrent callers (e.g. tests) are safe.

    Startup probe:
      Calls ``GET {service_url}/health`` with a 10 s timeout.  On failure:
        - Logs ERROR with fallback notice.
        - Returns False → caller falls through to bundled SWAN mode.
      On success: sets ``_remote_url``, starts the background health thread,
      returns True.

    Args:
        service_url: Base URL of the standalone TruShore service.
        marine_config: MarineConfig from api.conf (supplies spot_ids for the
                       health thread's initial spot list).

    Returns:
        True if remote mode was activated; False if the startup probe failed
        and the caller should fall back to bundled mode.
    """
    global _remote_url, _remote_health_thread  # noqa: PLW0603

    logger.info("TruShore: probing remote service at %s", service_url)
    try:
        resp = httpx.get(f"{service_url}/health", timeout=10.0)
        resp.raise_for_status()
        logger.info(
            "TruShore remote service reachable: %s (last_run=%s)",
            service_url,
            resp.json().get("last_run"),
        )
    except Exception as exc:
        logger.error(
            "TruShore: remote service unreachable at startup (%s: %s); "
            "falling back to bundled SWAN mode if SWAN binary is available",
            service_url,
            exc,
        )
        return False

    spot_ids = list(getattr(marine_config, "surf_spots", {}).keys()) if marine_config else []

    _remote_url = service_url
    _remote_health_thread = threading.Thread(
        target=_remote_health_loop,
        args=(service_url, spot_ids),
        daemon=True,
        name="trushore-remote-health",
    )
    _remote_health_thread.start()
    logger.info(
        "TruShore: remote mode active (service_url=%s, %d spot(s), "
        "health check every %ds)",
        service_url,
        len(spot_ids),
        _REMOTE_HEALTH_INTERVAL_S,
    )
    return True


# ---------------------------------------------------------------------------
# Public fetch entrypoint (PROVIDER-MANUAL §14.15)
# ---------------------------------------------------------------------------


def fetch(spot_id: str) -> dict[str, Any] | None:
    """Return the last-good SWAN wave forecast for one surf spot from cache.

    This function is READ-ONLY — it never triggers a SWAN run.  It is intended
    for the surf endpoint (T3.x) to call in the request path.  The cache warmer
    populates the cache via run_all_spots() in the background.

    Args:
        spot_id: Marine location ID (e.g. ``"wrightsville_beach"``).

    Returns:
        dict with keys:
          ``"forecast"`` — list[dict] (MarineForecastPoint.model_dump() shape)
          ``"run_time"`` — ISO-8601 UTC string of when the SWAN run completed
          ``"data_age_seconds"`` — int, seconds elapsed since the run completed
        Or None if no SWAN data has been cached for this spot yet.
    """
    last_good = get_cache().get(_build_last_good_key(spot_id))
    if last_good is None:
        if _remote_url:
            logger.debug(
                "TruShore remote: no cached data for spot %r — health thread has "
                "not yet completed a successful fetch from %s",
                spot_id,
                _remote_url,
            )
        else:
            logger.debug("TruShore: no cached data for spot %r", spot_id)
        return None

    # Compute live data age from the stored run_time.
    run_time_str: str | None = last_good.get("run_time")
    data_age_seconds: int | None = None
    if run_time_str:
        try:
            run_dt = datetime.fromisoformat(run_time_str.replace("Z", "+00:00"))
            data_age_seconds = int(
                (datetime.now(UTC) - run_dt).total_seconds()
            )
        except (ValueError, TypeError):
            pass

    return {
        "forecast": last_good.get("forecast", []),
        "run_time": run_time_str,
        "data_age_seconds": data_age_seconds,
    }


# ---------------------------------------------------------------------------
# SWAN run + cache (called from cache warmer)
# ---------------------------------------------------------------------------


def run_all_spots(
    marine_config: Any,
    *,
    hrrr_wind_field: dict | None = None,
    swan_binary: str = _DEFAULT_SWAN_BINARY,
    grid_resolution_m: float = _DEFAULT_GRID_RESOLUTION_M,
    compute_dt_min: int = _DEFAULT_COMPUTE_DT_MIN,
    output_interval_hr: float = _DEFAULT_OUTPUT_INTERVAL_HR,
    swan_timeout_s: int = _DEFAULT_SWAN_TIMEOUT_S,
) -> None:
    """Run SWAN for all configured surf spots and cache per-spot results.

    Called from BackgroundCacheWarmer._warm_swan() in a background daemon
    thread.  All surf spot locations in ``marine_config`` are processed in a
    single SWAN run.  On success the per-spot results are stored at the
    last-good cache key (7-day TTL).  On failure the last-good cache is
    untouched (stale data preserved indefinitely) per PROVIDER-MANUAL §14.15.

    Args:
        marine_config: Parsed MarineConfig from config.marine_config.
            Must have .locations (list[MarineLocation]) and .surf_spots
            (dict[str, SurfSpotConfig]).
        hrrr_wind_field: Pre-fetched HRRR wind field dict from the cache
            warmer.  When provided, TruShore uses it directly instead of
            calling _hrrr.fetch() — single fetch, single cache key, no
            divergence.  When None, falls back to fetching (legacy path).
        swan_binary: Absolute path to the SWAN executable.
        grid_resolution_m: SWAN grid spacing in metres (default 200).
        compute_dt_min: SWAN internal time step in minutes (default 10).
        output_interval_hr: TABLE output timestep in hours (default 1).
        swan_timeout_s: Subprocess timeout in seconds (default 900 / 15 min).

    Returns:
        None.  Results are stored in the cache.

    Raises:
        Nothing.  All exceptions are caught, logged, and the last-good cache
        is left intact.
    """
    # ---------------------------------------------------------------------------
    # Remote mode detection — lazy initialisation on first run_all_spots() call.
    # Protected by _remote_init_lock so parallel test runners are safe.
    # ---------------------------------------------------------------------------
    if _remote_url is None and marine_config is not None:
        trushore_cfg = getattr(marine_config, "trushore", None)
        if trushore_cfg is not None and trushore_cfg.is_remote:
            with _remote_init_lock:
                # Double-check after acquiring the lock (another thread may have
                # initialised while we were waiting).
                if _remote_url is None:
                    activated = configure_remote_mode(trushore_cfg.service_url, marine_config)
                    if not activated:
                        logger.warning(
                            "TruShore: remote mode startup probe failed; "
                            "continuing with bundled SWAN mode"
                        )
                        # _remote_url stays None → bundled mode continues.

    if _remote_url is not None:
        # Remote mode active: the background health thread fetches forecast data
        # from the remote service and populates the local last-good cache.
        # This function is a no-op — do NOT run SWAN locally.
        logger.debug(
            "TruShore: remote mode active (%s); local SWAN run skipped",
            _remote_url,
        )
        return

    if not _NEARSHORE_AVAILABLE:
        logger.debug(
            "TruShore: [nearshore] extra not installed; skipping SWAN run"
        )
        return

    if marine_config is None:
        return

    # Collect surf locations (locations that have a surf config block).
    surf_spot_ids: set[str] = set(getattr(marine_config, "surf_spots", {}).keys())
    if not surf_spot_ids:
        logger.debug("TruShore: no surf spots configured; skipping SWAN run")
        return

    surf_locations = [
        loc
        for loc in getattr(marine_config, "locations", [])
        if loc.id in surf_spot_ids
    ]
    if not surf_locations:
        logger.debug("TruShore: no surf locations found; skipping SWAN run")
        return

    # Use canonical bboxes from MarineConfig (computed once at parse time).
    domain_bbox = marine_config.swan_domain_bbox
    if domain_bbox is None:
        logger.debug("TruShore: no SWAN domain bbox; skipping")
        return

    # ------------------------------------------------------------------
    # 1. Use HRRR wind field passed by the cache warmer (single fetch).
    # ------------------------------------------------------------------
    if hrrr_wind_field is None:
        # Legacy fallback: fetch directly (used by manual trigger, tests).
        try:
            from weewx_clearskies_api.providers.wind import hrrr as _hrrr

            bbox = marine_config.hrrr_bbox
            if bbox is None:
                logger.error("TruShore: no HRRR bbox configured; skipping")
                return
            hrrr_wind_field = _hrrr.fetch(bbox=bbox)
        except Exception:
            logger.error(
                "TruShore: HRRR wind data unavailable; skipping SWAN run",
                exc_info=True,
            )
            return

    hrrr_cycle_time: str = hrrr_wind_field["cycle_time"]

    # Deduplication: skip if SWAN already ran for this HRRR cycle.
    run_marker_key = _build_run_marker_key(hrrr_cycle_time)
    if get_cache().get(run_marker_key) is not None:
        logger.debug(
            "TruShore: SWAN run already completed for HRRR cycle %s; skipping",
            hrrr_cycle_time,
        )
        return

    # ------------------------------------------------------------------
    # 2. Fetch WW3 boundary conditions (expected cache hit).
    # ------------------------------------------------------------------
    center_lat = (min(lats) + max(lats)) / 2.0
    center_lon = (min(lons) + max(lons)) / 2.0
    try:
        from weewx_clearskies_api.providers.marine import wavewatch

        ww3_boundary = wavewatch.fetch(lat=center_lat, lon=center_lon)
    except Exception:
        logger.warning(
            "TruShore: WW3 boundary data unavailable; SWAN will use calm boundary",
            exc_info=True,
        )
        ww3_boundary = {"forecast": [], "grid": "unavailable", "model_run": ""}

    # ------------------------------------------------------------------
    # 3. CUDEM bathymetry — stored in spot config (setup-time download).
    #    Pass {} when CUDEM data is not available; cudem_to_swan_bottom()
    #    defaults to a uniform 15m ocean depth (acceptable placeholder
    #    until CUDEM provider is implemented in a later task).
    # ------------------------------------------------------------------
    cudem_bathymetry: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 4. Build SWANRunner config and run SWAN.
    # ------------------------------------------------------------------
    surf_spots_config: dict[str, dict[str, float]] = {
        loc.id: {"lon": loc.lon, "lat": loc.lat} for loc in surf_locations
    }

    # Read omp_num_threads from TrushoreConfig (T4.2 / coordinator addition).
    # 0 means "let OpenMP decide" (all available cores — default behaviour).
    trushore_cfg = getattr(marine_config, "trushore", None)
    omp_num_threads: int = getattr(trushore_cfg, "omp_num_threads", 0) if trushore_cfg else 0

    swan_config: dict[str, Any] = {
        "domain_bbox": list(domain_bbox),
        "surf_spots": surf_spots_config,
        "swan_binary": swan_binary,
        "grid_resolution_m": grid_resolution_m,
        "compute_dt_min": compute_dt_min,
        "output_interval_hr": output_interval_hr,
        "swan_timeout_s": swan_timeout_s,
        "omp_num_threads": omp_num_threads,
    }

    runner = _SWANRunnerWithCleanup(swan_config)
    run_time = datetime.now(UTC)

    logger.info(
        "TruShore: starting SWAN run for %d spot(s) (HRRR cycle=%s, domain=%s)",
        len(surf_spots_config),
        hrrr_cycle_time,
        domain_bbox,
    )

    tmpdir: Path | None = None
    try:
        results, tmpdir = runner.run_with_tmpdir(
            hrrr_wind_field, ww3_boundary, cudem_bathymetry
        )
    except SWANRunError as exc:
        logger.error(
            "TruShore: SWAN run failed (returncode=%s); "
            "last-good cache preserved; tmpdir=%s\nSWAN stderr: %s",
            exc.returncode,
            tmpdir,
            exc.stderr[:2000] if exc.stderr else "(no stderr)",
        )
        # Do NOT invalidate last-good cache — stale data preferred to nothing.
        return
    except Exception:
        logger.error(
            "TruShore: unexpected error during SWAN run; "
            "last-good cache preserved",
            exc_info=True,
        )
        return

    # ------------------------------------------------------------------
    # 5. Successful run: cache per-spot results and clean up tmpdir.
    # ------------------------------------------------------------------
    run_time_iso = run_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    cache = get_cache()

    spots_cached = 0
    for spot_id, forecast_points in results.items():
        if not forecast_points:
            logger.warning(
                "TruShore: spot %r returned no valid wave data (all timesteps "
                "failed physical validation); skipping cache update for this spot",
                spot_id,
            )
            continue

        payload = {
            "forecast": [pt.model_dump() for pt in forecast_points],
            "run_time": run_time_iso,
            "hrrr_cycle_time": hrrr_cycle_time,
        }
        cache.set(
            _build_last_good_key(spot_id),
            payload,
            _LAST_GOOD_TTL_SECONDS,
        )
        spots_cached += 1

    # Store the run marker so duplicate runs for this HRRR cycle are skipped.
    cache.set(run_marker_key, {"run_time": run_time_iso}, _CACHE_TTL_SECONDS)

    # Clean up tmpdir on success.
    if tmpdir is not None and tmpdir.exists():
        try:
            shutil.rmtree(tmpdir)
            logger.debug("TruShore: cleaned up tmpdir %s", tmpdir)
        except OSError:
            logger.warning(
                "TruShore: could not remove tmpdir %s", tmpdir, exc_info=True
            )

    elapsed_s = int((datetime.now(UTC) - run_time).total_seconds())
    logger.info(
        "TruShore: SWAN run complete in %ds — %d/%d spot(s) cached "
        "(HRRR cycle=%s)",
        elapsed_s,
        spots_cached,
        len(surf_spots_config),
        hrrr_cycle_time,
    )
