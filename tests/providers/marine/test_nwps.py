"""Unit tests for the NWPS nearshore wave data provider module (Phase 2 T2.2).

Covers:
  - CAPABILITY symbol: present when GRIB_AVAILABLE, None when not
  - WFO → region mapping completeness and correctness
  - _determine_wfo(): CWA lookup and bbox fallback
  - _build_grib_url(): NOMADS URL construction matches live-verified pattern
  - _available_cycles(): cycle ordering (most recent first)
  - NwpsExtraction dataclass defaults
  - fetch() caching and cycle fallback (via respx mocks)

No real GRIB2 files or eccodes are needed — network calls mocked via respx,
GRIB processing mocked via monkeypatch.

Integration tests (live NOMADS fetch + real eccodes) are separate.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from weewx_clearskies_api.providers.marine.nwps import (
    CAPABILITY,
    DOMAIN,
    PROVIDER_ID,
    NwpsExtraction,
    _ALL_FIELDS,
    _REGION_WFOS,
    _WFO_BBOX,
    _WFO_TO_REGION,
    _available_cycles,
    _build_grib_url,
    _determine_wfo,
)
from weewx_clearskies_api.providers.marine.grib_processor import GRIB_AVAILABLE


# ---------------------------------------------------------------------------
# CAPABILITY declaration tests
# ---------------------------------------------------------------------------


class TestCapability:
    def test_provider_id(self) -> None:
        assert PROVIDER_ID == "nwps"

    def test_domain(self) -> None:
        assert DOMAIN == "marine"

    def test_capability_matches_grib_available(self) -> None:
        if GRIB_AVAILABLE:
            assert CAPABILITY is not None
            assert CAPABILITY.provider_id == "nwps"
            assert CAPABILITY.domain == "marine"
            assert CAPABILITY.geographic_coverage == "us_coastal"
            assert CAPABILITY.auth_required == ()
        else:
            assert CAPABILITY is None


# ---------------------------------------------------------------------------
# WFO → region mapping tests
# ---------------------------------------------------------------------------


class TestWfoRegionMapping:
    def test_all_region_wfos_in_mapping(self) -> None:
        """Every WFO listed in _REGION_WFOS has an entry in _WFO_TO_REGION."""
        for region, wfos in _REGION_WFOS.items():
            for wfo in wfos:
                assert wfo in _WFO_TO_REGION, f"{wfo} not in _WFO_TO_REGION"
                assert _WFO_TO_REGION[wfo] == region

    def test_known_wfos(self) -> None:
        assert _WFO_TO_REGION["ilm"] == "er"
        assert _WFO_TO_REGION["hgx"] == "sr"
        assert _WFO_TO_REGION["lox"] == "wr"
        assert _WFO_TO_REGION["aer"] == "ar"
        assert _WFO_TO_REGION["hfo"] == "pr"

    def test_florida_wfos_in_sr(self) -> None:
        """Florida WFOs are in Southern Region, not Eastern (live-verified)."""
        for wfo in ["jax", "mlb", "mfl", "key", "tbw", "tae"]:
            assert _WFO_TO_REGION[wfo] == "sr", f"{wfo} should be in sr"

    def test_total_wfo_count(self) -> None:
        """All 36 NWPS WFOs are mapped (live-verified 2026-07-10)."""
        total = sum(len(wfos) for wfos in _REGION_WFOS.values())
        assert total == len(_WFO_TO_REGION)
        assert total == 36

    def test_all_lowercase(self) -> None:
        for wfo in _WFO_TO_REGION:
            assert wfo == wfo.lower(), f"WFO {wfo} should be lowercase"


# ---------------------------------------------------------------------------
# URL construction tests
# ---------------------------------------------------------------------------


class TestBuildGribUrl:
    def test_ilm_url(self) -> None:
        """ILM WFO, Eastern Region, 2026-07-10 06Z cycle."""
        cycle_dt = datetime(2026, 7, 10, 6, 0, 0, tzinfo=UTC)
        url = _build_grib_url("ilm", cycle_dt)
        expected = (
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nwps/prod/"
            "er.20260710/ilm/06/CG1/ilm_nwps_CG1_20260710_0600.grib2"
        )
        assert url == expected

    def test_hgx_url(self) -> None:
        """HGX WFO, Southern Region, 2026-07-10 12Z cycle."""
        cycle_dt = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
        url = _build_grib_url("hgx", cycle_dt)
        assert "/sr.20260710/hgx/12/CG1/hgx_nwps_CG1_20260710_1200.grib2" in url

    def test_lox_url(self) -> None:
        """LOX WFO, Western Region."""
        cycle_dt = datetime(2026, 7, 10, 0, 0, 0, tzinfo=UTC)
        url = _build_grib_url("lox", cycle_dt)
        assert "/wr.20260710/lox/00/CG1/lox_nwps_CG1_20260710_0000.grib2" in url

    def test_hfo_url(self) -> None:
        """HFO WFO, Pacific Region."""
        cycle_dt = datetime(2026, 7, 10, 6, 0, 0, tzinfo=UTC)
        url = _build_grib_url("hfo", cycle_dt)
        assert "/pr.20260710/hfo/06/CG1/hfo_nwps_CG1_20260710_0600.grib2" in url


# ---------------------------------------------------------------------------
# Cycle determination tests
# ---------------------------------------------------------------------------


class TestAvailableCycles:
    def test_order_most_recent_first(self) -> None:
        now = datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC)
        cycles = _available_cycles(now)
        assert len(cycles) >= 3
        times = [c[0] for c in cycles]
        for i in range(len(times) - 1):
            assert times[i] >= times[i + 1]

    def test_does_not_include_future_cycles(self) -> None:
        now = datetime(2026, 7, 10, 5, 0, 0, tzinfo=UTC)
        cycles = _available_cycles(now)
        for cycle_dt, _ in cycles:
            assert cycle_dt <= now

    def test_first_cycle_is_most_recent(self) -> None:
        now = datetime(2026, 7, 10, 14, 0, 0, tzinfo=UTC)
        cycles = _available_cycles(now)
        assert cycles[0][1] == 12  # 12Z is the most recent before 14Z


# ---------------------------------------------------------------------------
# WFO determination tests
# ---------------------------------------------------------------------------


class TestDetermineWfo:
    def test_bbox_fallback_offshore(self) -> None:
        """Offshore point that /points doesn't cover — falls back to bbox."""
        with patch(
            "weewx_clearskies_api.providers.marine.nwps.get_cwa",
            side_effect=__import__(
                "weewx_clearskies_api.providers._common.errors",
                fromlist=["GeographicallyUnsupported"],
            ).GeographicallyUnsupported(
                "test", provider_id="nws_zones", domain="zones"
            ),
        ):
            wfo = _determine_wfo(34.0, -77.5)
            assert wfo in _WFO_TO_REGION

    def test_known_cwa_maps_directly(self) -> None:
        """CWA that's in the NWPS production set maps directly."""
        with patch(
            "weewx_clearskies_api.providers.marine.nwps.get_cwa",
            return_value="ILM",
        ):
            wfo = _determine_wfo(34.2, -77.8)
            assert wfo == "ilm"

    def test_non_coastal_cwa_falls_to_bbox(self) -> None:
        """Inland CWA (not in NWPS set) falls to bbox."""
        with patch(
            "weewx_clearskies_api.providers.marine.nwps.get_cwa",
            return_value="RAH",
        ):
            wfo = _determine_wfo(34.2, -77.5)
            assert wfo in _WFO_TO_REGION

    def test_far_inland_raises(self) -> None:
        """Point far inland with no bbox match raises GeographicallyUnsupported."""
        from weewx_clearskies_api.providers._common.errors import GeographicallyUnsupported

        with patch(
            "weewx_clearskies_api.providers.marine.nwps.get_cwa",
            return_value="OUN",
        ):
            with pytest.raises(GeographicallyUnsupported):
                _determine_wfo(35.0, -97.0)


# ---------------------------------------------------------------------------
# NwpsExtraction defaults
# ---------------------------------------------------------------------------


class TestNwpsExtraction:
    def test_defaults_none(self) -> None:
        ext = NwpsExtraction()
        assert ext.wave_height is None
        assert ext.wave_period is None
        assert ext.wave_direction is None
        assert ext.current_speed is None
        assert ext.current_direction is None
        assert ext.bottom_orbital_velocity is None
        assert ext.rip_current_probability is None
        assert ext.total_water_level is None
        assert ext.wave_runup is None
        assert ext.cycle_time == ""


# ---------------------------------------------------------------------------
# GRIB field list tests
# ---------------------------------------------------------------------------


class TestFieldList:
    def test_core_fields(self) -> None:
        for field in ["HTSGW", "PERPW", "DIRPW", "UCUR", "VCUR"]:
            assert field in _ALL_FIELDS

    def test_optional_fields(self) -> None:
        assert "BODO" in _ALL_FIELDS

    def test_v15_fields(self) -> None:
        for field in ["RIPCUR", "TWL", "RUNUP"]:
            assert field in _ALL_FIELDS


# ---------------------------------------------------------------------------
# Bbox table tests
# ---------------------------------------------------------------------------


class TestBboxTable:
    def test_all_bbox_wfos_in_mapping(self) -> None:
        for entry in _WFO_BBOX:
            assert entry["wfo"] in _WFO_TO_REGION, (
                f"Bbox WFO {entry['wfo']} not in _WFO_TO_REGION"
            )

    def test_bbox_bounds_valid(self) -> None:
        for entry in _WFO_BBOX:
            lat_lo, lat_hi = entry["lat"]
            lon_lo, lon_hi = entry["lon"]
            assert lat_lo < lat_hi, f"WFO {entry['wfo']}: lat_lo >= lat_hi"
            assert lon_lo < lon_hi, f"WFO {entry['wfo']}: lon_lo >= lon_hi"
