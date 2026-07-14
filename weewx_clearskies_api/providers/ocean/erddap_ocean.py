"""ERDDAP ocean data provider module (ADR-091, PROVIDER-MANUAL §14.11).

Config-driven provider that fetches ocean data from ERDDAP griddap servers.
Single module handles multiple datasets: NASA MUR SST, NOAA RTOFS (2D + 3D),
PacIOOS ROMS (Hawaii), CARICOOS FVCOM (Caribbean).

Used as fallback when OFS (THREDDS) is unavailable or doesn't cover a location.
The ocean data resolver (services/ocean_data_resolver.py) orchestrates calls —
endpoints never call this module directly.

Wire format: ERDDAP griddap JSON response with table.columnNames + table.rows.
"""

from __future__ import annotations

import logging
from typing import Any

from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient

logger = logging.getLogger(__name__)

PROVIDER_ID = "erddap_ocean"
DOMAIN = "ocean"

DATASETS: dict[str, dict[str, Any]] = {
    "mur_sst": {
        "server": "coastwatch.pfeg.noaa.gov",
        "dataset_id": "jplMURSST41",
        "temp_var": "analysed_sst",
        "has_depth": False,
        "lon_convention": "signed",
        "cache_ttl": 3600,
    },
    "rtofs_3d": {
        "server": "coastwatch.pfeg.noaa.gov",
        "dataset_id": "ncepRtofsG3DForeDaily",
        "temp_var": "temperature",
        "salt_var": "salinity",
        "u_var": "u",
        "v_var": "v",
        "has_depth": True,
        "lon_convention": "positive",
        "cache_ttl": 1800,
    },
    "rtofs_2d": {
        "server": "coastwatch.pfeg.noaa.gov",
        "dataset_id": "ncepRtofsG2DFore3hrlyProg",
        "temp_var": "sst",
        "has_depth": False,
        "lon_convention": "positive",
        "cache_ttl": 1800,
    },
    "pacioos": {
        "server": "pae-paha.pacioos.hawaii.edu",
        "dataset_id": "roms_hiig",
        "temp_var": "temp",
        "salt_var": "salt",
        "has_depth": True,
        "lon_convention": "signed",
        "cache_ttl": 1800,
    },
    "caricoos": {
        "server": "dm3.caricoos.org",
        "dataset_id": "FVCOM_Historical_3D_StructuredGrid",
        "temp_var": "temp",
        "salt_var": "salt",
        "has_depth": True,
        "lon_convention": "signed",
        "cache_ttl": 1800,
    },
}

_http_client: ProviderHTTPClient | None = None


def _get_http_client() -> ProviderHTTPClient:
    global _http_client  # noqa: PLW0603
    if _http_client is None:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            base_url="",
            timeout_seconds=15,
        )
    return _http_client


def _normalize_lon(lon: float, convention: str) -> float:
    if convention == "positive" and lon < 0:
        return lon + 360.0
    return lon


def _build_url(config: dict[str, Any], lat: float, lon: float, *, depth_all: bool = False) -> str:
    server = config["server"]
    dataset_id = config["dataset_id"]
    norm_lon = _normalize_lon(lon, config["lon_convention"])
    lat_str = f"[({lat:.4f})]"
    lon_str = f"[({norm_lon:.4f})]"
    depth_str = "[(0):(last)]" if depth_all else "[(0)]"

    variables = [config["temp_var"]]
    if "salt_var" in config:
        variables.append(config["salt_var"])
    if "u_var" in config:
        variables.extend([config["u_var"], config["v_var"]])

    constraints_parts = []
    for var in variables:
        if config["has_depth"]:
            constraints_parts.append(f"{var}[(last)]{depth_str}{lat_str}{lon_str}")
        else:
            constraints_parts.append(f"{var}[(last)]{lat_str}{lon_str}")

    constraints = ",".join(constraints_parts)
    return f"https://{server}/erddap/griddap/{dataset_id}.json?{constraints}"


def _parse_response(
    data: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any] | None:
    table = data.get("table")
    if not table:
        return None
    columns = table.get("columnNames", [])
    rows = table.get("rows", [])
    if not rows:
        return None

    temp_var = config["temp_var"]
    salt_var = config.get("salt_var")
    u_var = config.get("u_var")
    v_var = config.get("v_var")
    has_depth = config["has_depth"]

    def _col_idx(name: str) -> int | None:
        try:
            return columns.index(name)
        except ValueError:
            return None

    temp_idx = _col_idx(temp_var)
    salt_idx = _col_idx(salt_var) if salt_var else None
    u_idx = _col_idx(u_var) if u_var else None
    v_idx = _col_idx(v_var) if v_var else None
    depth_idx = _col_idx("depth") if has_depth else None

    if temp_idx is None:
        return None

    import math

    if not has_depth:
        val = rows[0][temp_idx]
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return None
        return {
            "surface_temp": float(val),
            "column_profile": None,
            "surface_salinity": (
                float(rows[0][salt_idx])
                if salt_idx is not None and rows[0][salt_idx] is not None
                else None
            ),
        }

    profile: list[dict[str, Any]] = []
    for row in rows:
        temp_val = row[temp_idx] if temp_idx is not None else None
        if temp_val is None or (isinstance(temp_val, float) and math.isnan(temp_val)):
            continue
        depth_val = row[depth_idx] if depth_idx is not None else 0.0
        layer: dict[str, Any] = {
            "depth_m": float(depth_val),
            "temp_c": float(temp_val),
        }
        if salt_idx is not None and row[salt_idx] is not None:
            salt_v = row[salt_idx]
            if not (isinstance(salt_v, float) and math.isnan(salt_v)):
                layer["salt_psu"] = float(salt_v)
        profile.append(layer)

    if not profile:
        return None

    surface_temp = profile[0]["temp_c"]
    surface_salinity = profile[0].get("salt_psu")

    surface_current_speed: float | None = None
    surface_current_dir: float | None = None
    if u_idx is not None and v_idx is not None:
        u_val = rows[0][u_idx]
        v_val = rows[0][v_idx]
        if u_val is not None and v_val is not None:
            u_f, v_f = float(u_val), float(v_val)
            if not (math.isnan(u_f) or math.isnan(v_f)):
                surface_current_speed = math.sqrt(u_f**2 + v_f**2)
                surface_current_dir = (math.degrees(math.atan2(u_f, v_f)) + 360) % 360

    return {
        "surface_temp": surface_temp,
        "column_profile": profile,
        "surface_salinity": surface_salinity,
        "surface_current_speed": surface_current_speed,
        "surface_current_dir": surface_current_dir,
    }


def fetch(*, dataset: str, lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch ocean data from an ERDDAP griddap dataset.

    Returns dict with surface_temp, column_profile (when dataset has depth),
    surface_salinity, surface_current_speed/dir. Returns None on failure.
    """
    config = DATASETS.get(dataset)
    if config is None:
        logger.error("Unknown ERDDAP ocean dataset: %r", dataset)
        return None

    cache_key = f"{PROVIDER_ID}:{dataset}:{lat:.3f}:{lon:.3f}"
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cached

    has_depth = config["has_depth"]
    url = _build_url(config, lat, lon, depth_all=has_depth)

    try:
        response = _get_http_client().get(url)
        data = response.json()
    except Exception:
        logger.warning(
            "ERDDAP fetch failed for dataset %r at (%.4f, %.4f)",
            dataset, lat, lon, exc_info=True,
        )
        return None

    result = _parse_response(data, config)
    if result is not None:
        get_cache().set(cache_key, result, ttl_seconds=config["cache_ttl"])

    return result
