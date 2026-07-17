"""Breaker height conversion: SWAN Hsig → breaking face height (T2.6).

Converts post-supplement significant wave height (Hsig) from SWAN output
points to the trough-to-crest face height that surfers observe at the break.

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

Pipeline position (T2.6):

  SWAN Hsig (raw)
    → wave_transform.apply_supplements() → corrected Hsig
    → hsig_to_face_height(corrected_hsig, Tp, depth, formula)
    → breakingFaceHeight
    → hawaiian_height(face_height)
    → breakingHawaiianHeight

The supplements always run BEFORE this module. No double-counting: the
supplements correct the physical Hsig at the output point; this module
converts that corrected Hsig to the display-convention height surfers use.
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

#: Depth threshold below which the SWAN output is considered "shallow" and the
#: Komar-Gaughan amplification is reduced proportionally to avoid double-counting
#: the shoaling that SWAN already computed internally.
SHALLOW_DEPTH_THRESHOLD_M: float = 15.0

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

    Predicts the full deepwater-to-breaking transformation. When the SWAN
    output point is in shallow water, this over-estimates because SWAN has
    already computed part of the shoaling — caller applies depth correction.

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
    output_depth_m: float | None = None,
    formula: str = "komar_gaughan",
) -> float:
    """Convert post-supplement Hsig to breaking wave face height (trough-to-crest).

    Args:
        hsig_m: Post-supplement significant wave height in meters. Must be
            the corrected Hsig output from wave_transform.apply_supplements(),
            not raw SWAN Hsig. Must be ≥ 0.
        period_s: Peak wave period in seconds. Must be > 0.
        output_depth_m: Water depth at the SWAN output point (meters, positive
            = water depth). When < SHALLOW_DEPTH_THRESHOLD_M (15m), the
            Komar-Gaughan amplification is proportionally reduced to avoid
            double-counting shoaling that SWAN already computed internally.
            None = assume deepwater (full Komar-Gaughan amplification applies).
        formula: Breaker formula to use. One of:
            ``"komar_gaughan"`` (default) — Komar & Gaughan (1973), general
            purpose, all periods and coastlines.
            ``"caldwell"`` — Caldwell & Aucan (2007), empirical H1/10 predictor
            for steep volcanic island coasts. Auto-falls to ``"komar_gaughan"``
            when period_s < CALDWELL_MIN_PERIOD_S (10s).

    Returns:
        Breaking face height in meters (trough-to-crest). Always in the range
        [hsig_m, 3 × hsig_m]. Returns 0.0 when hsig_m ≤ 0 or period_s ≤ 0.

    Raises:
        ValueError: ``formula`` is not a recognised value.
    """
    if formula not in ("komar_gaughan", "caldwell"):
        raise ValueError(
            f"hsig_to_face_height: unknown formula {formula!r}. "
            "Expected 'komar_gaughan' or 'caldwell'."
        )

    if hsig_m <= 0.0 or period_s <= 0.0:
        return 0.0

    # ---------------------------------------------------------------------------
    # Select and compute the raw breaking height
    # ---------------------------------------------------------------------------

    use_komar_gaughan = formula == "komar_gaughan" or period_s < CALDWELL_MIN_PERIOD_S

    if use_komar_gaughan:
        # Full deepwater-to-breaking amplification from Komar-Gaughan.
        hb_full = _komar_gaughan_full(hsig_m, period_s)

        if output_depth_m is not None and output_depth_m < SHALLOW_DEPTH_THRESHOLD_M:
            # SWAN output is in the nearshore — SWAN has already computed part of
            # the shoaling-to-breaking path. Scale down the amplification ratio
            # proportionally based on how shallow the output point is.
            #
            # At 15m depth (threshold): shallow_fraction = 0 → full K-G ratio.
            # At  0m depth (shoreline): shallow_fraction = 1 → ratio = 1.0 (no amp).
            # At 10m depth (typical):  shallow_fraction ≈ 0.33 → ~67% of K-G ratio.
            #
            # This lerp approach is adapted from the research brief §4 depth-aware
            # correction (T2.6 WAVE-BREAKING-CONVERSION-BRIEF.md §5).
            kg_ratio = hb_full / hsig_m
            shallow_fraction = max(0.0, min(1.0, 1.0 - output_depth_m / SHALLOW_DEPTH_THRESHOLD_M))
            reduced_ratio = 1.0 + (kg_ratio - 1.0) * (1.0 - shallow_fraction)
            face_height = hsig_m * reduced_ratio
        else:
            # Deepwater output point — full Komar-Gaughan amplification.
            face_height = hb_full
    else:
        # Caldwell (2007): valid for Tp ≥ 10s only (crossover handled above).
        # Depth correction not applied to Caldwell — it's an empirical surface
        # fit to the full buoy-to-shore path rather than a theoretical shoaling
        # formula. Applying a depth correction would produce unphysical results.
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
