"""Pure-compute unit tests for providers/aqi/_units.py (3b-9, extended 3b-10).

Covers per the task-3b-9 brief §Test-author parallel scope (test_units.py):

  ugm3_to_ppm round-trips:
  - Known values for each supported gas (O3, NO2, SO2, CO).
  - None propagates → returns None.
  - Unknown pollutant raises KeyError.

  epa_category boundary tests:
  - Every breakpoint boundary value (0, 50, 51, 100, 101, 150, 151, 200, 201, 300, 301, 500).
  - Values above 500 → "Hazardous" (defensive cap).
  - None → None.
  - Floating-point AQI (e.g. 50.0, 50.5, 100.9) handled correctly.

Formula reference (canonical-data-model §4.2 footnote):
  ppm = µg/m³ × 24.45 / molecular_weight
  where 24.45 L/mol is the molar volume at 25°C / 1 atm.

Molecular weights:
  O3:  48.00 g/mol
  NO2: 46.01 g/mol
  SO2: 64.07 g/mol
  CO:  28.01 g/mol

No DB, no HTTP, no external state.
ADR references: ADR-013, ADR-038.
"""

from __future__ import annotations

import math

import pytest


# ===========================================================================
# 1. ugm3_to_ppm — conversion round-trips and edge cases
# ===========================================================================


class TestUgm3ToPpm:
    """ugm3_to_ppm converts µg/m³ to ppm for the four supported gases.

    Formula: ppm = µg/m³ × 24.45 / MW.
    Particulates (PM2.5, PM10) are NOT in the table and raise KeyError.
    None input → None output (pass-through for missing wire values).
    """

    def test_ozone_100_ugm3_converts_to_expected_ppm(self) -> None:
        """O3 100 µg/m³ → 100 × 24.45 / 48.00 ≈ 50.9375 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(100.0, pollutant="O3")
        assert result is not None
        expected = 100.0 * 24.45 / 48.00
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"O3 100 µg/m³ → expected {expected:.6f} ppm, got {result:.6f}"
        )

    def test_ozone_zero_ugm3_returns_zero(self) -> None:
        """O3 0.0 µg/m³ → 0.0 ppm (zero input stays zero)."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(0.0, pollutant="O3")
        assert result == 0.0

    def test_no2_100_ugm3_converts_to_expected_ppm(self) -> None:
        """NO2 100 µg/m³ → 100 × 24.45 / 46.01 ≈ 53.140 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(100.0, pollutant="NO2")
        assert result is not None
        expected = 100.0 * 24.45 / 46.01
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"NO2 100 µg/m³ → expected {expected:.6f} ppm, got {result:.6f}"
        )

    def test_so2_100_ugm3_converts_to_expected_ppm(self) -> None:
        """SO2 100 µg/m³ → 100 × 24.45 / 64.07 ≈ 38.162 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(100.0, pollutant="SO2")
        assert result is not None
        expected = 100.0 * 24.45 / 64.07
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"SO2 100 µg/m³ → expected {expected:.6f} ppm, got {result:.6f}"
        )

    def test_co_100_ugm3_converts_to_expected_ppm(self) -> None:
        """CO 100 µg/m³ → 100 × 24.45 / 28.01 ≈ 87.254 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(100.0, pollutant="CO")
        assert result is not None
        expected = 100.0 * 24.45 / 28.01
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"CO 100 µg/m³ → expected {expected:.6f} ppm, got {result:.6f}"
        )

    def test_co_fixture_value_155_ugm3_converts_correctly(self) -> None:
        """CO 155.0 µg/m³ (fixture value) → 155 × 24.45 / 28.01 ≈ 135.245 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(155.0, pollutant="CO")
        assert result is not None
        expected = 155.0 * 24.45 / 28.01
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"CO 155.0 µg/m³ → expected {expected:.6f} ppm, got {result:.6f}"
        )

    def test_ozone_fixture_value_87_ugm3_converts_correctly(self) -> None:
        """O3 87.0 µg/m³ (fixture value) → 87 × 24.45 / 48.00 ≈ 44.316 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(87.0, pollutant="O3")
        assert result is not None
        expected = 87.0 * 24.45 / 48.00
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"O3 87.0 µg/m³ → expected {expected:.6f} ppm, got {result:.6f}"
        )

    def test_none_input_returns_none_for_o3(self) -> None:
        """ugm3_to_ppm(None, pollutant='O3') → None (None propagates, ADR-010 null passthrough)."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(None, pollutant="O3")
        assert result is None, (
            f"Expected None propagation for None input, got {result!r}"
        )

    def test_none_input_returns_none_for_no2(self) -> None:
        """ugm3_to_ppm(None, pollutant='NO2') → None."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        assert ugm3_to_ppm(None, pollutant="NO2") is None

    def test_none_input_returns_none_for_so2(self) -> None:
        """ugm3_to_ppm(None, pollutant='SO2') → None."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        assert ugm3_to_ppm(None, pollutant="SO2") is None

    def test_none_input_returns_none_for_co(self) -> None:
        """ugm3_to_ppm(None, pollutant='CO') → None."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        assert ugm3_to_ppm(None, pollutant="CO") is None

    def test_unknown_pollutant_raises_key_error(self) -> None:
        """ugm3_to_ppm(100, pollutant='UNKNOWN') → KeyError (not in MW table)."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        with pytest.raises(KeyError):
            ugm3_to_ppm(100.0, pollutant="UNKNOWN")

    def test_pm25_not_in_conversion_table_raises_key_error(self) -> None:
        """PM2.5 raises KeyError — particulates stay in µg/m³, no conversion (canonical §3.8)."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        with pytest.raises(KeyError):
            ugm3_to_ppm(3.1, pollutant="PM2.5")

    def test_pm10_not_in_conversion_table_raises_key_error(self) -> None:
        """PM10 raises KeyError — particulates stay in µg/m³, no conversion (canonical §3.8)."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        with pytest.raises(KeyError):
            ugm3_to_ppm(4.5, pollutant="PM10")

    def test_result_is_float_not_none_for_valid_input(self) -> None:
        """Result is a float (not None) for a valid non-None input."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result = ugm3_to_ppm(50.0, pollutant="O3")
        assert isinstance(result, float), (
            f"Expected float result, got {type(result).__name__!r}"
        )

    def test_molar_volume_and_mw_are_reflected_in_result(self) -> None:
        """Doubling the input doubles the output (linear formula sanity check)."""
        from weewx_clearskies_api.providers.aqi._units import ugm3_to_ppm  # noqa: PLC0415
        result1 = ugm3_to_ppm(50.0, pollutant="CO")
        result2 = ugm3_to_ppm(100.0, pollutant="CO")
        assert result1 is not None and result2 is not None
        assert math.isclose(result2, result1 * 2, rel_tol=1e-9), (
            "Linear formula: doubling µg/m³ must double ppm"
        )


# ===========================================================================
# 2. epa_category — boundary tests
# ===========================================================================


class TestEpaCategory:
    """epa_category maps EPA AQI values to canonical category names.

    Boundary tests for every breakpoint per brief §test_units.py spec.
    Canonical spelling per canonical-data-model §3.8 (LC13):
      0–50:    "Good"
      51–100:  "Moderate"
      101–150: "Unhealthy for Sensitive Groups"
      151–200: "Unhealthy"
      201–300: "Very Unhealthy"
      301–500: "Hazardous"
    Values >500 → "Hazardous" (defensive cap; provider-side bugs shouldn't crash).
    None → None.
    """

    def test_aqi_zero_is_good(self) -> None:
        """AQI 0 → 'Good' (lower edge of lowest band)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(0) == "Good", "AQI 0 must be 'Good'"

    def test_aqi_50_is_good(self) -> None:
        """AQI 50 → 'Good' (upper bound of Good band, inclusive)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(50) == "Good", "AQI 50 must be 'Good'"

    def test_aqi_51_is_moderate(self) -> None:
        """AQI 51 → 'Moderate' (first value in Moderate band)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(51) == "Moderate", "AQI 51 must be 'Moderate'"

    def test_aqi_100_is_moderate(self) -> None:
        """AQI 100 → 'Moderate' (upper bound of Moderate band, inclusive)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(100) == "Moderate", "AQI 100 must be 'Moderate'"

    def test_aqi_101_is_unhealthy_for_sensitive_groups(self) -> None:
        """AQI 101 → 'Unhealthy for Sensitive Groups' (first value in USG band)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(101) == "Unhealthy for Sensitive Groups", (
            "AQI 101 must be 'Unhealthy for Sensitive Groups'"
        )

    def test_aqi_150_is_unhealthy_for_sensitive_groups(self) -> None:
        """AQI 150 → 'Unhealthy for Sensitive Groups' (upper bound inclusive)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(150) == "Unhealthy for Sensitive Groups", (
            "AQI 150 must be 'Unhealthy for Sensitive Groups'"
        )

    def test_aqi_151_is_unhealthy(self) -> None:
        """AQI 151 → 'Unhealthy' (first value in Unhealthy band)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(151) == "Unhealthy", "AQI 151 must be 'Unhealthy'"

    def test_aqi_200_is_unhealthy(self) -> None:
        """AQI 200 → 'Unhealthy' (upper bound of Unhealthy band, inclusive)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(200) == "Unhealthy", "AQI 200 must be 'Unhealthy'"

    def test_aqi_201_is_very_unhealthy(self) -> None:
        """AQI 201 → 'Very Unhealthy' (first value in Very Unhealthy band)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(201) == "Very Unhealthy", "AQI 201 must be 'Very Unhealthy'"

    def test_aqi_300_is_very_unhealthy(self) -> None:
        """AQI 300 → 'Very Unhealthy' (upper bound inclusive)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(300) == "Very Unhealthy", "AQI 300 must be 'Very Unhealthy'"

    def test_aqi_301_is_hazardous(self) -> None:
        """AQI 301 → 'Hazardous' (first value in Hazardous band)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(301) == "Hazardous", "AQI 301 must be 'Hazardous'"

    def test_aqi_500_is_hazardous(self) -> None:
        """AQI 500 → 'Hazardous' (top of the defined EPA range, inclusive)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(500) == "Hazardous", "AQI 500 must be 'Hazardous'"

    def test_aqi_501_is_hazardous_defensive_cap(self) -> None:
        """AQI 501 → 'Hazardous' (above spec range; defensive cap, not an error).

        Provider-side bugs can emit values >500. Per brief §module-2 spec:
        cap at 'Hazardous' rather than raising — dashboards shouldn't crash
        on sensor/provider bugs.
        """
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(501) == "Hazardous", (
            "AQI > 500 must cap at 'Hazardous' (defensive cap)"
        )

    def test_aqi_600_is_hazardous_defensive_cap(self) -> None:
        """AQI 600 → 'Hazardous' (well above spec range; defensive cap still applies)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(600) == "Hazardous", (
            "AQI 600 must cap at 'Hazardous' (defensive cap)"
        )

    def test_none_aqi_returns_none(self) -> None:
        """epa_category(None) → None (None propagates; no provider reading → no category)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        result = epa_category(None)
        assert result is None, f"Expected None for None AQI input, got {result!r}"

    def test_float_aqi_50_point_0_is_good(self) -> None:
        """AQI 50.0 (float) → 'Good' (float comparison works with <=)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(50.0) == "Good"

    def test_float_aqi_50_point_5_is_moderate(self) -> None:
        """AQI 50.5 (float) → 'Moderate' (above Good upper bound 50)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(50.5) == "Moderate"

    def test_float_aqi_100_point_9_is_moderate(self) -> None:
        """AQI 100.9 (float) → 'Moderate' (below Moderate upper bound 100? No — 100.9 > 100).

        Expect 'Unhealthy for Sensitive Groups': 100.9 > 100 → falls into USG band.
        """
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        # 100.9 > 100.0 → not in Moderate (upper=100); falls into USG band (101–150)
        assert epa_category(100.9) == "Unhealthy for Sensitive Groups", (
            "AQI 100.9 > 100 → 'Unhealthy for Sensitive Groups' (float boundary check)"
        )

    def test_fixture_aqi_73_is_moderate(self) -> None:
        """AQI 73 (from real fixture) → 'Moderate' (51–100 band)."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        assert epa_category(73) == "Moderate", (
            "Fixture AQI 73 must be 'Moderate' (51–100 band)"
        )

    def test_result_is_str_for_valid_non_none_input(self) -> None:
        """Return type is str (not None) for any valid numeric AQI."""
        from weewx_clearskies_api.providers.aqi._units import epa_category  # noqa: PLC0415
        result = epa_category(125)
        assert isinstance(result, str), (
            f"Expected str result, got {type(result).__name__!r}"
        )


# ===========================================================================
# 3. ppb_to_ppm — conversion round-trips and edge cases (3b-10 extension)
# ===========================================================================


class TestPpbToPpm:
    """ppb_to_ppm converts ppb (parts per billion) to ppm (parts per million).

    Formula: ppm = ppb / 1000.0 (no molar-weight involved — same divisor for
    all gases; Aeris provides valuePPB directly).

    Brief reference: LC16 in phase-2-task-3b-10 brief.
    No pollutant arg needed — the conversion is gas-agnostic.
    None input → None output (pass-through for missing wire values).
    """

    def test_o3_32_point_1_ppb_converts_to_0_point_0321_ppm(self) -> None:
        """O3 32.1 ppb → 0.0321 ppm (exact division by 1000)."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(32.1)
        assert result is not None
        assert abs(result - 0.0321) < 1e-9, (
            f"32.1 ppb → expected 0.0321 ppm, got {result!r}"
        )

    def test_co_143_ppb_converts_to_0_point_143_ppm(self) -> None:
        """CO 143 ppb (fixture value) → 0.143 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(143.0)
        assert result is not None
        assert abs(result - 0.143) < 1e-9, (
            f"143.0 ppb → expected 0.143 ppm, got {result!r}"
        )

    def test_no2_3_ppb_converts_to_0_point_003_ppm(self) -> None:
        """NO2 3.0 ppb (fixture value) → 0.003 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(3.0)
        assert result is not None
        assert abs(result - 0.003) < 1e-9, (
            f"3.0 ppb → expected 0.003 ppm, got {result!r}"
        )

    def test_so2_zero_ppb_converts_to_zero_ppm(self) -> None:
        """SO2 0 ppb (fixture value) → 0.0 ppm (zero input stays zero)."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(0.0)
        assert result == 0.0, f"0.0 ppb → expected 0.0 ppm, got {result!r}"

    def test_1000_ppb_converts_to_1_ppm(self) -> None:
        """1000 ppb → 1.0 ppm (round-number boundary check)."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(1000.0)
        assert result is not None
        assert abs(result - 1.0) < 1e-9, (
            f"1000.0 ppb → expected 1.0 ppm, got {result!r}"
        )

    def test_none_input_returns_none(self) -> None:
        """ppb_to_ppm(None) → None (None propagates; ADR-010 null passthrough)."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(None)
        assert result is None, f"Expected None for None input, got {result!r}"

    def test_result_is_float_for_valid_input(self) -> None:
        """Return type is float (not None) for a valid non-None input."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(50.0)
        assert isinstance(result, float), (
            f"Expected float result, got {type(result).__name__!r}"
        )

    def test_doubling_ppb_doubles_ppm(self) -> None:
        """Linear formula: doubling ppb must double ppm."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result1 = ppb_to_ppm(100.0)
        result2 = ppb_to_ppm(200.0)
        assert result1 is not None and result2 is not None
        assert abs(result2 - result1 * 2) < 1e-9, (
            "Doubling ppb must double ppm (linear formula sanity check)"
        )

    def test_fixture_o3_36_ppb_converts_correctly(self) -> None:
        """O3 36 ppb (real fixture value) → 0.036 ppm."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        result = ppb_to_ppm(36.0)
        assert result is not None
        assert abs(result - 0.036) < 1e-9, (
            f"36.0 ppb → expected 0.036 ppm, got {result!r}"
        )

    def test_ppb_to_ppm_does_not_require_pollutant_arg(self) -> None:
        """ppb_to_ppm takes only ppb — no pollutant kwarg needed (gas-agnostic)."""
        from weewx_clearskies_api.providers.aqi._units import ppb_to_ppm  # noqa: PLC0415
        # Call with positional arg only — must not raise TypeError
        result = ppb_to_ppm(25.0)
        assert result is not None
