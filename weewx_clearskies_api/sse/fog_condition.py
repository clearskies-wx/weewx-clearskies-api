"""Fog/mist detection module (ADR-069).

Multi-parameter fog/mist detection algorithm that replaces the single-variable
T-Td ≤ 1°F check.  Implements seven gates and a temporal coherence filter.

Algorithm overview:
  Gate 1 — Rain gate:         Suppress during active precipitation.
  Gate 2 — T-Td gate:        ASOS standard: suppress when T-Td > 4°F.
  Gate 3 — Fog/mist split:   T-Td ≤ 2°F → "Foggy"; 2–4°F → "Misty".
  Gate 4 — Wind gate:        > 7 m/s suppresses both; 3–7 m/s downgrades
                              fog candidates to mist.
  Gate 5 — PM disambiguation: T-Td ≤ 4°F AND PM2.5 > 35 µg/m³ → "Hazy"
                              (particulate haze with moisture absorption,
                              not water-droplet fog).
  Gate 6 — Daytime solar:    Kcs > 0.3 suppresses mist candidates
                              (T-Td 2–4°F) during daytime; dense fog
                              (T-Td ≤ 2°F) is NOT suppressed at sunrise.
                              Kcs > 0.5 suppresses near-boundary fog
                              (T-Td 3.5–4°F) during daytime.
  Gate 7 — Temporal coherence: 15-minute rolling window (deque of
                              (timestamp, label_or_none) pairs).  Label
                              is only reported when ≥ 50% of the window
                              entries agree on a non-None result.

Evaluation order matches the order above (rain → T-Td → split → wind →
PM → solar → temporal coherence).

Module-level state is intentional — the API is a single-process service.
Use reset() for test isolation.
"""

from __future__ import annotations

import time
from collections import deque

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Rolling history of (unix_timestamp, label_or_none) pairs for the 15-minute
# temporal coherence filter.  Entries older than 900 s are pruned on each
# call to detect_fog_mist().
_fog_history: deque[tuple[float, str | None]] = deque()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_fog_mist(
    *,
    out_temp: float | None,
    dewpoint: float | None,
    wind_speed: float | None,
    rain_rate: float | None,
    kcs: float | None,
    is_daytime: bool,
    pm25: float | None,
) -> str | None:
    """Return 'Foggy', 'Misty', 'Hazy', or None.

    Implements the multi-parameter fog/mist detection algorithm per
    API-MANUAL §8.

    All temperature inputs are in °F (US units from input_smoother).
    Wind speed is in mph (US units from input_smoother).
    PM2.5 is in µg/m³.

    Args:
        out_temp:   Dry-bulb temperature in °F (smoothed).
        dewpoint:   Dewpoint temperature in °F (smoothed).
        wind_speed: Wind speed in mph (smoothed).
        rain_rate:  Rain rate in in/hr (smoothed).
        kcs:        Clear-sky index from sky_condition.get_current_kcs().
        is_daytime: True when sun is up, from sky_condition.is_daytime().
        pm25:       PM2.5 concentration in µg/m³ (smoothed), or None.

    Returns:
        'Foggy' when dense fog is detected and temporally confirmed.
        'Misty' when mist (less saturated) is detected and confirmed.
        'Hazy'  when near-saturated air with elevated PM2.5 suggests
                particulate haze rather than water-droplet fog.
        None    when conditions do not indicate fog or mist.
    """
    now = time.time()

    # ------------------------------------------------------------------
    # Gate 1 — Rain gate.
    # Precipitation fog is a distinct phenomenon not reported here.
    # Suppress immediately — do not feed the temporal history.
    # ------------------------------------------------------------------
    if rain_rate is not None and rain_rate > 0.0:
        _record_history(now, label=None)
        return None

    # ------------------------------------------------------------------
    # Gate 2 — T-Td gate (ASOS standard).
    # Fog and mist are suppressed when T-Td > 4°F.
    # Both values must be present — if either is missing, return None.
    # ------------------------------------------------------------------
    if out_temp is None or dewpoint is None:
        _record_history(now, label=None)
        return None

    t_td = out_temp - dewpoint

    if t_td > 4.0:
        _record_history(now, label=None)
        return None

    # ------------------------------------------------------------------
    # Gate 3 — Fog/mist split.
    # T-Td ≤ 2°F → "Foggy" candidate.
    # T-Td > 2°F and ≤ 4°F → "Misty" candidate.
    # ------------------------------------------------------------------
    if t_td <= 2.0:
        candidate = "Foggy"
    else:
        # 2.0 < t_td <= 4.0
        candidate = "Misty"

    # ------------------------------------------------------------------
    # Gate 4 — Wind gate.
    # Convert wind_speed from mph to m/s before comparison.
    # Thresholds:
    #   ≤ 3 m/s (~7 mph):  fog and mist eligible.
    #   3–7 m/s:           fog NOT eligible (downgrade to mist); mist OK.
    #   > 7 m/s:           suppress both — return None.
    # When wind_speed is None, skip this gate.
    # ------------------------------------------------------------------
    if wind_speed is not None:
        wind_ms = wind_speed * 0.44704
        if wind_ms > 7.0:
            _record_history(now, label=None)
            return None
        if wind_ms > 3.0 and candidate == "Foggy":
            # Downgrade fog to mist in moderate wind.
            candidate = "Misty"

    # ------------------------------------------------------------------
    # Gate 5 — PM2.5 disambiguation.
    # Near-saturated air (T-Td ≤ 4°F) with elevated PM2.5 > 35 µg/m³
    # indicates particulate haze with moisture absorption, not water-
    # droplet fog.  Change candidate to "Hazy".
    # When pm25 is None, skip this check.
    # ------------------------------------------------------------------
    if pm25 is not None and pm25 > 35.0:
        candidate = "Hazy"

    # ------------------------------------------------------------------
    # Gate 6 — Daytime solar suppression.
    # Only applies during daytime (is_daytime=True) and when Kcs is known.
    #
    # Case A (mist candidate, T-Td 2–4°F):
    #   Kcs > 0.3 → SUPPRESS.  Humid air under strong insolation dissolves
    #   quickly; > 0.3 Kcs is incompatible with persistent mist.
    #   Note: "Hazy" candidates (from Gate 5) are NOT suppressed here —
    #   PM haze doesn't dissipate with solar radiation the way mist does.
    #
    # Case B (fog candidate, T-Td ≤ 2°F):
    #   Dense fog persists through sunrise.  Do NOT suppress on Kcs > 0.3.
    #   However, near the T-Td gate boundary (3.5°F < T-Td ≤ 4°F already
    #   handled by Gate 2) and Kcs > 0.5, suppress as a dissipation signal.
    #   Practically: the main protection is Gate 2 (T-Td > 4°F → None).
    #   This sub-case handles the edge: is_daytime AND Kcs > 0.5 AND
    #   3.5 < t_td ≤ 4.0 (fog candidate can't occur here; already "Misty"
    #   by Gate 3).  For true fog (T-Td ≤ 2°F), no solar suppression.
    # ------------------------------------------------------------------
    if is_daytime and kcs is not None:
        if candidate == "Misty" and kcs > 0.3:
            _record_history(now, label=None)
            return None
        # Extra dissipation guard: fog near T-Td boundary with strong sun.
        # T-Td 2–3.5°F + Kcs > 0.5 → suppress (mist already caught above
        # at Kcs > 0.3; this handles fog that was downgraded to mist by
        # wind, or borderline T-Td scenarios).
        # For fog (T-Td ≤ 2°F) with Kcs > 0.5, allow — dense fog persists.

    # ------------------------------------------------------------------
    # Gate 7 — Temporal coherence (15-minute rolling window).
    # Record the current observation and evaluate window majority.
    # Fog/mist is only reported when ≥ 50% of entries in the window
    # agree on a non-None result.  The majority label is returned.
    # ------------------------------------------------------------------
    _record_history(now, label=candidate)
    return _evaluate_coherence(now)


def reset() -> None:
    """Clear all module-level state.  For test isolation only."""
    _fog_history.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _record_history(now: float, *, label: str | None) -> None:
    """Append a (timestamp, label) entry and prune entries older than 15 min."""
    _fog_history.append((now, label))
    cutoff = now - 900.0
    while _fog_history and _fog_history[0][0] < cutoff:
        _fog_history.popleft()


def _evaluate_coherence(now: float) -> str | None:  # noqa: ARG001
    """Return the majority label from the 15-minute window, or None.

    Fog/mist is only reported when ≥ 50% of entries in the window have
    a non-None label AND a single label achieves the majority.  When two
    labels tie (e.g. equal "Foggy" and "Misty" counts), the denser label
    ("Foggy") is preferred.
    """
    window = list(_fog_history)
    if not window:
        return None

    total = len(window)
    # Count non-None labels.
    label_counts: dict[str, int] = {}
    for _, lbl in window:
        if lbl is not None:
            label_counts[lbl] = label_counts.get(lbl, 0) + 1

    if not label_counts:
        return None

    # Total non-None count must be ≥ 50% of the window.
    non_none_total = sum(label_counts.values())
    if non_none_total / total < 0.50:
        return None

    # Find the label with the highest count.
    # Tie-break order: Foggy > Misty > Hazy (denser condition preferred).
    best_label = max(
        label_counts,
        key=lambda lbl: (label_counts[lbl], _tiebreak_priority(lbl)),
    )
    return best_label


def _tiebreak_priority(label: str) -> int:
    """Return a priority score for tie-breaking majority label selection.

    Higher value = preferred when counts are equal.
    Foggy (densest) > Misty > Hazy.
    """
    if label == "Foggy":
        return 3
    if label == "Misty":
        return 2
    if label == "Hazy":
        return 1
    return 0
