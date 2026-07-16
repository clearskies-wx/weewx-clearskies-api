"""Ocean data resolver — provider-agnostic fallback chain (ADR-091, API-MANUAL §16).

Endpoints call ``resolve()`` instead of calling ocean providers directly.
The resolver implements the tiered fallback chain from ADR-091 Decision 2:

    Tier 1: On-premises sensor (when configured and within threshold)
    Tier 2: NOAA OFS regional coastal model (via THREDDS/OPeNDAP)
    Tier 3a (surface): NASA MUR SST (via ERDDAP)
    Tier 3b (column+forecast): NOAA RTOFS (via ERDDAP)
    Tier 4: Unavailable

Each tier is independently wrapped in try/except — a failure at one tier
logs a warning and falls through to the next.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from weewx_clearskies_api.models.responses import OceanDataResult

logger = logging.getLogger(__name__)


def resolve(
    lat: float,
    lon: float,
    location_config: dict[str, Any],
    mode: str = "modeled",
    needs: str = "surface",
) -> OceanDataResult:
    """Resolve ocean data for a marine location through the tiered fallback chain.

    Args:
        lat: Latitude.
        lon: Longitude.
        location_config: Dict with keys ``ofs_model``, ``ofs_fallback``,
            ``ofs_region`` from api.conf location config.
        mode: ``"modeled"`` (default — run full tier chain) or ``"observed"``
            (return only a real sensor reading; never silently falls back to
            modeled data).
        needs: ``"surface"`` (default — surface temp sufficient) or ``"full"``
            (request water column profile, currents, salinity, forecast).

    Returns:
        ``OceanDataResult`` with ``coverage_tier`` indicating which tier
        supplied the data. All temperatures in Celsius, speeds in m/s,
        salinity in PSU — unit conversion happens in the endpoint.
    """
    if mode == "observed":
        return _resolve_observed(lat, lon, location_config)

    return _resolve_modeled(lat, lon, location_config, needs)


def resolve_forecast(
    lat: float,
    lon: float,
    location_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return water temperature forecast time series from the OFS model.

    Tries the configured primary OFS model first, then the fallback model.
    Does not fall through to ERDDAP — ERDDAP sources do not supply a
    regulargrid forecast time series comparable to OFS.

    Returns a list of dicts:
        [{"time": "2026-07-16T18:00:00Z", "water_temp_c": 21.3}, ...]

    Returns an empty list when OFS is not configured, when the model is
    unavailable, or when the point is on land.  Callers treat an empty
    list as "no forecast data" without raising.

    All temperatures are in Celsius — unit conversion is the caller's
    responsibility (same contract as ``resolve()``).
    """
    from weewx_clearskies_api.providers.ocean import ofs  # noqa: PLC0415

    ofs_model: str | None = location_config.get("ofs_model")
    ofs_fallback: str | None = location_config.get("ofs_fallback")

    if ofs_model:
        try:
            series = ofs.fetch_forecast(model=ofs_model, lat=lat, lon=lon)
            if series:
                return series
        except Exception:
            logger.warning(
                "OFS primary model %s forecast failed at (%.4f, %.4f)",
                ofs_model,
                lat,
                lon,
                exc_info=True,
            )

    if ofs_fallback:
        try:
            series = ofs.fetch_forecast(model=ofs_fallback, lat=lat, lon=lon)
            if series:
                return series
        except Exception:
            logger.warning(
                "OFS fallback model %s forecast failed at (%.4f, %.4f)",
                ofs_fallback,
                lat,
                lon,
                exc_info=True,
            )

    return []


def _resolve_observed(
    lat: float, lon: float, location_config: dict[str, Any]
) -> OceanDataResult:
    """Return only physical sensor data. Never falls back to modeled."""
    return OceanDataResult(
        source="unavailable",
        source_type="observed",
        coverage_tier="unavailable",
    )


def _resolve_modeled(
    lat: float,
    lon: float,
    location_config: dict[str, Any],
    needs: str,
) -> OceanDataResult:
    """Run the full tiered fallback chain."""
    from weewx_clearskies_api.providers.ocean import erddap_ocean, ofs

    ofs_model = location_config.get("ofs_model")
    ofs_fallback = location_config.get("ofs_fallback")
    ofs_region = location_config.get("ofs_region")

    # --- Tier 2: OFS primary model ---
    if ofs_model:
        try:
            data = ofs.fetch(model=ofs_model, lat=lat, lon=lon)
            if data and data.get("surface_temp") is not None:
                return _build_result(data, needs)
        except Exception:
            logger.warning(
                "OFS primary model %s failed at (%.4f, %.4f)", ofs_model, lat, lon,
                exc_info=True,
            )

    # --- Tier 2 fallback: OFS secondary model ---
    if ofs_fallback:
        try:
            data = ofs.fetch(model=ofs_fallback, lat=lat, lon=lon)
            if data and data.get("surface_temp") is not None:
                return _build_result(data, needs)
        except Exception:
            logger.warning(
                "OFS fallback model %s failed at (%.4f, %.4f)", ofs_fallback, lat, lon,
                exc_info=True,
            )

    # --- Tier 3: Regional ERDDAP (PacIOOS / CARICOOS) ---
    if ofs_region:
        try:
            data = erddap_ocean.fetch(dataset=ofs_region, lat=lat, lon=lon)
            if data and data.get("surface_temp") is not None:
                data["source"] = f"erddap:{ofs_region}"
                data["source_type"] = "modeled"
                data["coverage_tier"] = "regional_erddap"
                return _build_result(data, needs)
        except Exception:
            logger.warning(
                "Regional ERDDAP %s failed at (%.4f, %.4f)", ofs_region, lat, lon,
                exc_info=True,
            )

    # --- Tier 4: Global fallback ---
    if needs == "full":
        # RTOFS 3D for column profiles + forecast
        try:
            data = erddap_ocean.fetch(dataset="rtofs_3d", lat=lat, lon=lon)
            if data and data.get("surface_temp") is not None:
                data["source"] = "rtofs"
                data["source_type"] = "modeled"
                data["coverage_tier"] = "rtofs"
                return _build_result(data, needs)
        except Exception:
            logger.warning(
                "RTOFS 3D failed at (%.4f, %.4f)", lat, lon, exc_info=True,
            )

    # MUR SST for surface temp (always tried as last resort for surface)
    try:
        data = erddap_ocean.fetch(dataset="mur_sst", lat=lat, lon=lon)
        if data and data.get("surface_temp") is not None:
            data["source"] = "mur_sst"
            data["source_type"] = "modeled"
            data["coverage_tier"] = "mur_sst"
            return _build_result(data, needs)
    except Exception:
        logger.warning(
            "MUR SST failed at (%.4f, %.4f)", lat, lon, exc_info=True,
        )

    # RTOFS 2D surface as final fallback for surface temp
    if needs == "surface":
        try:
            data = erddap_ocean.fetch(dataset="rtofs_2d", lat=lat, lon=lon)
            if data and data.get("surface_temp") is not None:
                data["source"] = "rtofs"
                data["source_type"] = "modeled"
                data["coverage_tier"] = "rtofs"
                return _build_result(data, needs)
        except Exception:
            logger.warning(
                "RTOFS 2D failed at (%.4f, %.4f)", lat, lon, exc_info=True,
            )

    return OceanDataResult(source="unavailable", source_type="modeled", coverage_tier="unavailable")


def _build_result(data: dict[str, Any], needs: str) -> OceanDataResult:
    """Build an OceanDataResult from raw provider data dict."""
    profile = data.get("column_profile")

    thermocline_depth_m: float | None = None
    bottom_temp_c: float | None = None

    if profile and len(profile) >= 2:
        # Thermocline: depth of maximum |dT/dz| gradient
        max_gradient = 0.0
        for i in range(len(profile) - 1):
            dz = profile[i + 1]["depth_m"] - profile[i]["depth_m"]
            if dz > 0:
                dt = abs(profile[i + 1]["temp_c"] - profile[i]["temp_c"])
                gradient = dt / dz
                if gradient > max_gradient:
                    max_gradient = gradient
                    thermocline_depth_m = (profile[i]["depth_m"] + profile[i + 1]["depth_m"]) / 2

        # Bottom temp: deepest non-null depth level
        bottom_temp_c = profile[-1]["temp_c"]

    from weewx_clearskies_api.models.responses import (
        OceanCurrentSnapshot,
        WaterColumnLayer,
        WaterColumnProfile,
    )

    column_profile_model: WaterColumnProfile | None = None
    if profile and needs == "full":
        layers = [
            WaterColumnLayer(
                depth_m=l["depth_m"],
                temperature=l["temp_c"],
                salinity=l.get("salt_psu"),
            )
            for l in profile
        ]
        from datetime import UTC, datetime

        column_profile_model = WaterColumnProfile(
            layers=layers,
            thermocline_depth_m=thermocline_depth_m,
            bottom_temp=bottom_temp_c,
            seafloor_depth_m=data.get("seafloor_depth_m"),
            source=data.get("source", "unknown"),
            timestamp=datetime.now(tz=UTC).isoformat(),
        )

    current_profile: list[OceanCurrentSnapshot] | None = None
    if data.get("surface_current_speed") is not None:
        current_profile = [
            OceanCurrentSnapshot(
                speed=data["surface_current_speed"],
                direction=data.get("surface_current_dir", 0.0),
                depth_m=0.0,
            )
        ]

    return OceanDataResult(
        surface_temp=data.get("surface_temp"),
        column_profile=column_profile_model,
        thermocline_depth_m=thermocline_depth_m,
        bottom_temp_c=bottom_temp_c,
        seafloor_depth_m=data.get("seafloor_depth_m"),
        surface_current_speed=data.get("surface_current_speed"),
        surface_current_dir=data.get("surface_current_dir"),
        current_profile=current_profile,
        surface_salinity=data.get("surface_salinity"),
        water_level_msl=data.get("water_level_msl"),
        water_level_mllw=data.get("water_level_mllw"),
        forecast=None,
        source=data.get("source", "unavailable"),
        source_type=data.get("source_type", "modeled"),
        coverage_tier=data.get("coverage_tier", "unavailable"),
    )
