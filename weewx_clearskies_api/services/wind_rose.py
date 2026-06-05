"""Wind rose data binning service.

Queries archive windSpeed / windDir columns and bins records into a 16×7
direction × Beaufort-speed matrix.  All arithmetic is in m/s internally;
the archive's usUnits field determines the conversion factor applied once
at query time (homogeneous archive — first row's usUnits applies to all).

SQL note: all user-supplied values (from_epoch, to_epoch) are bound via
SQLAlchemy text() named parameters — never string-interpolated into SQL.

Beaufort scale (0–6, simplified for wind rose display, matching Belchertown
beauford0–beauford6):
  0 < 0.5 m/s    Calm
  1 0.5–1.5 m/s  Light Air
  2 1.6–3.3 m/s  Light Breeze
  3 3.4–5.4 m/s  Gentle Breeze
  4 5.5–7.9 m/s  Moderate Breeze
  5 8.0–10.7 m/s Fresh Breeze
  6 ≥ 10.8 m/s   Strong Breeze+

Direction bins: 16 compass points, 22.5° each, centred on compass point.
  bin_index = int((windDir + 11.25) % 360 / 22.5)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIRECTIONS: list[str] = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
]

# (label, lower_bound_inclusive, upper_bound_exclusive)
# upper for bin 6 is +inf — handled separately.
_BEAUFORT_THRESHOLDS: list[tuple[str, float, float]] = [
    ("Calm",             0.0,  0.5),
    ("Light Air",        0.5,  1.6),
    ("Light Breeze",     1.6,  3.4),
    ("Gentle Breeze",    3.4,  5.5),
    ("Moderate Breeze",  5.5,  8.0),
    ("Fresh Breeze",     8.0, 10.8),
    ("Strong Breeze+",  10.8, float("inf")),
]

BEAUFORT_LABELS: list[str] = [t[0] for t in _BEAUFORT_THRESHOLDS]

# usUnits conversion factors to m/s
_US_TO_MS = 0.44704   # mph → m/s
_METRIC_TO_MS = 0.27778  # km/h → m/s
_METRICWX_TO_MS = 1.0   # m/s → m/s (no conversion)

# weewx usUnits identifiers
_US_UNITS = 1
_METRIC_UNITS = 16
_METRICWX_UNITS = 17


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class WindRoseResult:
    """Output of compute_wind_rose()."""

    #: 16×7 matrix of percentages (direction × Beaufort).
    bins: list[list[float]] = field(default_factory=lambda: _empty_bins())
    total_records: int = 0
    calm_percentage: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_bins() -> list[list[float]]:
    """Return a 16×7 zero-filled matrix."""
    return [[0.0] * 7 for _ in range(16)]


def _conversion_factor(us_units: int) -> float:
    """Return the m/s conversion factor for the given usUnits identifier.

    Args:
        us_units: Value from archive.usUnits column.

    Returns:
        Multiplication factor to convert archive windSpeed to m/s.
    """
    if us_units == _US_UNITS:
        return _US_TO_MS
    if us_units == _METRIC_UNITS:
        return _METRIC_TO_MS
    if us_units == _METRICWX_UNITS:
        return _METRICWX_TO_MS
    # Fallback: assume m/s (log a warning so operators can diagnose).
    logger.warning(
        "Unknown usUnits value %r; treating windSpeed as m/s (no conversion). "
        "Verify [database] unit_system in api.conf.",
        us_units,
    )
    return _METRICWX_TO_MS


def _dir_bin(wind_dir: float) -> int:
    """Map a wind direction (0–360°) to a 0-based 16-bin index.

    Uses the formula: int((windDir + 11.25) % 360 / 22.5)
    N is index 0, NNE is index 1, … NNW is index 15.

    Args:
        wind_dir: Wind direction in degrees [0, 360).

    Returns:
        Integer 0–15.
    """
    return int((wind_dir + 11.25) % 360 / 22.5)


def _beaufort_bin(speed_ms: float) -> int:
    """Map a wind speed in m/s to Beaufort bin index 0–6.

    Args:
        speed_ms: Wind speed in m/s (non-negative).

    Returns:
        Integer 0–6.
    """
    for i, (_, _lower, upper) in enumerate(_BEAUFORT_THRESHOLDS):
        if speed_ms < upper:
            return i
    return 6  # unreachable but satisfies type checker


# ---------------------------------------------------------------------------
# Public service function
# ---------------------------------------------------------------------------


def compute_wind_rose(
    db: Session,
    from_epoch: float,
    to_epoch: float,
) -> WindRoseResult:
    """Query archive and compute a 16×7 wind rose binning matrix.

    Args:
        db: SQLAlchemy session (read-only).
        from_epoch: Start of query window, Unix epoch seconds (inclusive).
        to_epoch: End of query window, Unix epoch seconds (exclusive).

    Returns:
        WindRoseResult with bins as percentages, totalRecords, calmPercentage.

    Raises:
        KeyError: windSpeed or windDir columns are not present in the archive.
            Caller should convert to HTTP 404 with RFC 9457 problem+json.
    """
    sql = text(
        "SELECT windSpeed, windDir, usUnits FROM archive "
        "WHERE dateTime >= :from_ts AND dateTime < :to_ts "
        "AND windSpeed IS NOT NULL AND windDir IS NOT NULL"
    )

    try:
        rows = db.execute(sql, {"from_ts": from_epoch, "to_ts": to_epoch}).fetchall()
    except Exception as exc:
        # Surface missing-column errors as KeyError so the endpoint can return 404.
        # MariaDB raises OperationalError, SQLite raises OperationalError too.
        # Both include "Unknown column" or "no such column" in the message.
        exc_str = str(exc).lower()
        if "unknown column" in exc_str or "no such column" in exc_str:
            raise KeyError(
                "Wind data columns not available in archive"
            ) from exc
        raise

    if not rows:
        return WindRoseResult()

    # Determine conversion factor from the first row's usUnits (archive is
    # homogeneous — all rows use the same unit system per lead call #6).
    first_us_units = int(rows[0][2])
    factor = _conversion_factor(first_us_units)

    # Accumulate counts in a 16×7 matrix.
    counts: list[list[int]] = [[0] * 7 for _ in range(16)]
    calm_count = 0
    total = len(rows)

    for row in rows:
        raw_speed = float(row[0])
        wind_dir = float(row[1])

        speed_ms = raw_speed * factor

        dir_idx = _dir_bin(wind_dir)
        bft_idx = _beaufort_bin(speed_ms)

        counts[dir_idx][bft_idx] += 1
        if bft_idx == 0:
            calm_count += 1

    # Convert counts to percentages.
    bins: list[list[float]] = [
        [round(counts[d][b] / total * 100, 2) for b in range(7)]
        for d in range(16)
    ]

    calm_pct = round(calm_count / total * 100, 2)

    return WindRoseResult(
        bins=bins,
        total_records=total,
        calm_percentage=calm_pct,
    )
