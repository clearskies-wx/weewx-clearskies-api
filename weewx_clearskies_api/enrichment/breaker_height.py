"""Breaker height conversion: SWAN Hsig → breaking face height (T2.6).

Converts significant wave height (Hsig) from SWAN output points to the
trough-to-crest face height that surfers observe at the break.

Two formulas are supported, selected per surf spot via ``breaker_formula``
in the marine location config:

  komar_gaughan (default)
      Komar & Gaughan (1973), "Airy Wave Theory and Breaker Height Prediction",
      Proceedings 13th Coastal Engineering Conference, ASCE, pp. 405–418.
      Formula: Hb = 0.39 × g^(1/5) × (Tp × Hsig²)^(2/5)
      Valid for all periods and all coastline types. Period-dependent: a 4 ft
      Hsig at 16s produces a taller breaking face than 4 ft Hsig at 8s.

  caldwell (opt-in)
      Caldwell & Aucan (2007), "An Empirical Method for Estimating Surf Heights
      from Deepwater Significant Wave Heights and Peak Periods in Coastal Zones
      with Narrow Shelves, Steep Bottom Slopes, and High Refraction",
      Journal of Coastal Research, Vol. 23, No. 5, pp. 1190–1196.
      Empirical H1/10 predictor calibrated to Hawaiian (Oahu north shore)
      geomorphology. Only valid for Tp ≥ 10s — shorter periods automatically
      fall back to Komar-Gaughan (the paper states "unrealistic for wave
      periods less than ~10 seconds").

Pipeline position (T2.6 / T4.3, amended T4A.7):

  SWAN handoff SPECOUT (raw Hsig, at the requested handoff coordinate)
    → 1D cross-shore model (SwellTrack, services/surf_1d_pipeline.py) → break-point Hs
    → hsig_to_face_height(break_point_hs, Tp, formula=…, source="break_point")
    → breakingFaceHeight
    → hawaiian_height(face_height)
    → breakingHawaiianHeight

Legacy path (no 1D model, deep-water Hs fed directly):
    → hsig_to_face_height(offshore_hsig, Tp, formula=…, source="deep_water")

T4A.7 (2026-07-25) removed the `enrichment/wave_transform.py` supplement
stage that used to run between SWAN and this module — both of its
surviving supplements were dead or redundant (see that module's
docstring). The 1D model now consumes SWAN's handoff Hsig directly; this
module converts the resulting break-point Hs to the display-convention
height surfers use. When the 1D model provides the break-point Hs, use
source="break_point" (Rayleigh H1/10 only). For direct offshore Hs inputs,
use source="deep_water" (full K-G or Caldwell formula). The ad-hoc linear
depth correction that previously existed in this module has been removed
(T4.3 — no physical basis; superseded by the 1D model which explicitly
tracks the complete shoaling path).
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Physics constants
# ---------------------------------------------------------------------------

#: Standard gravitational acceleration, m/s².
_G: float = 9.81

#: Caldwell auto-crossover threshold (seconds).
#: Below this period, Komar-Gaughan is always used regardless of formula setting.
#: Source: Caldwell & Aucan (2007) — "unrealistic for wave periods less than ~10s".
CALDWELL_MIN_PERIOD_S: float = 10.0

#: Clamp bounds: breaking face height must be between 1× and 3× Hsig.
#: Lower: waves do not shrink during final shoaling.
#: Upper: physical limit on wave amplification (Hmax / Hs ≈ 1.6–2.0 per Rayleigh).
_CLAMP_LOWER_FACTOR: float = 1.0
_CLAMP_UPPER_FACTOR: float = 3.0

#: Caldwell (2007): H1/10 ≈ 1.27 × H1/3 (Rayleigh distribution for narrow-banded
#: sea states, from Coastal Wiki "Statistical description of wave parameters").
_RAYLEIGH_H1_10_FACTOR: float = 1.27

#: Caldwell (2007): Period-dependent amplification rate for steep volcanic coasts.
#: The Hawaiian focusing geometry concentrates additional energy at longer periods.
#: Calibrated to produce ~1.42× Hsig at Tp=16s and ~1.27× Hsig at Tp=10s.
_CALDWELL_PERIOD_AMPLIFICATION_RATE: float = 0.020

#: Hawaiian scale factor: back-of-wave ≈ ½ of trough-to-crest face height.
#: Source: Hawaiian scale convention; documented in Surfline support docs
#: and Surfertoday.com wave measurement methods.
HAWAIIAN_SCALE_FACTOR: float = 0.5


# ---------------------------------------------------------------------------
# Internal formula implementations
# ---------------------------------------------------------------------------


def _komar_gaughan_full(hsig_m: float, period_s: float) -> float:
    """Komar-Gaughan (1973) deepwater-to-breaking height.

    Hb = 0.39 × g^(1/5) × (Tp × Hsig²)^(2/5)

    Predicts the full deepwater-to-breaking transformation. Input must be a
    true deep-water or far-offshore Hs value. If the input Hs has already
    been partially or fully shoaled (e.g., by a 1D cross-shore model at
    the break point), use source="break_point" in hsig_to_face_height()
    instead — applying this formula on shoaled Hs double-counts shoaling.

    Source: Komar & Gaughan (1973) ASCE Coastal Engineering Conference.
    Empirically fitted to three laboratory datasets and one field dataset.
    """
    return 0.39 * (_G ** 0.2) * ((period_s * hsig_m ** 2) ** 0.4)


def _caldwell_h1_10(hsig_m: float, period_s: float) -> float:
    """Caldwell & Aucan (2007) H1/10 estimate for steep volcanic coasts.

    Empirical method based on 32 years of Oahu north shore surf observations
    correlated against Waimea Canyon buoy (NOAA NDBC 51001 / CDIP 106) data.
    Estimates the average height of the highest 10% of breaking waves (set
    waves), accounting for the refraction focusing characteristic of steep
    narrow-shelf volcanic island topography.

    Only valid for Tp ≥ 10s. The paper explicitly states results are
    "unrealistic for wave periods less than ~10 seconds"; callers must
    route to Komar-Gaughan below that threshold (hsig_to_face_height handles
    this automatically via CALDWELL_MIN_PERIOD_S).

    Implementation:
      H1/10 = 1.27 × Hs × (1 + 0.020 × (Tp − 10.0))

    The 1.27 factor is the Rayleigh distribution ratio H1/10 / H1/3 for
    narrow-banded sea states (Coastal Wiki, "Statistical description of wave
    parameters"). The period-amplification factor captures the heightened
    refraction focusing at longer swell periods on steep volcanic coasts.

    Source: Caldwell, P.C. and Aucan, J. (2007), Journal of Coastal Research,
    Vol. 23, No. 5, pp. 1190–1196.
    """
    period_factor = 1.0 + _CALDWELL_PERIOD_AMPLIFICATION_RATE * (period_s - CALDWELL_MIN_PERIOD_S)
    return _RAYLEIGH_H1_10_FACTOR * hsig_m * period_factor


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def hsig_to_face_height(
    hsig_m: float,
    period_s: float,
    depth_m: float | None = None,
    formula: str = "komar_gaughan",
    source: str = "deep_water",
) -> float:
    """Convert Hsig to breaking wave face height (trough-to-crest).

    Two operational modes are selected via the ``source`` parameter:

    **source="deep_water"** (default — legacy path)
        Input Hs is a deep-water or far-offshore significant wave height.
        The full Komar-Gaughan (1973) or Caldwell & Aucan (2007) formula
        converts deepwater Hs to breaking face height, accounting for the
        complete shoaling-to-breaking path. Use when feeding offshore Hs
        directly, without a 1D cross-shore model.

    **source="break_point"** (1D model path)
        Input Hs is the wave height at the actual break point, already
        fully shoaled by the 1D cross-shore model. Applying K-G here would
        double-count the shoaling the 1D model already computed. Instead,
        only the Rayleigh H1/10 statistical conversion is applied:
        face_height = 1.27 × Hs.

        The 1.27 factor (H1/10 / H1/3) converts the statistical significant
        wave height Hs (= H1/3) to the average height of the highest 10%
        of breaking waves — the set waves surfers observe at the break.
        Source: Rayleigh distribution for narrow-banded sea states
        (Coastal Wiki, "Statistical description of wave parameters").

    The earlier ad-hoc linear depth correction (lerp between full K-G and
    no-amplification based on output-point depth) has been removed. That
    correction had no physical basis and was superseded by the 1D model,
    which explicitly tracks the complete shoaling path. When the 1D model
    provides the break-point Hs, use ``source="break_point"``; when feeding
    deep-water Hs directly, use ``source="deep_water"`` (full K-G, no
    depth correction). There is no intermediate case.

    Args:
        hsig_m: Significant wave height in meters. Must be ≥ 0.
            For ``source="deep_water"``: offshore Hs (post-supplement).
            For ``source="break_point"``: Hs at the break from the 1D model.
        period_s: Peak wave period in seconds. Must be > 0. Used only for
            the ``source="deep_water"`` path (K-G and Caldwell are both
            period-dependent).
        depth_m: Water depth in meters. Not used in either current path;
            retained in the signature for forward compatibility.
        formula: Breaker formula. One of:
            ``"komar_gaughan"`` (default) — Komar & Gaughan (1973), general
            purpose, all periods and coastlines.
            ``"caldwell"`` — Caldwell & Aucan (2007), empirical H1/10
            predictor for steep volcanic island coasts. Auto-falls to
            ``"komar_gaughan"`` when period_s < CALDWELL_MIN_PERIOD_S (10s).
            Ignored when ``source="break_point"`` — only the H1/10 factor
            applies regardless of formula.
        source: Input Hs provenance. One of:
            ``"deep_water"`` (default) — offshore Hs; apply full K-G or
            Caldwell formula.
            ``"break_point"`` — break-point Hs from the 1D model; apply
            the Rayleigh H1/10 factor only (1.27 × Hs). No additional
            shoaling amplification.

    Returns:
        Breaking face height in meters (trough-to-crest). Always in the range
        [hsig_m, 3 × hsig_m]. Returns 0.0 when hsig_m ≤ 0 or period_s ≤ 0.

    Raises:
        ValueError: ``formula`` or ``source`` is not a recognised value.
    """
    if formula not in ("komar_gaughan", "caldwell"):
        raise ValueError(
            f"hsig_to_face_height: unknown formula {formula!r}. "
            "Expected 'komar_gaughan' or 'caldwell'."
        )
    if source not in ("deep_water", "break_point"):
        raise ValueError(
            f"hsig_to_face_height: unknown source {source!r}. "
            "Expected 'deep_water' or 'break_point'."
        )

    if hsig_m <= 0.0 or period_s <= 0.0:
        return 0.0

    # ---------------------------------------------------------------------------
    # Select and compute the raw breaking height
    # ---------------------------------------------------------------------------

    if source == "break_point":
        # The 1D model has already fully shoaled the wave to the break point.
        # Applying K-G here would double-count shoaling (K-G is a deepwater-to-
        # breaking formula — its amplification factor IS the shoaling path).
        # Apply only the Rayleigh H1/10 statistical conversion: the average of
        # the highest 10% of breaking waves (set waves) observed at the break.
        face_height = hsig_m * _RAYLEIGH_H1_10_FACTOR
    else:
        # source == "deep_water": full deepwater-to-breaking formula.
        # No depth correction is applied — if the input Hs has been partially
        # shoaled by intermediate processing, use source="break_point" instead.
        use_komar_gaughan = formula == "komar_gaughan" or period_s < CALDWELL_MIN_PERIOD_S

        if use_komar_gaughan:
            # Full deepwater-to-breaking amplification from Komar-Gaughan.
            face_height = _komar_gaughan_full(hsig_m, period_s)
        else:
            # Caldwell (2007): valid for Tp ≥ 10s only (crossover handled above).
            # Empirical surface fit to the full buoy-to-shore path — no depth
            # correction is meaningful for an empirical regression.
            face_height = _caldwell_h1_10(hsig_m, period_s)

    # ---------------------------------------------------------------------------
    # Clamp: face height must be ≥ Hsig (waves do not shrink during final
    # shoaling to the break) and ≤ 3 × Hsig (physical upper limit from Rayleigh
    # distribution: Hmax / Hs ≈ 1.6–2.0; 3× provides conservative headroom).
    # ---------------------------------------------------------------------------
    face_height = max(face_height, hsig_m * _CLAMP_LOWER_FACTOR)
    face_height = min(face_height, hsig_m * _CLAMP_UPPER_FACTOR)

    return face_height


def hawaiian_height(face_height_m: float) -> float:
    """Convert breaking face height to Hawaiian (back-of-wave) scale.

    The Hawaiian / traditional measurement convention measures the back of
    the wave rather than the face, resulting in approximately half the
    trough-to-crest face height.

    Args:
        face_height_m: Breaking face height in meters (trough-to-crest),
            as returned by ``hsig_to_face_height()``.

    Returns:
        Hawaiian scale height in meters. Always exactly face_height_m × 0.5.
        Returns 0.0 for non-positive inputs.

    Source: Hawaiian scale convention; Surfline support documentation on
    "Surf Height Unit Preference — Face Height vs. Traditional."
    """
    if face_height_m <= 0.0:
        return 0.0
    return face_height_m * HAWAIIAN_SCALE_FACTOR
