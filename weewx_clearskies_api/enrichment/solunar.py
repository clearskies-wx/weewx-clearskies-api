"""Solunar major/minor feeding-period computation (API-MANUAL.md §17 "Solunar computation").

Standalone function — not registered via ``register_enrichment``. Called
directly by the solunar and fishing endpoints, the same pattern as
``enrichment/surf_scorer.py``'s ``score_surf()``.

Computed entirely via Skyfield — no external API call. Reuses the cached
timescale/ephemeris and station-local day-window helpers from
``services/almanac.py`` rather than duplicating Skyfield setup:
``get_ts_eph()``, ``_station_local_window()``, ``_phase_name_from_angle()``,
``_to_utc_z()``.

**Moon transit / underfoot (API-MANUAL.md §17 "Solunar computation"):**
``almanac.meridian_transits()`` returns a step function queried via
``find_discrete()``. Empirically verified against Skyfield 1.54 (not just
inferred from the docstring): event value ``1`` = upper transit (moon at its
highest point), event value ``0`` = anti-meridian transit ("underfoot", moon
at its lowest point / opposite side of the Earth). The two alternate roughly
every 12h25m (half of the ~24h50m mean lunar day).

A single station-local calendar-day window frequently contains the day's
upper transit but *not* its underfoot event, because underfoot commonly
falls just before or after a midnight boundary and lands in the adjacent
calendar day. To handle this, the search window is widened by one day on
each side, and among all transit (``1``) and underfoot (``0``) events found,
the ones closest to the day's local-noon midpoint are selected.

**moonPhase wire format:** ``SolunarTimes.moonPhase`` uses underscores
(``"waxing_crescent"``) per the model docstring and API-MANUAL.md §16/§17 —
not the hyphenated form (``"waxing-crescent"``) that
``_phase_name_from_angle()`` returns for the pre-existing (and separately
contracted) almanac ``phaseName`` field. This module converts hyphens to
underscores before assigning ``moonPhase``.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING

from skyfield import almanac
from skyfield.api import wgs84
from skyfield.framelib import ecliptic_frame

from weewx_clearskies_api.models.responses import SolunarTimes
from weewx_clearskies_api.services.almanac import (
    _phase_name_from_angle,
    _station_local_window,
    _to_utc_z,
    get_ts_eph,
)

if TYPE_CHECKING:
    from skyfield.timelib import Time

# ---------------------------------------------------------------------------
# Moon-phase illumination thresholds (API-MANUAL.md §17 "Moon phase intensity")
# ---------------------------------------------------------------------------

_NEW_FULL_ILLUM_LOW = 0.05
_NEW_FULL_ILLUM_HIGH = 0.95
_QUARTER_ILLUM_LOW = 0.45
_QUARTER_ILLUM_HIGH = 0.55

# ---------------------------------------------------------------------------
# Period half-widths, hours (API-MANUAL.md §17 "Period duration modulation")
# ---------------------------------------------------------------------------

_MAJOR_HALF_WIDTH_STANDARD_H = 1.5
_MAJOR_HALF_WIDTH_NEWFULL_H = 2.0
_MINOR_HALF_WIDTH_STANDARD_H = 1.0
_MINOR_HALF_WIDTH_NEWFULL_H = 1.5

# ---------------------------------------------------------------------------
# Intensity mapping (API-MANUAL.md §17 "Moon phase intensity")
# ---------------------------------------------------------------------------

_INTENSITY_NEW_FULL = 1.0
_INTENSITY_QUARTER = 0.7
_INTENSITY_DEFAULT = 0.5

# Search margin on each side of the station-local day window when hunting for
# moon transit/underfoot events; needed because underfoot commonly falls just
# outside the calendar-day boundary (see module docstring).
_TRANSIT_SEARCH_MARGIN_DAYS = 1

# Fallback offset if Skyfield defensively returns no underfoot event within
# the widened search window (not expected in practice — mean lunar day is
# ~24h50m, so a 3-day-wide window always contains both event types).
_UNDERFOOT_FALLBACK_OFFSET = timedelta(hours=12, minutes=25)


def _is_new_or_full(illumination: float) -> bool:
    return illumination < _NEW_FULL_ILLUM_LOW or illumination > _NEW_FULL_ILLUM_HIGH


def _period_window(center_t: Time, half_width_hours: float) -> dict[str, str]:
    """Build a {start, end} UTC ISO-8601 dict centered on a Skyfield Time."""
    start = center_t - timedelta(hours=half_width_hours)
    end = center_t + timedelta(hours=half_width_hours)
    return {"start": _to_utc_z(start), "end": _to_utc_z(end)}


def _nearest_event(
    times: list[Time], events: list[int], target_value: int, center_tt: float
) -> Time | None:
    """Return the Time among *times* whose event == target_value and which is
    closest (in TT days) to *center_tt*, or None if no match exists.
    """
    best: Time | None = None
    best_diff: float | None = None
    for t, e in zip(times, events, strict=False):
        if int(e) != target_value:
            continue
        diff = abs(t.tt - center_tt)  # type: ignore[attr-defined]
        if best_diff is None or diff < best_diff:
            best = t
            best_diff = diff
    return best


def compute_solunar(
    d: date,
    lat: float,
    lon: float,
    station_tz: str = "UTC",
) -> SolunarTimes:
    """Compute solunar major/minor feeding periods for one date at one location.

    Args:
        d: Station-local calendar date.
        lat: Station latitude (decimal degrees, signed).
        lon: Station longitude (decimal degrees, signed).
        station_tz: IANA timezone identifier; the day window is station-local
            midnight-to-midnight (same pattern as services/almanac.py).

    Returns:
        SolunarTimes for the given date.
    """
    ts, eph = get_ts_eph()
    location = wgs84.latlon(lat, lon)  # type: ignore[call-arg]

    t0, t1 = _station_local_window(ts, d, station_tz)
    day_center_tt = (t0.tt + t1.tt) / 2.0  # type: ignore[attr-defined]

    moon = eph["moon"]  # type: ignore[index]
    sun = eph["sun"]  # type: ignore[index]
    earth = eph["earth"]  # type: ignore[index]

    # --- Moon transit / underfoot (widened search window; see module docstring) ---
    t_search0 = t0 - timedelta(days=_TRANSIT_SEARCH_MARGIN_DAYS)
    t_search1 = t1 + timedelta(days=_TRANSIT_SEARCH_MARGIN_DAYS)
    f_transit = almanac.meridian_transits(eph, moon, location)  # type: ignore[arg-type]
    t_transits, e_transits = almanac.find_discrete(t_search0, t_search1, f_transit)  # type: ignore[arg-type]

    transit_t = _nearest_event(t_transits, e_transits, 1, day_center_tt)
    underfoot_t = _nearest_event(t_transits, e_transits, 0, day_center_tt)

    # Both are expected to be found in practice — see module docstring.
    # Defensive fallback only; not expected to trigger with a real ephemeris.
    if transit_t is None:
        transit_t = ts.utc(d.year, d.month, d.day, 12, 0, 0)  # type: ignore[call-arg]
    if underfoot_t is None:
        underfoot_t = transit_t - _UNDERFOOT_FALLBACK_OFFSET

    moon_transit_str = _to_utc_z(transit_t)
    moon_underfoot_str = _to_utc_z(underfoot_t)

    # --- Moonrise / moonset (station-local calendar day; may be absent at
    # high latitude) ---
    f_rise = almanac.risings_and_settings(eph, moon, location)  # type: ignore[arg-type]
    t_rise, e_rise = almanac.find_discrete(t0, t1, f_rise)  # type: ignore[arg-type]

    moonrise_t: Time | None = None
    moonset_t: Time | None = None
    for t, e in zip(t_rise, e_rise, strict=False):
        if e == 1 and moonrise_t is None:
            moonrise_t = t
        elif e == 0 and moonset_t is None:
            moonset_t = t

    moonrise_str = _to_utc_z(moonrise_t) if moonrise_t is not None else None
    moonset_str = _to_utc_z(moonset_t) if moonset_t is not None else None

    # --- Sunrise / sunset (C-47, 2026-07-25): for THIS function's own
    # lat/lon (a fishing spot's coordinates), not the station's — the only
    # existing sun-event computation, services/almanac.py's
    # _compute_sun_for_date(), is station-scoped by every current caller
    # and also computes civil twilight/transit/next-equinox-solstice, none
    # of which solunar needs; duplicating that overhead across up to 30
    # days per request (SolunarQueryParams.days) is worse than applying
    # the exact same lightweight risings_and_settings/find_discrete
    # pattern already used for moonrise/moonset two blocks up, to the sun
    # instead of the moon — same technique, not a second implementation of
    # it. Same station-local day window (t0, t1) as moonrise/moonset and
    # the same null-on-polar-day/night handling (Skyfield returns an empty
    # events array, not an exception, when the sun does not rise or set —
    # matches services/almanac.py's own F3 fix note).
    f_sun_rise = almanac.risings_and_settings(eph, sun, location)  # type: ignore[arg-type]
    t_sun_rise, e_sun_rise = almanac.find_discrete(t0, t1, f_sun_rise)  # type: ignore[arg-type]

    sunrise_t: Time | None = None
    sunset_t: Time | None = None
    for t, e in zip(t_sun_rise, e_sun_rise, strict=False):
        if e == 1 and sunrise_t is None:
            sunrise_t = t
        elif e == 0 and sunset_t is None:
            sunset_t = t

    sunrise_str = _to_utc_z(sunrise_t) if sunrise_t is not None else None
    sunset_str = _to_utc_z(sunset_t) if sunset_t is not None else None

    # --- Moon phase + illumination (ecliptic frame at transit time, same
    # formula as services/almanac.py._compute_moon_for_date) ---
    sun_ecl = earth.at(transit_t).observe(sun).apparent().frame_latlon(ecliptic_frame)  # type: ignore[attr-defined]
    moon_ecl = earth.at(transit_t).observe(moon).apparent().frame_latlon(ecliptic_frame)  # type: ignore[attr-defined]
    sun_lon = float(sun_ecl[1].degrees)  # type: ignore[index]
    moon_lon = float(moon_ecl[1].degrees)  # type: ignore[index]
    phase_angle = (moon_lon - sun_lon) % 360.0

    # SolunarTimes.moonPhase is underscore-separated per the wire contract
    # (API-MANUAL.md §16/§17) — see module docstring for why this diverges
    # from _phase_name_from_angle()'s raw (hyphenated) output.
    phase_name = _phase_name_from_angle(phase_angle).replace("-", "_")

    illumination = math.cos(math.radians((phase_angle - 180.0) / 2.0)) ** 2
    illumination = round(max(0.0, min(1.0, illumination)), 3)

    new_or_full = _is_new_or_full(illumination)

    # --- Major periods: centered on transit + underfoot ---
    major_half_width = (
        _MAJOR_HALF_WIDTH_NEWFULL_H if new_or_full else _MAJOR_HALF_WIDTH_STANDARD_H
    )
    major_periods = [
        _period_window(transit_t, major_half_width),
        _period_window(underfoot_t, major_half_width),
    ]

    # --- Minor periods: centered on moonrise + moonset (omitted if absent) ---
    minor_half_width = (
        _MINOR_HALF_WIDTH_NEWFULL_H if new_or_full else _MINOR_HALF_WIDTH_STANDARD_H
    )
    minor_periods: list[dict[str, str]] = []
    if moonrise_t is not None:
        minor_periods.append(_period_window(moonrise_t, minor_half_width))
    if moonset_t is not None:
        minor_periods.append(_period_window(moonset_t, minor_half_width))

    # --- Intensity (API-MANUAL.md §17 "Moon phase intensity") ---
    if new_or_full:
        intensity = _INTENSITY_NEW_FULL
    elif _QUARTER_ILLUM_LOW <= illumination <= _QUARTER_ILLUM_HIGH:
        intensity = _INTENSITY_QUARTER
    else:
        intensity = _INTENSITY_DEFAULT

    return SolunarTimes(
        date=d.isoformat(),
        moonPhase=phase_name,
        moonIllumination=illumination,
        moonrise=moonrise_str,
        moonset=moonset_str,
        moonTransit=moon_transit_str,
        moonUnderfoot=moon_underfoot_str,
        sunrise=sunrise_str,
        sunset=sunset_str,
        majorPeriods=major_periods,
        minorPeriods=minor_periods,
        intensity=intensity,
    )
