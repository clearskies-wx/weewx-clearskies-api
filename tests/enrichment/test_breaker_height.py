"""Unit tests for enrichment/breaker_height.py.

Covers:
- Komar-Gaughan formula: formula-correct numerical expectations
- Caldwell formula: H1/10 prediction for Tp >= 10s
- Caldwell auto-crossover: Tp < 10s falls to Komar-Gaughan
- Break-point source: Rayleigh H1/10 factor only (1.27x Hs)
- Clamp bounds: output in [Hsig, 3 x Hsig]
- Hawaiian height conversion: exactly face_height x 0.5
- Period monotonicity: same Hsig, longer period -> taller face height
- Edge cases: zero/negative inputs, unknown formula/source
"""

from __future__ import annotations

import math

import pytest

from weewx_clearskies_api.enrichment.breaker_height import (
    CALDWELL_MIN_PERIOD_S,
    HAWAIIAN_SCALE_FACTOR,
    hawaiian_height,
    hsig_to_face_height,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kg_deepwater(hsig_m: float, period_s: float) -> float:
    """Reference Komar-Gaughan deepwater value (no depth correction)."""
    G = 9.81
    return 0.39 * (G ** 0.2) * ((period_s * hsig_m ** 2) ** 0.4)


# ---------------------------------------------------------------------------
# Komar-Gaughan formula: mathematical correctness
# ---------------------------------------------------------------------------


def test_komar_gaughan_formula_constants():
    """K-G formula with known inputs produces expected deepwater output.

    Reference: Komar & Gaughan (1973).
    Hb = 0.39 * g^(1/5) * (Tp * Hsig^2)^(2/5)
    g = 9.81 m/s^2
    """
    # Hsig=1.0m, Tp=10s: analytically verify
    G = 9.81
    hsig, tp = 1.0, 10.0
    expected = 0.39 * (G ** 0.2) * ((tp * hsig ** 2) ** 0.4)
    result = hsig_to_face_height(hsig, tp, depth_m=None, formula="komar_gaughan")
    assert result == pytest.approx(expected, rel=1e-9), (
        f"K-G deepwater: expected {expected:.4f}m, got {result:.4f}m"
    )


def test_komar_gaughan_deepwater_amplification_increases_with_period():
    """Deepwater K-G amplification ratio increases with period."""
    hsig = 1.0
    ratios = []
    for tp in [6, 8, 10, 12, 14, 16, 18]:
        result = hsig_to_face_height(hsig, tp, depth_m=None, formula="komar_gaughan")
        ratios.append(result / hsig)
    for i in range(len(ratios) - 1):
        assert ratios[i] < ratios[i + 1], (
            f"K-G ratio at Tp={[6,8,10,12,14,16,18][i]}s ({ratios[i]:.3f}) "
            f"should be < at Tp={[6,8,10,12,14,16,18][i+1]}s ({ratios[i+1]:.3f})"
        )


def test_komar_gaughan_deepwater_amplification_decreases_with_hsig():
    """Deepwater K-G amplification ratio decreases as Hsig increases (same period).

    From K-G: ratio = 0.39 * g^0.2 * T^0.4 * H^(-0.2)
    H^(-0.2) decreases as H increases.
    """
    tp = 14.0
    prev_ratio = math.inf
    for hsig in [0.3, 0.6, 1.0, 1.5, 2.0, 3.0]:
        result = hsig_to_face_height(hsig, tp, depth_m=None, formula="komar_gaughan")
        ratio = result / hsig
        assert ratio < prev_ratio, (
            f"K-G ratio at Hsig={hsig}m ({ratio:.3f}) should decrease vs prior ({prev_ratio:.3f})"
        )
        prev_ratio = ratio


def test_komar_gaughan_deepwater_known_values():
    """K-G deepwater: verify a set of formula outputs against hand calculations."""
    G = 9.81
    cases = [
        # (hsig_m, tp_s)
        (0.6, 8.0),
        (0.6, 12.0),
        (1.2, 14.0),
        (1.8, 16.0),
        (2.4, 18.0),
    ]
    for hsig, tp in cases:
        expected = 0.39 * (G ** 0.2) * ((tp * hsig ** 2) ** 0.4)
        result = hsig_to_face_height(hsig, tp, depth_m=None, formula="komar_gaughan")
        assert result == pytest.approx(expected, rel=1e-9), (
            f"Hsig={hsig}, Tp={tp}: expected {expected:.4f}, got {result:.4f}"
        )


# ---------------------------------------------------------------------------
# Period monotonicity: same Hsig, longer period -> taller face height
# ---------------------------------------------------------------------------


def test_period_monotonicity_komar_gaughan():
    """Face height increases monotonically with period for same Hsig (Komar-Gaughan)."""
    hsig = 1.2
    periods = [6, 8, 10, 12, 14, 16, 18, 20]
    heights = [hsig_to_face_height(hsig, tp, formula="komar_gaughan") for tp in periods]
    for i in range(len(heights) - 1):
        assert heights[i] <= heights[i + 1], (
            f"Face height at Tp={periods[i]}s ({heights[i]:.4f}m) should be <= "
            f"Tp={periods[i+1]}s ({heights[i+1]:.4f}m)"
        )


def test_period_monotonicity_caldwell():
    """Face height increases with period for Caldwell formula (Tp >= 10s segment)."""
    hsig = 1.2
    periods = [10, 12, 14, 16, 18, 20]
    heights = [hsig_to_face_height(hsig, tp, formula="caldwell") for tp in periods]
    for i in range(len(heights) - 1):
        assert heights[i] <= heights[i + 1], (
            f"Caldwell face height at Tp={periods[i]}s ({heights[i]:.4f}m) should be <= "
            f"Tp={periods[i+1]}s ({heights[i+1]:.4f}m)"
        )


# ---------------------------------------------------------------------------
# Caldwell formula
# ---------------------------------------------------------------------------


def test_caldwell_at_min_period():
    """Caldwell at exactly Tp=10s returns 1.27 x Hsig (Rayleigh H1/10 factor)."""
    hsig = 1.0
    result = hsig_to_face_height(hsig, 10.0, formula="caldwell")
    # At Tp = CALDWELL_MIN_PERIOD_S (10s), period_factor = 1 + 0.020*(10-10) = 1.0
    # Expected: 1.27 * 1.0 * 1.0 = 1.27m
    expected = 1.27
    assert abs(result - expected) < 0.01, f"Expected {expected:.2f}, got {result:.4f}"


def test_caldwell_amplification_increases_with_period():
    """Caldwell at Tp=16s produces higher amplification than at Tp=10s."""
    hsig = 1.5
    result_10 = hsig_to_face_height(hsig, 10.0, formula="caldwell")
    result_16 = hsig_to_face_height(hsig, 16.0, formula="caldwell")
    assert result_16 > result_10, (
        f"Expected Caldwell at 16s ({result_16:.3f}) > 10s ({result_10:.3f})"
    )


def test_caldwell_h1_10_at_16s():
    """Caldwell at Tp=16s: 1.27 * (1 + 0.020 * 6) * Hsig."""
    hsig = 1.0
    result = hsig_to_face_height(hsig, 16.0, formula="caldwell")
    # period_factor = 1 + 0.020 * (16 - 10) = 1 + 0.12 = 1.12
    expected = 1.27 * 1.0 * 1.12
    assert abs(result - expected) < 0.005, f"Expected {expected:.4f}, got {result:.4f}"


def test_caldwell_rayleigh_factor():
    """Caldwell always produces >= 1.27x Hsig for Tp >= 10s (Rayleigh H1/10 base)."""
    hsig = 1.0
    for tp in [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]:
        result = hsig_to_face_height(hsig, tp, formula="caldwell")
        assert result >= 1.27 * hsig - 1e-9, (
            f"Caldwell at Tp={tp}s should be >= 1.27 x Hsig ({1.27*hsig:.3f}m), "
            f"got {result:.4f}m"
        )


# ---------------------------------------------------------------------------
# Caldwell auto-crossover: Tp < 10s -> Komar-Gaughan
# ---------------------------------------------------------------------------


def test_caldwell_crossover_tp_below_threshold():
    """When formula=caldwell and Tp=8s (<10s), Komar-Gaughan is used."""
    hsig = 1.2
    tp = 8.0  # below CALDWELL_MIN_PERIOD_S
    result_caldwell = hsig_to_face_height(hsig, tp, formula="caldwell")
    result_kg = hsig_to_face_height(hsig, tp, formula="komar_gaughan")
    assert result_caldwell == pytest.approx(result_kg, rel=1e-9), (
        f"Caldwell with Tp<10s should equal K-G: "
        f"caldwell={result_caldwell:.4f}, komar_gaughan={result_kg:.4f}"
    )


def test_caldwell_crossover_tp_exactly_threshold():
    """When formula=caldwell and Tp=10.0s, Caldwell is used (not K-G)."""
    hsig = 1.0
    tp = 10.0
    result_caldwell = hsig_to_face_height(hsig, tp, formula="caldwell")
    result_kg = hsig_to_face_height(hsig, tp, formula="komar_gaughan")
    # Caldwell at Tp=10s: 1.27 x Hsig
    assert abs(result_caldwell - 1.27) < 0.01, f"Expected 1.27, got {result_caldwell:.4f}"
    # K-G at Tp=10s gives a different (higher) value than Caldwell
    assert result_kg != pytest.approx(result_caldwell, rel=0.01), (
        "Caldwell and K-G should differ at Tp=10s"
    )


def test_caldwell_crossover_tp_just_below_threshold():
    """Caldwell with Tp=9.99s falls to Komar-Gaughan."""
    hsig = 1.0
    tp = 9.99
    result_caldwell = hsig_to_face_height(hsig, tp, formula="caldwell")
    result_kg = hsig_to_face_height(hsig, tp, formula="komar_gaughan")
    assert result_caldwell == pytest.approx(result_kg, rel=1e-9)


def test_caldwell_crossover_multiple_periods():
    """Caldwell auto-crossover verified at several sub-threshold periods."""
    for tp in [3.0, 5.0, 7.0, 8.0, 9.0, 9.5, 9.9]:
        r_caldwell = hsig_to_face_height(1.0, tp, formula="caldwell")
        r_kg = hsig_to_face_height(1.0, tp, formula="komar_gaughan")
        assert r_caldwell == pytest.approx(r_kg, rel=1e-9), (
            f"Caldwell with Tp={tp}s should equal K-G"
        )


# ---------------------------------------------------------------------------
# Break-point source: Rayleigh H1/10 factor only (source="break_point")
# ---------------------------------------------------------------------------


def test_break_point_source_applies_h1_10_factor():
    """source="break_point" returns exactly 1.27 x Hs."""
    hsig = 1.5
    result = hsig_to_face_height(hsig, 14.0, source="break_point")
    assert result == pytest.approx(hsig * 1.27, rel=1e-9)


def test_break_point_source_ignores_formula():
    """source="break_point" produces the same result regardless of formula."""
    hsig = 1.2
    r_kg = hsig_to_face_height(hsig, 14.0, formula="komar_gaughan", source="break_point")
    r_cw = hsig_to_face_height(hsig, 14.0, formula="caldwell", source="break_point")
    assert r_kg == pytest.approx(r_cw, rel=1e-9)


def test_break_point_source_differs_from_deep_water():
    """source="break_point" gives a different (lower) result than deep_water for same inputs."""
    hsig = 1.0
    tp = 14.0
    bp = hsig_to_face_height(hsig, tp, source="break_point")
    dw = hsig_to_face_height(hsig, tp, source="deep_water")
    assert bp < dw, (
        f"break_point ({bp:.4f}) should be < deep_water ({dw:.4f}) for same Hs"
    )


def test_break_point_source_zero_hsig():
    """source="break_point" returns 0.0 for zero Hs."""
    assert hsig_to_face_height(0.0, 12.0, source="break_point") == 0.0


def test_break_point_source_clamped():
    """source="break_point" result is clamped within [Hs, 3*Hs]."""
    hsig = 2.0
    result = hsig_to_face_height(hsig, 14.0, source="break_point")
    assert hsig <= result <= hsig * 3.0


def test_unknown_source_raises():
    """Unknown source string raises ValueError."""
    with pytest.raises(ValueError, match="unknown source"):
        hsig_to_face_height(1.0, 12.0, source="nearshore")


# ---------------------------------------------------------------------------
# Clamp bounds
# ---------------------------------------------------------------------------


def test_clamp_lower_bound():
    """Face height is always >= Hsig (lower clamp)."""
    for hsig in [0.3, 0.5, 1.0, 2.0]:
        for tp in [3.0, 5.0, 8.0]:
            result = hsig_to_face_height(hsig, tp, formula="komar_gaughan")
            assert result >= hsig - 1e-9, (
                f"Face height {result:.4f}m < Hsig {hsig}m at Tp={tp}s"
            )


def test_clamp_upper_bound_komar_gaughan():
    """Face height is always <= 3 x Hsig (upper clamp)."""
    hsig = 1.0
    for tp in [20.0, 30.0, 50.0, 100.0]:
        result = hsig_to_face_height(hsig, tp, formula="komar_gaughan")
        assert result <= hsig * 3.0 + 1e-9, (
            f"Face height {result:.4f}m > 3x Hsig={hsig * 3.0}m at Tp={tp}s"
        )


def test_clamp_upper_bound_caldwell():
    """Caldwell face height is always <= 3 x Hsig."""
    hsig = 1.0
    for tp in [15.0, 20.0, 25.0, 30.0, 50.0]:
        result = hsig_to_face_height(hsig, tp, formula="caldwell")
        assert result <= hsig * 3.0 + 1e-9, (
            f"Caldwell: {result:.4f}m > 3x Hsig at Tp={tp}s"
        )


# ---------------------------------------------------------------------------
# Hawaiian height conversion
# ---------------------------------------------------------------------------


def test_hawaiian_height_is_half_face():
    """hawaiian_height returns exactly face_height x 0.5."""
    for face in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.5, 7.0, 10.0]:
        assert hawaiian_height(face) == pytest.approx(face * HAWAIIAN_SCALE_FACTOR, rel=1e-12)


def test_hawaiian_height_is_exactly_half():
    """HAWAIIAN_SCALE_FACTOR is 0.5."""
    assert HAWAIIAN_SCALE_FACTOR == 0.5


def test_hawaiian_height_zero_input():
    """hawaiian_height returns 0.0 for zero input."""
    assert hawaiian_height(0.0) == 0.0


def test_hawaiian_height_negative_input():
    """hawaiian_height returns 0.0 for negative input."""
    assert hawaiian_height(-1.0) == 0.0


def test_hawaiian_height_proportional_to_face_height():
    """Hawaiian height tracks face height at exactly 0.5x for all wave conditions."""
    cases = [(0.6, 8.0), (1.2, 14.0), (1.8, 16.0), (2.4, 18.0)]
    for hsig, tp in cases:
        fh = hsig_to_face_height(hsig, tp, formula="komar_gaughan")
        hi = hawaiian_height(fh)
        assert hi == pytest.approx(fh * 0.5, rel=1e-12), (
            f"Hawaiian height {hi:.4f} should be exactly face {fh:.4f} x 0.5"
        )


def test_hawaiian_always_less_than_face():
    """Hawaiian height is always strictly less than face height for positive inputs."""
    for face in [0.1, 0.5, 1.0, 2.5, 5.0]:
        assert hawaiian_height(face) < face


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_hsig_returns_zero():
    """Zero Hsig input returns 0.0."""
    assert hsig_to_face_height(0.0, 12.0) == 0.0


def test_negative_hsig_returns_zero():
    """Negative Hsig input returns 0.0."""
    assert hsig_to_face_height(-1.0, 12.0) == 0.0


def test_zero_period_returns_zero():
    """Zero period input returns 0.0."""
    assert hsig_to_face_height(1.0, 0.0) == 0.0


def test_negative_period_returns_zero():
    """Negative period input returns 0.0."""
    assert hsig_to_face_height(1.0, -5.0) == 0.0


def test_unknown_formula_raises_value_error():
    """Unknown formula string raises ValueError."""
    with pytest.raises(ValueError, match="unknown formula"):
        hsig_to_face_height(1.0, 12.0, formula="goda")


def test_default_formula_is_komar_gaughan():
    """Default formula parameter is komar_gaughan."""
    result_default = hsig_to_face_height(1.2, 14.0)
    result_explicit = hsig_to_face_height(1.2, 14.0, formula="komar_gaughan")
    assert result_default == result_explicit


def test_small_hsig_small_period_clamped_at_hsig():
    """Very small wave with very short period: face height clamped at Hsig."""
    # K-G might give < Hsig for very short period; clamp ensures >= Hsig
    hsig = 0.1
    tp = 1.0
    result = hsig_to_face_height(hsig, tp)
    assert result >= hsig
