"""Bootstrap import orchestrator (ADR-068 T8.1).

Matches historical OpenAQ PM2.5 records against the weewx archive, computes
the clear-sky clearness index (Kcs = radiation / maxSolarRad) for each
qualifying record, and seeds the auto-calibration _samples list.

Read-only archive access: this module never INSERTs, UPDATEs, or DELETEs
from the weewx archive.  All writes go through auto_calibration.persist()
which writes only to /etc/weewx-clearskies/calibration.json.

Clean-sky sample criteria (must all pass):
  - PM2.5 < _PM25_CLEAN (12.0 µg/m³)
  - maxSolarRad > 100 W/m² (proxy for solar elevation > 10°)
  - rainRate = 0 or NULL
  - Kcs = radiation / maxSolarRad > 0.3
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

from weewx_clearskies_api.bootstrap.openaq_client import PMRecord
from weewx_clearskies_api.sse import auto_calibration

logger = logging.getLogger(__name__)

# Constants mirrored from auto_calibration for clean-sky gate logic.
_PM25_CLEAN = auto_calibration._PM25_CLEAN         # 12.0 µg/m³
_MIN_MAX_SOLAR_RAD = 100.0   # W/m² — proxy for solar elevation > 10°
_MIN_KCS = 0.3               # Sun actually shining (not just above horizon)
_ARCHIVE_MATCH_WINDOW = 1800  # ±30 minutes (seconds)


def run_bootstrap(
    engine: Engine,
    pm_records: list[PMRecord],
    station_lat: float,
    station_lon: float,
    station_alt_m: float,
) -> dict:
    """Import PM records and match against weewx archive to build calibration samples.

    For each PM record:
      1. Find nearest weewx archive record by timestamp (within ±30 min).
      2. Read radiation and maxSolarRad from the archive row.
      3. If maxSolarRad is NULL, recompute via
         auto_calibration.compute_max_solar_rad().
      4. Compute Kcs = radiation / maxSolarRad. Skip if either is 0 or NULL.
      5. Apply clean-sky criteria:
           - PM2.5 < 12.0 µg/m³
           - maxSolarRad > 100 W/m²
           - rainRate = 0 or NULL
           - Kcs > 0.3
      6. Append qualifying (timestamp, kcs) to auto_calibration._samples.

    After all records: recompute baseline and persist to calibration.json.

    Args:
        engine:       SQLAlchemy engine pointing at the weewx archive (read-only).
        pm_records:   PM2.5 records from OpenAQ, sorted ascending by timestamp.
        station_lat:  Station latitude (decimal degrees).
        station_lon:  Station longitude (decimal degrees).
        station_alt_m: Station altitude (metres above sea level).

    Returns:
        Summary dict with counters and final calibration state.
    """
    counters = {
        "total_pm_records": len(pm_records),
        "archive_matched": 0,
        "skipped_no_archive": 0,
        "skipped_no_radiation": 0,
        "skipped_max_solar_low": 0,
        "skipped_rain": 0,
        "skipped_kcs_low": 0,
        "skipped_pm_high": 0,
        "clean_sky_samples": 0,
    }

    for pm_rec in pm_records:
        ts = pm_rec.timestamp_utc
        pm25 = pm_rec.pm25

        # Gate: PM2.5 must be below clean threshold.
        if pm25 >= _PM25_CLEAN:
            counters["skipped_pm_high"] += 1
            continue

        # Query the nearest archive record within ±30 minutes.
        arch_row = _find_nearest_archive_record(engine, ts)
        if arch_row is None:
            counters["skipped_no_archive"] += 1
            continue

        counters["archive_matched"] += 1

        arch_ts, radiation, max_solar_rad, rain_rate = arch_row

        # Gate: radiation must not be NULL.
        if radiation is None:
            counters["skipped_no_radiation"] += 1
            continue

        try:
            radiation = float(radiation)
        except (TypeError, ValueError):
            counters["skipped_no_radiation"] += 1
            continue

        # If maxSolarRad is NULL, recompute via auto_calibration.
        if max_solar_rad is None:
            max_solar_rad = auto_calibration.compute_max_solar_rad(
                lat=station_lat,
                lon=station_lon,
                altitude_m=station_alt_m,
                unix_ts=float(arch_ts),
            )

        if max_solar_rad is None:
            counters["skipped_no_radiation"] += 1
            continue

        try:
            max_solar_rad = float(max_solar_rad)
        except (TypeError, ValueError):
            counters["skipped_no_radiation"] += 1
            continue

        # Gate: maxSolarRad must be above minimum (sun high enough).
        if max_solar_rad <= _MIN_MAX_SOLAR_RAD:
            counters["skipped_max_solar_low"] += 1
            continue

        # Gate: no rain in the archive record.
        try:
            rr = float(rain_rate) if rain_rate is not None else 0.0
        except (TypeError, ValueError):
            rr = 0.0

        if rr > 0.0:
            counters["skipped_rain"] += 1
            continue

        # Gate: Kcs must be > 0.3.
        if max_solar_rad == 0.0:
            counters["skipped_kcs_low"] += 1
            continue

        kcs = radiation / max_solar_rad
        if kcs <= _MIN_KCS:
            counters["skipped_kcs_low"] += 1
            continue

        # All criteria met — add sample.
        auto_calibration._samples.append((float(arch_ts), kcs))  # noqa: SLF001
        counters["clean_sky_samples"] += 1

    # Sort samples chronologically (we appended in PM-record order, which may
    # not be perfectly aligned with archive timestamps after matching).
    auto_calibration._samples.sort(key=lambda x: x[0])  # noqa: SLF001

    # Recompute baseline from accumulated samples.
    new_baseline = auto_calibration.compute_baseline()
    if new_baseline is not None:
        auto_calibration._current_baseline = new_baseline  # noqa: SLF001
        from weewx_clearskies_api.sse import haze_condition  # noqa: PLC0415
        haze_condition.set_baseline(new_baseline)
        logger.info(
            "Bootstrap calibration baseline set: Kcs=%.4f from %d samples",
            new_baseline,
            counters["clean_sky_samples"],
        )

    # Persist to disk.
    auto_calibration.persist()

    # Build final calibration state for the summary.
    cal_state = auto_calibration.get_calibration_state()

    return {
        **counters,
        "calibration_state": cal_state["state"],
        "baseline_kcs": cal_state["baseline_kcs"],
        "persist_path": auto_calibration._PERSIST_PATH,  # noqa: SLF001
    }


def _find_nearest_archive_record(
    engine: Engine,
    target_ts: float,
) -> tuple | None:
    """Return the nearest archive row within ±30 minutes of target_ts.

    The weewx archive dateTime column is Unix epoch seconds.

    Returns:
        Tuple of (dateTime, radiation, maxSolarRad, rainRate), or None when
        no record falls within the ±30-minute window.

    SQL is fully parameterized — no string interpolation.
    """
    start = target_ts - _ARCHIVE_MATCH_WINDOW
    end = target_ts + _ARCHIVE_MATCH_WINDOW

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT dateTime, radiation, maxSolarRad, rainRate "
                    "FROM archive "
                    "WHERE dateTime BETWEEN :start AND :end "
                    "ORDER BY ABS(dateTime - :target) "
                    "LIMIT 1"
                ),
                {"start": start, "end": end, "target": target_ts},
            )
            row = result.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Archive lookup failed for ts=%.0f: %s", target_ts, exc)
        return None

    if row is None:
        return None

    return tuple(row)
