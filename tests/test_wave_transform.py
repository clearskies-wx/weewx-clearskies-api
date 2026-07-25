"""Unit tests for the sub-grid bilinear interpolation helper (Phase 3, T3.2;
reduced T4A.7).

Module under test: weewx_clearskies_api/enrichment/wave_transform.py

T4A.7 (2026-07-25) removed both surviving SWAN supplements and the
``apply_supplements()`` entry point: Supplement 1 (breaker gamma
correction) was dead code, and Supplement 3 (sub-grid bilinear
interpolation as a SWAN-Hs supplement) was made redundant by SWAN's own
POINTS/SPECOUT interpolation (see the module docstring for the SWAN manual
citation). ``bilinear_interpolate()`` itself survives only because
``endpoints/surf.py`` uses it for an unrelated purpose — HRRR wind
interpolation (LC-R2-15) — so it is still covered here.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.enrichment import wave_transform as wt

# ---------------------------------------------------------------------------
# Sub-grid bilinear interpolation
# ---------------------------------------------------------------------------


def test_bilinear_at_node():
    grid_data = [[1.0, 2.0], [3.0, 4.0]]
    grid_lats = [0.0, 1.0]
    grid_lons = [0.0, 1.0]
    assert wt.bilinear_interpolate(grid_data, grid_lats, grid_lons, 0.0, 0.0) == pytest.approx(1.0)
    assert wt.bilinear_interpolate(grid_data, grid_lats, grid_lons, 1.0, 1.0) == pytest.approx(4.0)


def test_bilinear_midpoint():
    grid_data = [[5.0, 5.0], [5.0, 5.0]]
    grid_lats = [0.0, 1.0]
    grid_lons = [0.0, 1.0]
    result = wt.bilinear_interpolate(grid_data, grid_lats, grid_lons, 0.5, 0.5)
    assert result == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


def test_no_shoaling_code():
    import inspect

    source = inspect.getsource(wt)
    assert "def calculate_shoaling" not in source
    assert "def calculate_refraction" not in source


def test_no_supplement_code():
    """T4A.7 (2026-07-25): apply_supplements() and both surviving supplements
    (breaker gamma correction, sub-grid interpolation as a supplement) are
    removed. Only the bilinear helper itself remains, for the unrelated
    HRRR wind caller in endpoints/surf.py (LC-R2-15)."""
    assert not hasattr(wt, "apply_supplements")
    assert not hasattr(wt, "apply_breaker_correction")
    assert not hasattr(wt, "compute_iribarren")
    assert not hasattr(wt, "compute_breaker_gamma")
    assert hasattr(wt, "bilinear_interpolate")
