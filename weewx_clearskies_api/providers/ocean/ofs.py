"""OFS ocean data provider module (ADR-091, PROVIDER-MANUAL §14.10).

Fetches ocean data (temperature, salinity, currents, water levels) from
NOAA OFS (Operational Forecast System) models via THREDDS/OPeNDAP.

15 OFS models cover major US coastal areas at 34m-4km resolution.
Always uses ``regulargrid`` files (pre-interpolated to regular lat/lon).
Never uses native ``fields`` files (curvilinear/unstructured grids).

Lat/Lon are 2D arrays [ny, nx] — xarray label-based selection does not
work; nearest-neighbor lookup via numpy distance with land masking.

Grid coordinates (Latitude, Longitude, Depth, mask, h) are cached per
model (24h TTL) — grid topology is fixed across forecast cycles.

Cycle fallback pattern: try most recent cycle first, step back up to 3 prior cycles.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from weewx_clearskies_api.providers._common.cache import get_cache

logger = logging.getLogger(__name__)

PROVIDER_ID = "ofs"
DOMAIN = "ocean"

_RESULT_CACHE_TTL = 1800
_GRID_CACHE_TTL = 86400
_MAX_CYCLE_FALLBACKS = 3
_DATASET_TIMEOUT = 20

THREDDS_BASE = "https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA"

OFS_MODELS: dict[str, dict[str, Any]] = {
    "WCOFS": {"cycles": [3], "max_fhr": 72, "step": 3},
    "GOMOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 72, "step": 3},
    "CBOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
    "DBOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
    "TBOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
    "CIOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
    "SFBOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
    "NGOFS2": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
    "SSCOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 72, "step": 3},
    "LMHOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 120, "step": 3},
    "LEOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 120, "step": 3},
    "LOOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 120, "step": 3},
    "LSOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 120, "step": 3},
    "NYOFS": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
    "SJROFS": {"cycles": [0, 6, 12, 18], "max_fhr": 48, "step": 3},
}

OFS_DOMAINS: dict[str, tuple[float, float, float, float]] = {
    "WCOFS": (24.0, 54.0, -134.0, -115.0),
    "GOMOFS": (39.5, 46.0, -72.0, -62.0),
    "CBOFS": (36.5, 39.8, -77.5, -75.0),
    "DBOFS": (38.0, 41.5, -76.0, -73.0),
    "TBOFS": (27.0, 28.5, -83.5, -82.0),
    "CIOFS": (58.5, 61.5, -155.0, -148.0),
    "SFBOFS": (37.3, 38.2, -123.0, -121.8),
    "NGOFS2": (25.0, 31.0, -98.0, -84.0),
    "SSCOFS": (45.5, 50.5, -127.0, -121.0),
    "LMHOFS": (41.5, 46.5, -87.5, -79.5),
    "LEOFS": (41.3, 42.9, -83.5, -78.8),
    "LOOFS": (43.0, 44.5, -79.8, -76.0),
    "LSOFS": (46.0, 49.5, -92.2, -84.0),
    "NYOFS": (40.3, 41.0, -74.5, -73.5),
    "SJROFS": (29.5, 30.6, -82.0, -81.0),
}

# Approximate resolution in degrees for overlap tiebreaking
_MODEL_RESOLUTION_DEG: dict[str, float] = {
    "SFBOFS": 0.01, "CBOFS": 0.01, "DBOFS": 0.01, "TBOFS": 0.01,
    "NYOFS": 0.01, "SJROFS": 0.01, "CIOFS": 0.02,
    "GOMOFS": 0.006, "NGOFS2": 0.003, "SSCOFS": 0.05,
    "LMHOFS": 0.01, "LEOFS": 0.01, "LOOFS": 0.01, "LSOFS": 0.01,
    "WCOFS": 0.04,
}


def find_ofs_model(lat: float, lon: float) -> tuple[str | None, str | None]:
    """Check which OFS model domain(s) contain a lat/lon point.

    Returns (primary_model, fallback_model). Primary is the highest-resolution
    model when domains overlap. None when no OFS coverage.
    """
    matches: list[tuple[str, float]] = []
    for model_name, (lat_s, lat_n, lon_w, lon_e) in OFS_DOMAINS.items():
        if lat_s <= lat <= lat_n and lon_w <= lon <= lon_e:
            res = _MODEL_RESOLUTION_DEG.get(model_name, 0.05)
            matches.append((model_name, res))

    if not matches:
        return None, None

    matches.sort(key=lambda m: m[1])
    primary = matches[0][0]
    fallback = matches[1][0] if len(matches) > 1 else None
    return primary, fallback


def _build_regulargrid_url(model: str, date: datetime, cycle: int, fhr: int) -> str:
    return (
        f"{THREDDS_BASE}/{model}/MODELS/{date:%Y}/{date:%m}/{date:%d}/"
        f"{model.lower()}.t{cycle:02d}z.{date:%Y%m%d}.regulargrid.f{fhr:03d}.nc"
    )


def _get_grid(model: str, url: str) -> dict[str, Any] | None:
    """Fetch or retrieve cached grid coordinates for an OFS model."""
    cache_key = f"{PROVIDER_ID}:grid:{model}"
    cached = get_cache().get(cache_key)
    if cached is not None:
        import numpy as np
        return {
            k: np.array(v) if isinstance(v, list) else v
            for k, v in cached.items()
        }

    try:
        import numpy as np
        import xarray as xr

        ds = xr.open_dataset(url, engine="netcdf4")
        grid_raw = {
            "lat": ds["Latitude"].values,
            "lon": ds["Longitude"].values,
            "depth": ds["Depth"].values if "Depth" in ds else None,
            "mask": ds["mask"].values if "mask" in ds else None,
            "h": ds["h"].values if "h" in ds else None,
        }
        ds.close()

        if grid_raw["mask"] is not None and np.all(grid_raw["mask"] == 0):
            return None

        # Cache needs JSON-serializable data; convert numpy arrays to lists.
        # _find_nearest_water_point and _extract_data work with both.
        grid_for_cache = {
            k: v.tolist() if hasattr(v, "tolist") else v
            for k, v in grid_raw.items()
        }
        get_cache().set(cache_key, grid_for_cache, ttl_seconds=_GRID_CACHE_TTL)
        return grid_raw
    except Exception:
        logger.warning("Failed to fetch grid for OFS model %s", model, exc_info=True)
        return None


def _find_nearest_water_point(
    grid: dict[str, Any], lat: float, lon: float
) -> tuple[int, int] | None:
    """Find the nearest water grid point to (lat, lon)."""
    import numpy as np

    lat_grid = grid["lat"]
    lon_grid = grid["lon"]
    mask = grid["mask"]

    dist = np.sqrt((lat_grid - lat) ** 2 + (lon_grid - lon) ** 2)

    if mask is not None:
        dist[mask == 0] = np.inf

    min_dist = dist.min()
    if np.isinf(min_dist):
        return None
    if min_dist > 0.5:
        return None

    iy, ix = np.unravel_index(dist.argmin(), dist.shape)
    return int(iy), int(ix)


def _extract_data(url: str, grid: dict[str, Any], iy: int, ix: int) -> dict[str, Any] | None:
    """Extract ocean data from one regulargrid file at the given grid point."""
    try:
        import numpy as np
        import xarray as xr

        ds = xr.open_dataset(url, engine="netcdf4")

        result: dict[str, Any] = {}

        if "temp" in ds:
            temp_data = ds["temp"].isel(time=0, ny=iy, nx=ix)
            if "Depth" in ds["temp"].dims:
                temp_values = temp_data.values
                depth_values = grid["depth"]
                profile = []
                for d_idx in range(len(depth_values)):
                    t_val = float(temp_values[d_idx])
                    if not np.isnan(t_val):
                        layer: dict[str, Any] = {
                            "depth_m": float(depth_values[d_idx]),
                            "temp_c": t_val,
                        }
                        profile.append(layer)
                result["column_profile"] = profile if profile else None
                result["surface_temp"] = profile[0]["temp_c"] if profile else None
            else:
                val = float(temp_data.values)
                result["surface_temp"] = val if not np.isnan(val) else None

        if "salt" in ds:
            salt_data = ds["salt"].isel(time=0, ny=iy, nx=ix)
            if "Depth" in ds["salt"].dims:
                salt_values = salt_data.values
                if result.get("column_profile"):
                    for i, layer in enumerate(result["column_profile"]):
                        if i < len(salt_values):
                            s_val = float(salt_values[i])
                            if not np.isnan(s_val):
                                layer["salt_psu"] = s_val
                s_surface = float(salt_values[0])
                result["surface_salinity"] = s_surface if not np.isnan(s_surface) else None
            else:
                val = float(salt_data.values)
                result["surface_salinity"] = val if not np.isnan(val) else None

        if "u_eastward" in ds and "v_northward" in ds:
            u_data = ds["u_eastward"].isel(time=0, ny=iy, nx=ix)
            v_data = ds["v_northward"].isel(time=0, ny=iy, nx=ix)
            if "Depth" in ds["u_eastward"].dims:
                u_val = float(u_data.isel(Depth=0).values)
                v_val = float(v_data.isel(Depth=0).values)
            else:
                u_val = float(u_data.values)
                v_val = float(v_data.values)
            if not (np.isnan(u_val) or np.isnan(v_val)):
                result["surface_current_speed"] = math.sqrt(u_val**2 + v_val**2)
                result["surface_current_dir"] = (math.degrees(math.atan2(u_val, v_val)) + 360) % 360

        if "zeta" in ds:
            zeta_val = float(ds["zeta"].isel(time=0, ny=iy, nx=ix).values)
            result["water_level_msl"] = zeta_val if not np.isnan(zeta_val) else None

        if "zetatomllw" in ds:
            mllw_val = float(ds["zetatomllw"].isel(time=0, ny=iy, nx=ix).values)
            result["water_level_mllw"] = mllw_val if not np.isnan(mllw_val) else None

        if grid["h"] is not None:
            h_val = float(grid["h"][iy, ix])
            result["seafloor_depth_m"] = h_val if not np.isnan(h_val) else None

        ds.close()
        return result if result.get("surface_temp") is not None else None

    except Exception:
        logger.warning("OFS data extraction failed for %s", url, exc_info=True)
        return None


def _select_cycle(model: str) -> list[tuple[datetime, int]]:
    """Return a list of (date, cycle_hour) candidates to try, most recent first."""
    cfg = OFS_MODELS.get(model)
    if cfg is None:
        return []

    now = datetime.now(tz=UTC)
    cycles = sorted(cfg["cycles"], reverse=True)
    candidates: list[tuple[datetime, int]] = []

    for hours_back in range(0, 48, 6):
        candidate_time = datetime(
            now.year, now.month, now.day, now.hour, tzinfo=UTC
        )
        from datetime import timedelta

        candidate_time = candidate_time - timedelta(hours=hours_back)
        for c in cycles:
            if candidate_time.hour >= c or hours_back > 0:
                from datetime import timedelta as td

                cycle_dt = candidate_time.replace(hour=0, minute=0, second=0) + td(hours=c)
                if cycle_dt <= now and (cycle_dt, c) not in candidates:
                    candidates.append((cycle_dt, c))
                    if len(candidates) >= _MAX_CYCLE_FALLBACKS:
                        return candidates
    return candidates


def fetch(*, model: str, lat: float, lon: float) -> dict[str, Any]:
    """Fetch ocean data from an OFS model at the given coordinates.

    Returns dict with surface_temp, column_profile, surface_current_speed,
    surface_current_dir, surface_salinity, water_level_msl, water_level_mllw,
    seafloor_depth_m. All temperatures in Celsius, speeds in m/s, salinity
    in PSU. Returns dict with source="unavailable" on failure.
    """
    empty: dict[str, Any] = {"source": f"ofs:{model}", "source_type": "modeled", "coverage_tier": "unavailable"}

    if model not in OFS_MODELS:
        logger.error("Unknown OFS model: %r", model)
        return empty

    cache_key = f"{PROVIDER_ID}:{model}:{lat:.3f}:{lon:.3f}"
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cached

    cfg = OFS_MODELS[model]
    candidates = _select_cycle(model)
    if not candidates:
        logger.warning("No valid cycle candidates for OFS model %s", model)
        return empty

    for cycle_dt, cycle_hour in candidates:
        first_fhr = cfg["step"]
        url = _build_regulargrid_url(model, cycle_dt, cycle_hour, first_fhr)

        grid = _get_grid(model, url)
        if grid is None:
            continue

        point = _find_nearest_water_point(grid, lat, lon)
        if point is None:
            logger.debug("OFS %s: point (%.4f, %.4f) is on land", model, lat, lon)
            return empty

        iy, ix = point
        data = _extract_data(url, grid, iy, ix)
        if data is not None:
            data["source"] = f"ofs:{model}"
            data["source_type"] = "modeled"
            data["coverage_tier"] = "ofs"
            get_cache().set(cache_key, data, ttl_seconds=_RESULT_CACHE_TTL)
            return data

    logger.warning("All OFS cycles exhausted for %s at (%.4f, %.4f)", model, lat, lon)
    return empty


def fetch_forecast(*, model: str, lat: float, lon: float) -> list[dict[str, Any]]:
    """Fetch water temperature forecast time series from an OFS model.

    Returns a list of dicts, one per forecast hour:
    [{"time": "2026-07-16T18:00:00Z", "water_temp_c": 21.3}, ...]

    Each entry corresponds to one regulargrid file (f003, f006, ..., f{max_fhr}).
    Entries are sorted by ascending time. Returns an empty list on total
    failure — callers treat empty the same as "unavailable" without raising.

    Cached with the same TTL as ``fetch()`` (1800 s).  Grid coordinates
    share the existing per-model cache so they are not re-fetched when
    ``fetch()`` has already been called for the same location.
    """
    cache_key = f"{PROVIDER_ID}:forecast:{model}:{lat:.3f}:{lon:.3f}"
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cached

    if model not in OFS_MODELS:
        logger.error("Unknown OFS model for forecast: %r", model)
        return []

    cfg = OFS_MODELS[model]
    candidates = _select_cycle(model)
    if not candidates:
        logger.warning("No valid cycle candidates for OFS model %s forecast", model)
        return []

    step: int = cfg["step"]
    max_fhr: int = cfg["max_fhr"]

    for cycle_dt, cycle_hour in candidates:
        first_url = _build_regulargrid_url(model, cycle_dt, cycle_hour, step)

        grid = _get_grid(model, first_url)
        if grid is None:
            continue

        point = _find_nearest_water_point(grid, lat, lon)
        if point is None:
            logger.debug(
                "OFS %s forecast: point (%.4f, %.4f) is on land — no forecast available",
                model,
                lat,
                lon,
            )
            return []

        iy, ix = point
        series: list[dict[str, Any]] = []

        for fhr in range(step, max_fhr + 1, step):
            url = _build_regulargrid_url(model, cycle_dt, cycle_hour, fhr)
            try:
                import numpy as np
                import xarray as xr

                ds = xr.open_dataset(url, engine="netcdf4")

                if "temp" not in ds:
                    ds.close()
                    logger.debug("OFS %s fhr=%03d: no 'temp' variable in dataset", model, fhr)
                    continue

                temp_data = ds["temp"].isel(time=0, ny=iy, nx=ix)

                # Models with a depth dimension: index surface (Depth=0).
                # Models without depth: read the scalar directly.
                if "Depth" in ds["temp"].dims:
                    temp_val = float(temp_data.isel(Depth=0).values)
                else:
                    temp_val = float(temp_data.values)

                ds.close()

                if np.isnan(temp_val):
                    logger.debug(
                        "OFS %s fhr=%03d: surface temp is NaN at (%.4f, %.4f)",
                        model,
                        fhr,
                        lat,
                        lon,
                    )
                    continue

                forecast_time = cycle_dt + timedelta(hours=fhr)
                series.append(
                    {
                        "time": forecast_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "water_temp_c": temp_val,
                    }
                )
            except Exception:
                logger.debug(
                    "OFS %s forecast: failed to extract fhr=%03d from %s",
                    model,
                    fhr,
                    url,
                    exc_info=True,
                )
                continue

        if series:
            series.sort(key=lambda e: e["time"])
            get_cache().set(cache_key, series, ttl_seconds=_RESULT_CACHE_TTL)
            return series

        # No data points from this cycle — try the previous one.
        logger.debug(
            "OFS %s forecast: cycle %s/%02dz yielded 0 points, trying earlier cycle",
            model,
            cycle_dt.strftime("%Y-%m-%d"),
            cycle_hour,
        )

    logger.warning(
        "OFS forecast: all cycles exhausted for %s at (%.4f, %.4f)", model, lat, lon
    )
    return []


def fetch_surface_currents(
    bbox: tuple[float, float, float, float],
    forecast_hours: int = 48,
) -> list[dict[str, Any]]:
    """Fetch gridded OFS surface currents covering a bounding box.

    Used by the SWAN nearshore wave model to include current forcing
    (PROVIDER-MANUAL §14.15, T2.2).  Returns one entry per forecast
    timestep with full 2-D U/V grids interpolated onto the OFS regulargrid
    cells that fall within ``bbox``.

    Args:
        bbox: (lon_sw, lat_sw, lon_ne, lat_ne) — SWAN outer domain in
            geographic degrees.
        forecast_hours: Maximum number of forecast hours to retrieve
            (default 48).  Capped at the OFS model's own ``max_fhr``.

    Returns:
        List of dicts, one per OFS forecast timestep:
          ``{"time": ISO-8601 str,
             "u":  list[list[float]],   # eastward m/s, shape [nj][ni]
             "v":  list[list[float]],   # northward m/s, shape [nj][ni]
             "ni": int, "nj": int,
             "lat_first": float, "lon_first": float,
             "lat_last":  float, "lon_last":  float}``
        Empty list on any failure — SWAN runs without current input
        (safe default; current forcing is optional per SWAN manual).

    Notes:
        NaN current values (land-masked cells) are replaced with 0.0
        so SWAN sees calm water rather than an invalid sentinel.
        Variable-name conventions tried in order: ``water_u``/``water_v``
        then ``u_eastward``/``v_northward``.
    """
    try:
        import numpy as np
        import xarray as xr
    except ImportError:
        logger.debug("fetch_surface_currents: xarray/numpy not installed; skipping")
        return []

    lon_sw, lat_sw, lon_ne, lat_ne = bbox
    center_lat = (lat_sw + lat_ne) / 2.0
    center_lon = (lon_sw + lon_ne) / 2.0

    model, _ = find_ofs_model(center_lat, center_lon)
    if model is None:
        logger.debug(
            "fetch_surface_currents: no OFS model covers bbox center "
            "(%.4f, %.4f); skipping current forcing",
            center_lat,
            center_lon,
        )
        return []

    cfg = OFS_MODELS[model]
    step: int = cfg["step"]
    max_fhr: int = min(cfg["max_fhr"], forecast_hours)
    candidates = _select_cycle(model)
    if not candidates:
        logger.debug("fetch_surface_currents: no OFS cycle candidates for %s", model)
        return []

    for cycle_dt, cycle_hour in candidates:
        first_url = _build_regulargrid_url(model, cycle_dt, cycle_hour, step)

        # Read grid coordinates to determine bbox indices.
        try:
            ds0 = xr.open_dataset(first_url, engine="netcdf4")
            lat_2d: Any = ds0["Latitude"].values   # shape [ny, nx]
            lon_2d: Any = ds0["Longitude"].values  # shape [ny, nx]
            ds0.close()
        except Exception:
            logger.debug(
                "fetch_surface_currents: could not open %s; trying earlier cycle",
                first_url,
                exc_info=True,
            )
            continue

        # For OFS regulargrid, lat varies along axis 0 (ny) and lon along axis 1 (nx).
        lat_1d = lat_2d[:, 0]
        lon_1d = lon_2d[0, :]

        j_indices = np.where((lat_1d >= lat_sw) & (lat_1d <= lat_ne))[0]
        i_indices = np.where((lon_1d >= lon_sw) & (lon_1d <= lon_ne))[0]

        if len(j_indices) == 0 or len(i_indices) == 0:
            logger.debug(
                "fetch_surface_currents: bbox does not overlap OFS %s grid domain",
                model,
            )
            return []

        j0, j1 = int(j_indices[0]), int(j_indices[-1]) + 1
        i0, i1 = int(i_indices[0]), int(i_indices[-1]) + 1

        lat_sub = lat_2d[j0:j1, i0:i1]
        lon_sub = lon_2d[j0:j1, i0:i1]
        nj, ni = lat_sub.shape
        lat_first = float(lat_sub[0, 0])
        lat_last = float(lat_sub[-1, 0])
        lon_first = float(lon_sub[0, 0])
        lon_last = float(lon_sub[0, -1])

        results: list[dict[str, Any]] = []

        for fhr in range(step, max_fhr + 1, step):
            url = _build_regulargrid_url(model, cycle_dt, cycle_hour, fhr)
            try:
                ds = xr.open_dataset(url, engine="netcdf4")

                # Resolve current variable names — try multiple OFS conventions.
                u_var: str | None = None
                v_var: str | None = None
                for u_name, v_name in (
                    ("water_u", "water_v"),
                    ("u_eastward", "v_northward"),
                    ("u", "v"),
                ):
                    if u_name in ds and v_name in ds:
                        u_var, v_var = u_name, v_name
                        break

                if u_var is None:
                    ds.close()
                    logger.debug(
                        "fetch_surface_currents: no U/V variables in OFS %s fhr=%03d",
                        model,
                        fhr,
                    )
                    continue

                u_da = ds[u_var].isel(time=0)
                v_da = ds[v_var].isel(time=0)

                # Select surface (depth index 0) when a vertical dimension is present.
                for depth_dim in ("Depth", "depth", "s_rho", "s_w", "sigma"):
                    if depth_dim in u_da.dims:
                        u_da = u_da.isel(**{depth_dim: 0})
                        v_da = v_da.isel(**{depth_dim: 0})
                        break

                # Spatial subset via OFS regulargrid ny/nx dimension names.
                u_arr = u_da.values[j0:j1, i0:i1]
                v_arr = v_da.values[j0:j1, i0:i1]

                forecast_time = cycle_dt + timedelta(hours=fhr)
                t_iso = forecast_time.strftime("%Y-%m-%dT%H:%M:%SZ")

                ds.close()

                # Replace NaN (land-masked) with 0.0 (calm — no effect on SWAN).
                u_clean = np.where(np.isnan(u_arr), 0.0, u_arr)
                v_clean = np.where(np.isnan(v_arr), 0.0, v_arr)

                results.append({
                    "time": t_iso,
                    "u": u_clean.tolist(),
                    "v": v_clean.tolist(),
                    "ni": ni,
                    "nj": nj,
                    "lat_first": lat_first,
                    "lon_first": lon_first,
                    "lat_last": lat_last,
                    "lon_last": lon_last,
                })

            except Exception:
                logger.debug(
                    "fetch_surface_currents: failed fhr=%03d from %s",
                    fhr,
                    url,
                    exc_info=True,
                )
                continue

        if results:
            logger.info(
                "fetch_surface_currents: OFS %s cycle %s/%02dz → %d timestep(s) "
                "(%dx%d grid, bbox=%s)",
                model,
                cycle_dt.strftime("%Y-%m-%d"),
                cycle_hour,
                len(results),
                ni,
                nj,
                bbox,
            )
            return results

        logger.debug(
            "fetch_surface_currents: OFS %s cycle %s/%02dz yielded 0 current grids; "
            "trying earlier cycle",
            model,
            cycle_dt.strftime("%Y-%m-%d"),
            cycle_hour,
        )

    logger.warning(
        "fetch_surface_currents: all OFS cycles exhausted for bbox %s; "
        "SWAN will run without current forcing",
        bbox,
    )
    return []
