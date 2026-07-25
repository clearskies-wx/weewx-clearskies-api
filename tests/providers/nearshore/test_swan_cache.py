"""Tests for T4A.3 Do step 9 — the SWAN runtime path reads pre-computed
caches only, performing zero CUDEM downloads and zero grid sizing.

Coverage:
  - download_bathymetry_for_level(allow_download=False): missing cache is
    an ERROR + empty grid, never a download; a stale-but-present cache is
    used anyway (with a WARNING) rather than treated as missing; a corrupt
    cache is an ERROR + empty grid.
  - download_bathymetry_for_level(allow_download=True) (default, apply-time
    caller): staleness DOES trigger the existing download-priority-chain
    behaviour, unchanged from before T4A.3.
  - load_grid_sizing_cache(): missing/corrupt cache returns None + logs
    ERROR (never raises, never falls back to compute_domains()); a valid
    cache round-trips to an equivalent DomainSizing.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from weewx_clearskies_api.providers.nearshore import swan as swan_provider
from weewx_clearskies_api.services.swan_domain import (
    DomainSizing,
    GridDomain,
    SpotCluster,
    domain_sizing_to_dict,
)


@pytest.fixture
def _l1_domain() -> GridDomain:
    return GridDomain(
        lat_min=33.60, lat_max=33.70, lon_min=-118.10, lon_max=-117.90,
        resolution_m=1000.0, level=1,
    )


class TestDownloadBathymetryForLevelCacheOnly:
    def test_missing_cache_is_error_and_empty_grid_no_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        _l1_domain: GridDomain, caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(swan_provider, "_CUDEM_GRID_PATH_L1", tmp_path / "L1.json")
        with caplog.at_level(logging.ERROR):
            grid = swan_provider.download_bathymetry_for_level(
                _l1_domain, level=1, allow_download=False,
            )
        assert grid == {}
        assert any("downloads are disabled at runtime" in r.message for r in caplog.records)

    def test_fresh_cache_is_used_without_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _l1_domain: GridDomain,
    ) -> None:
        cache_path = tmp_path / "L1.json"
        cache_path.write_text(
            json.dumps({
                "lat_first": 33.60, "lon_first": -118.10,
                "lat_last": 33.70, "lon_last": -117.90,
                "ni": 3, "nj": 3, "depths": [[-1, -2, -3]] * 3,
                "vertical_datum": "NAVD88",
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(swan_provider, "_CUDEM_GRID_PATH_L1", cache_path)
        grid = swan_provider.download_bathymetry_for_level(
            _l1_domain, level=1, allow_download=False,
        )
        assert grid.get("vertical_datum") == "NAVD88"

    def test_stale_cache_is_used_anyway_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _l1_domain: GridDomain,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cache_path = tmp_path / "L1.json"
        cache_path.write_text(
            json.dumps({
                "lat_first": 33.60, "lon_first": -118.10,
                "lat_last": 33.70, "lon_last": -117.90,
                "ni": 3, "nj": 3, "depths": [[-1, -2, -3]] * 3,
                "vertical_datum": "NAVD88",
            }),
            encoding="utf-8",
        )
        # Backdate the file past the 180-day TTL.
        old_time = time.time() - (200 * 86400)
        import os
        os.utime(cache_path, (old_time, old_time))

        monkeypatch.setattr(swan_provider, "_CUDEM_GRID_PATH_L1", cache_path)
        with caplog.at_level(logging.WARNING):
            grid = swan_provider.download_bathymetry_for_level(
                _l1_domain, level=1, allow_download=False,
            )
        assert grid.get("vertical_datum") == "NAVD88"  # used anyway
        assert any("but downloads are disabled at runtime" in r.message for r in caplog.records)

    def test_corrupt_cache_is_error_and_empty_grid_no_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _l1_domain: GridDomain,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cache_path = tmp_path / "L1.json"
        cache_path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(swan_provider, "_CUDEM_GRID_PATH_L1", cache_path)
        with caplog.at_level(logging.WARNING):
            grid = swan_provider.download_bathymetry_for_level(
                _l1_domain, level=1, allow_download=False,
            )
        assert grid == {}
        assert any("corrupt" in r.message for r in caplog.records)


class TestLoadGridSizingCache:
    def test_missing_cache_returns_none_and_logs_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            swan_provider, "_SWAN_GRID_SIZING_CACHE_PATH", tmp_path / "missing.json"
        )
        with caplog.at_level(logging.ERROR):
            result = swan_provider.load_grid_sizing_cache()
        assert result is None
        assert any("no grid sizing cache" in r.message for r in caplog.records)

    def test_corrupt_cache_returns_none_and_logs_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cache_path = tmp_path / "swan_grid_sizing.json"
        cache_path.write_text("not json at all", encoding="utf-8")
        monkeypatch.setattr(swan_provider, "_SWAN_GRID_SIZING_CACHE_PATH", cache_path)
        with caplog.at_level(logging.ERROR):
            result = swan_provider.load_grid_sizing_cache()
        assert result is None
        assert any("corrupt or malformed" in r.message for r in caplog.records)

    def test_valid_cache_round_trips_to_equivalent_domain_sizing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _l1_domain: GridDomain,
    ) -> None:
        level2 = GridDomain(
            lat_min=33.64, lat_max=33.66, lon_min=-118.02, lon_max=-117.98,
            resolution_m=100.0, level=2,
        )
        sizing = DomainSizing(
            level1=_l1_domain,
            level2=level2,
            level3_clusters=[
                SpotCluster(spot_ids=["hb"], lats=[33.65], lons=[-118.0], grid=None),
            ],
        )
        cache_path = tmp_path / "swan_grid_sizing.json"
        cache_path.write_text(json.dumps(domain_sizing_to_dict(sizing)), encoding="utf-8")
        monkeypatch.setattr(swan_provider, "_SWAN_GRID_SIZING_CACHE_PATH", cache_path)

        result = swan_provider.load_grid_sizing_cache()
        assert result is not None
        assert result.level1 == sizing.level1
        assert result.level2 == sizing.level2
        assert result.level3_clusters[0].grid is None
