"""CAMS McClear clear-sky GHI fetcher for bootstrap calibration (ADR-072 T2.1).

Fetches historical clear-sky GHI from CAMS McClear via pvlib.iotools.get_cams().
Used exclusively during the one-time bootstrap run — NOT in the real-time path.

Auth:     SoDa email registration (free, https://www.soda-pro.com/).
          Passed via env var WEEWX_CLEARSKIES_SODA_EMAIL.
Rate:     100 requests/day (free tier).  We chunk by calendar year,
          so at most 3 requests cover the 3-year bootstrap window.
Column:   ghi_clear — atmosphere-adjusted clear-sky GHI at ground level.
          NOT ghi_extra (extraterrestrial, no atmosphere).
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)

# McClear is fetched at 15-minute intervals for accuracy at sunrise/sunset.
_TIME_STEP = "15min"

# Maximum years to request in a single chunk.  We chunk by calendar year
# to stay well within SoDa's 100 req/day free-tier limit.
_CHUNK_YEARS = 1


def fetch_mcclear_clearsky_ghi(
    latitude: float,
    longitude: float,
    altitude_m: float,
    start_date: str,
    end_date: str,
    soda_email: str,
) -> dict[int, float]:
    """Fetch McClear clear-sky GHI from CAMS via pvlib.

    Returns dict mapping Unix timestamp (int, UTC) to clear-sky GHI (W/m²).
    Fetches at 15-minute resolution for accuracy at sunrise/sunset.
    Chunks by year to respect SoDa's 100 req/day rate limit.

    Args:
        latitude:    Station latitude in decimal degrees.
        longitude:   Station longitude in decimal degrees.
        altitude_m:  Station altitude in metres above sea level.
        start_date:  ISO date string "YYYY-MM-DD" (inclusive).
        end_date:    ISO date string "YYYY-MM-DD" (inclusive).
        soda_email:  Email address registered at soda-pro.com.

    Returns:
        Dict mapping integer Unix timestamps (UTC, seconds) to GHI (W/m²).

    Raises:
        RuntimeError: if any CAMS fetch fails (caller handles gracefully).
    """
    try:
        from pvlib.iotools import get_cams  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "pvlib is required for McClear bootstrap (pip install pvlib>=0.15.0)"
        ) from exc

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    # Build per-year chunks.
    chunks: list[tuple[date, date]] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end_year_last = date(chunk_start.year, 12, 31)
        chunk_end = min(chunk_end_year_last, end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = date(chunk_start.year + 1, 1, 1)

    logger.info(
        "McClear: fetching %d year-chunk(s) for (%s, %s) altitude=%.0f m [%s → %s]",
        len(chunks),
        latitude,
        longitude,
        altitude_m,
        start_date,
        end_date,
    )

    all_results: dict[int, float] = {}

    for chunk_start_d, chunk_end_d in chunks:
        ts_start = pd.Timestamp(chunk_start_d.isoformat(), tz="UTC")
        ts_end = pd.Timestamp(chunk_end_d.isoformat(), tz="UTC")

        logger.info(
            "McClear: fetching %s → %s ...",
            chunk_start_d.isoformat(),
            chunk_end_d.isoformat(),
        )

        try:
            data, _metadata = get_cams(
                latitude=latitude,
                longitude=longitude,
                start=ts_start,
                end=ts_end,
                email=soda_email,
                identifier="mcclear",
                altitude=altitude_m,
                time_step=_TIME_STEP,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"CAMS McClear fetch failed for {chunk_start_d} → {chunk_end_d}: {exc}"
            ) from exc

        if data is None or data.empty:
            logger.warning(
                "McClear: empty response for %s → %s",
                chunk_start_d.isoformat(),
                chunk_end_d.isoformat(),
            )
            continue

        if "ghi_clear" not in data.columns:
            # pvlib may return column named 'Clear sky GHI' or similar in older versions.
            # Log available columns and raise so the caller can diagnose.
            raise RuntimeError(
                f"McClear: 'ghi_clear' column not found in response. "
                f"Available columns: {list(data.columns)}"
            )

        # Convert DatetimeIndex → integer Unix timestamps (seconds since epoch).
        unix_index = data.index.astype("int64") // 10**9
        ghi_series = data["ghi_clear"]

        chunk_dict = dict(zip(unix_index, ghi_series.astype(float)))
        all_results.update(chunk_dict)

        logger.info(
            "McClear: %s → %s — %d data points",
            chunk_start_d.isoformat(),
            chunk_end_d.isoformat(),
            len(chunk_dict),
        )

    logger.info(
        "McClear: total %d data points fetched across %d chunk(s)",
        len(all_results),
        len(chunks),
    )

    return all_results
