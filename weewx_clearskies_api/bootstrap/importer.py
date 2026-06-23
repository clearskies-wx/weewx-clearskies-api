"""Bootstrap import orchestrator (ADR-068 T8.1, ADR-072 T2.2/T2.5).

Matches historical OpenAQ PM2.5 records against the weewx archive, computes
the clear-sky clearness index (Kcs = radiation / mcclear_ghi) for each
qualifying record, and seeds the auto-calibration monthly sample bins.

Clear-sky GHI is provided by CAMS McClear (via pvlib.iotools.get_cams()),
fetched before bootstrap begins and passed in as a timestamp→GHI lookup dict.
McClear provides atmosphere-adjusted clear-sky GHI at ground level with real
atmospheric conditions, eliminating the sunrise/sunset poison zone that
affected the Ryan-Stolzenbach fallback.

Read-only archive access: this module never INSERTs, UPDATEs, or DELETEs
from the weewx archive.  All writes go through auto_calibration.persist()
which writes only to /etc/weewx-clearskies/calibration.json.

Clean-sky sample criteria (must all pass):
  - PM2.5 < _PM25_CLEAN (12.0 µg/m³)
  - McClear GHI > 50 W/m² (proxy for solar elevation high enough)
  - rainRate = 0 or NULL
  - Kcs = radiation / mcclear_ghi in range (0.3, 1.5]
"""

from __future__ import annotations

import bisect
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from weewx_clearskies_api.bootstrap.openaq_client import PMRecord
from weewx_clearskies_api.sse import auto_calibration

logger = logging.getLogger(__name__)

# Constants mirrored from auto_calibration for clean-sky gate logic.
_PM25_CLEAN = auto_calibration._PM25_CLEAN         # 12.0 µg/m³  # noqa: SLF001
_MIN_CLEARSKY_GHI = 50.0     # W/m² — McClear GHI gate (realistic at low angles)
_MIN_KCS = 0.3               # Sun actually shining (not just above horizon)
_MAX_KCS = 1.5               # Defense-in-depth ceiling (cloud enhancement ~1.2-1.3)
_ARCHIVE_MATCH_WINDOW = 1800  # ±30 minutes (seconds)
_MCCLEAR_MATCH_WINDOW = 900   # ±15 minutes (McClear is at 15-min resolution)


def run_bootstrap(
    engine: Engine,
    pm_records: list[PMRecord],
    station_lat: float,
    station_lon: float,
    station_alt_m: float,
    mcclear_ghi: dict[int, float],
) -> dict:
    """Import PM records and match against weewx archive to build calibration samples.

    Clear-sky GHI is provided by CAMS McClear (mcclear_ghi lookup dict).
    For each PM record:
      1. Find nearest weewx archive record by timestamp (within ±30 min).
      2. Read radiation from the archive row.
      3. Look up McClear clear-sky GHI for the archive timestamp (±15 min).
      4. Compute Kcs = radiation / mcclear_ghi. Skip if either is 0 or NULL.
      5. Apply clean-sky criteria:
           - PM2.5 < 12.0 µg/m³
           - McClear GHI > 50 W/m²
           - rainRate = 0 or NULL
           - Kcs in range (0.3, 1.5]
      6. Append qualifying (timestamp, kcs) to the appropriate monthly bin in
         auto_calibration._monthly_samples, keyed by local calendar month.

    After all records: sort each month bin, recompute per-month baselines,
    recompute flat fallback, notify haze_condition, and persist.

    Args:
        engine:        SQLAlchemy engine pointing at the weewx archive (read-only).
        pm_records:    PM2.5 records from OpenAQ, sorted ascending by timestamp.
        station_lat:   Station latitude (decimal degrees).
        station_lon:   Station longitude (decimal degrees).
        station_alt_m: Station altitude (metres above sea level).
        mcclear_ghi:   Dict mapping Unix timestamp (int, UTC) to McClear clear-sky
                       GHI (W/m²), as returned by fetch_mcclear_clearsky_ghi().

    Returns:
        Summary dict with counters, per-month sample counts, and calibration state.
    """
    counters = {
        "total_pm_records": len(pm_records),
        "archive_matched": 0,
        "skipped_no_archive": 0,
        "skipped_no_radiation": 0,
        "skipped_max_solar_low": 0,
        "skipped_rain": 0,
        "skipped_kcs_low": 0,
        "skipped_kcs_high": 0,
        "skipped_pm_high": 0,
        "clean_sky_samples": 0,
    }

    # Pre-sort McClear keys for O(log n) nearest-neighbour lookup.
    _mcclear_sorted_keys = sorted(mcclear_ghi.keys())

    # Get station timezone for month binning.
    tz = ZoneInfo(auto_calibration._timezone_name)  # noqa: SLF001

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

        arch_ts, radiation, _max_solar_rad, rain_rate, _out_temp, _out_humidity = arch_row

        # Gate: radiation must not be NULL.
        if radiation is None:
            counters["skipped_no_radiation"] += 1
            continue

        try:
            radiation = float(radiation)
        except (TypeError, ValueError):
            counters["skipped_no_radiation"] += 1
            continue

        # Look up McClear clear-sky GHI for this archive timestamp.
        max_solar_rad = _lookup_mcclear_ghi(
            mcclear_ghi, _mcclear_sorted_keys, int(arch_ts)
        )

        if max_solar_rad is None:
            counters["skipped_no_radiation"] += 1
            continue

        # Gate: McClear GHI must be above minimum (sun high enough).
        if max_solar_rad <= _MIN_CLEARSKY_GHI:
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

        # Gate: Kcs ceiling (defense-in-depth against cloud enhancement outliers).
        if kcs > _MAX_KCS:
            counters["skipped_kcs_high"] += 1
            continue

        # All criteria met — bin sample by local calendar month.
        local_month = datetime.fromtimestamp(float(arch_ts), tz=tz).month
        auto_calibration._monthly_samples[local_month].append((float(arch_ts), kcs))  # noqa: SLF001
        counters["clean_sky_samples"] += 1

    # Sort each month bin chronologically after import.
    for m in range(1, 13):
        auto_calibration._monthly_samples[m].sort(key=lambda x: x[0])  # noqa: SLF001

    # Recompute per-month baselines.
    for m in range(1, 13):
        auto_calibration._monthly_baselines[m] = auto_calibration.compute_monthly_baseline(m)  # noqa: SLF001

    # Recompute flat fallback.
    auto_calibration._flat_baseline_update()  # noqa: SLF001

    # Notify haze_condition with current baseline.
    current = auto_calibration.get_current_baseline()
    if current is not None:
        from weewx_clearskies_api.sse import haze_condition  # noqa: PLC0415
        haze_condition.set_baseline(current)

    cal_state = auto_calibration.get_calibration_state()
    logger.info(
        "Bootstrap import complete: %d clean-sky samples across %d/12 months calibrated",
        counters["clean_sky_samples"],
        cal_state["months_calibrated"],
    )

    # Persist to disk.
    auto_calibration.persist()

    # Build per-month sample counts for the summary.
    per_month_counts = {m: len(auto_calibration._monthly_samples[m]) for m in range(1, 13)}  # noqa: SLF001

    return {
        **counters,
        "per_month_counts": per_month_counts,
        "months_calibrated": cal_state["months_calibrated"],
        "persist_path": auto_calibration._PERSIST_PATH,  # noqa: SLF001
    }


def _lookup_mcclear_ghi(
    mcclear_ghi: dict[int, float],
    sorted_keys: list[int],
    target_ts: int,
) -> float | None:
    """Find the nearest McClear GHI value within ±15 minutes of target_ts.

    Uses a pre-sorted key list and bisect for O(log n) lookup.

    Args:
        mcclear_ghi:  Timestamp → GHI dict (from fetch_mcclear_clearsky_ghi).
        sorted_keys:  Sorted list of keys from mcclear_ghi.
        target_ts:    Archive timestamp to look up (integer Unix seconds, UTC).

    Returns:
        Nearest GHI value (float) if within ±15 minutes, else None.
    """
    if not sorted_keys:
        return None

    pos = bisect.bisect_left(sorted_keys, target_ts)

    # Check candidates: the insertion point and the one before it.
    best_ts: int | None = None
    best_diff = _MCCLEAR_MATCH_WINDOW + 1

    for idx in (pos - 1, pos):
        if 0 <= idx < len(sorted_keys):
            diff = abs(sorted_keys[idx] - target_ts)
            if diff < best_diff:
                best_diff = diff
                best_ts = sorted_keys[idx]

    if best_ts is None or best_diff > _MCCLEAR_MATCH_WINDOW:
        return None

    return mcclear_ghi[best_ts]


def _find_nearest_archive_record(
    engine: Engine,
    target_ts: float,
) -> tuple | None:
    """Return the nearest archive row within ±30 minutes of target_ts.

    The weewx archive dateTime column is Unix epoch seconds.

    Returns:
        Tuple of (dateTime, radiation, maxSolarRad, rainRate, outTemp,
        outHumidity), or None when no record falls within the ±30-minute
        window.

    SQL is fully parameterized — no string interpolation.
    """
    start = target_ts - _ARCHIVE_MATCH_WINDOW
    end = target_ts + _ARCHIVE_MATCH_WINDOW

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT dateTime, radiation, maxSolarRad, rainRate, "
                    "outTemp, outHumidity "
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
