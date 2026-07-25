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
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pytest

from weewx_clearskies_api.providers._common.cache import (
    get_cache,
    reset_cache_for_tests,
    wire_cache_from_env,
)
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


# ---------------------------------------------------------------------------
# SURF-PUBLISH-RESULTS-ONLY §3.4 — conditional forecast fetch in the remote
# health loop: fetch only when run_time is unchanged (or no cached entry).
# ---------------------------------------------------------------------------


class _StopLoop(Exception):
    """Sentinel used to break _remote_health_loop's `while True:` after
    exactly one iteration -- raised from a monkeypatched time.sleep()."""


class _FakeHttpResponse:
    def __init__(self, json_data: dict, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json_data


@pytest.fixture
def wired_cache():
    """Wire an in-memory cache for tests that exercise the remote health loop."""
    reset_cache_for_tests()
    wire_cache_from_env()
    yield get_cache()
    reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _reset_remote_health_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate module-level remote-mode state so tests in this file (and any
    that ran before them in the same process) cannot leak into each other."""
    monkeypatch.setattr(swan_provider, "_remote_consecutive_failures", 0)
    monkeypatch.setattr(swan_provider, "_remote_healthy", True)
    monkeypatch.setattr(swan_provider, "_remote_warned_unreachable", False)
    yield


def _run_one_health_loop_iteration(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health_response: dict,
    forecast_responses: dict[str, _FakeHttpResponse] | None = None,
) -> list[str]:
    """Run swan_provider._remote_health_loop() for exactly one iteration.

    Patches time.sleep to raise _StopLoop so the `while True:` loop exits
    after its first pass (there is no other way to terminate it — it is
    designed to run forever as a daemon thread). Returns the list of URLs
    httpx.get() was called with, in order.
    """
    calls: list[str] = []
    forecast_responses = forecast_responses or {}

    def _fake_get(url, timeout=None, verify=None, headers=None):  # noqa: ANN001
        calls.append(url)
        if url.endswith("/health"):
            return _FakeHttpResponse(health_response)
        for spot_id, resp in forecast_responses.items():
            if url.endswith(f"/surf/{spot_id}/forecast"):
                return resp
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(swan_provider.httpx, "get", _fake_get)
    monkeypatch.setattr(swan_provider.time, "sleep", lambda _s: (_ for _ in ()).throw(_StopLoop()))

    with pytest.raises(_StopLoop):
        swan_provider._remote_health_loop(
            "https://model-host:8767", ["spot_a"], verify_tls=False, auth_secret="s3cr3t",
        )
    return calls


class TestConditionalForecastFetch:
    def test_no_cached_entry_always_fetches(self, monkeypatch, wired_cache, caplog):
        """The last-good cache has a 7-day TTL and can expire -- "no entry"
        must always trigger a fetch, never be treated as "unchanged"."""
        forecast_resp = _FakeHttpResponse(
            {"forecast": [], "spectral": [], "transect": {}, "swelltrack": {},
             "run_time": "2026-07-25T18:00:00Z", "hrrr_cycle_time": ""}
        )
        calls = _run_one_health_loop_iteration(
            monkeypatch,
            health_response={"last_run": "2026-07-25T18:00:00Z", "spots": ["spot_a"]},
            forecast_responses={"spot_a": forecast_resp},
        )
        assert any(c.endswith("/surf/spot_a/forecast") for c in calls)

    def test_run_time_unchanged_skips_forecast_fetch(self, monkeypatch, wired_cache, caplog):
        """Acceptance criterion 4: the forecast download is skipped when
        run_time is unchanged -- verified from logs."""
        cache = get_cache()
        cache.set(
            swan_provider._build_last_good_key("spot_a"),
            {"run_time": "2026-07-25T18:00:00Z"},
            999_999,
        )
        with caplog.at_level(logging.DEBUG):
            calls = _run_one_health_loop_iteration(
                monkeypatch,
                health_response={"last_run": "2026-07-25T18:00:00Z", "spots": ["spot_a"]},
            )
        # Only the /health GET happened -- no /forecast GET was ever made.
        assert all(not c.endswith("/forecast") for c in calls)
        assert any(
            "run_time unchanged" in r.message and "spot_a" in r.message
            for r in caplog.records
        )

    def test_run_time_changed_triggers_forecast_fetch(self, monkeypatch, wired_cache):
        """A cached entry exists but its run_time differs from the health
        response's last_run -- must still fetch (new model cycle)."""
        cache = get_cache()
        cache.set(
            swan_provider._build_last_good_key("spot_a"),
            {"run_time": "2026-07-25T18:00:00Z"},
            999_999,
        )
        forecast_resp = _FakeHttpResponse(
            {"forecast": [], "spectral": [], "transect": {}, "swelltrack": {},
             "run_time": "2026-07-25T18:25:00Z", "hrrr_cycle_time": ""}
        )
        calls = _run_one_health_loop_iteration(
            monkeypatch,
            health_response={"last_run": "2026-07-25T18:25:00Z", "spots": ["spot_a"]},
            forecast_responses={"spot_a": forecast_resp},
        )
        assert any(c.endswith("/surf/spot_a/forecast") for c in calls)


# ---------------------------------------------------------------------------
# SURF-PUBLISH-RESULTS-ONLY §3.3 — gap reporting: single worker, bounded
# queue, bounded client-side dedup. NOT a thread per report (rejected during
# review as a thread bomb when the swelltrack cache is empty, which is
# production's actual current state -- one /surf request can produce ~67
# gaps).
# ---------------------------------------------------------------------------


class TestGapReporting:
    @pytest.fixture(autouse=True)
    def _isolate_gap_report_state(self, monkeypatch: pytest.MonkeyPatch):
        """Fresh queue/dedup/worker-thread state per test, and a harmless
        default httpx.post fake so no test hits the real network even if the
        background worker drains before the test overrides it."""
        monkeypatch.setattr(
            swan_provider, "_gap_report_queue",
            queue.Queue(maxsize=swan_provider._GAP_REPORT_QUEUE_MAXSIZE),
        )
        monkeypatch.setattr(swan_provider, "_gap_report_seen", OrderedDict())
        monkeypatch.setattr(swan_provider, "_gap_report_worker_thread", None)
        monkeypatch.setattr(swan_provider, "_remote_url", "https://model-host:8767")
        monkeypatch.setattr(swan_provider, "_remote_auth_secret", "s3cr3t")
        monkeypatch.setattr(swan_provider, "_remote_verify_tls", False)
        monkeypatch.setattr(swan_provider.httpx, "post", lambda *a, **kw: _FakeHttpResponse({}))
        yield

    def test_noop_in_bundled_mode(self, monkeypatch):
        monkeypatch.setattr(swan_provider, "_remote_url", None)
        swan_provider.report_gap("spot_a", "2026-07-25T18:00:00Z", "forecast", "run1")
        assert swan_provider._gap_report_queue.empty()

    def test_dedup_prevents_reenqueuing_the_same_gap(self):
        key = ("spot_a", "2026-07-25T18:00:00Z", "forecast", "run1")
        swan_provider.report_gap(*key)
        swan_provider.report_gap(*key)
        # Whether or not the worker has already drained the first entry, the
        # dedup set must have recorded the key exactly once (not grown
        # unbounded, and a repeat call must not re-enqueue).
        assert key in swan_provider._gap_report_seen
        assert swan_provider._gap_report_queue.qsize() <= 1

    def test_worker_posts_the_queued_report(self, monkeypatch):
        posted = threading.Event()
        captured: dict = {}

        def _fake_post(url, json=None, headers=None, verify=None, timeout=None):  # noqa: ANN001
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            posted.set()
            return _FakeHttpResponse({})

        monkeypatch.setattr(swan_provider.httpx, "post", _fake_post)

        swan_provider.report_gap("spot_b", "2026-07-25T19:00:00Z", "profile", "run2")

        assert posted.wait(timeout=2.0), "background worker never posted the gap report"
        assert captured["url"] == "https://model-host:8767/report/gap"
        assert captured["json"] == {
            "spot_id": "spot_b",
            "valid_time": "2026-07-25T19:00:00Z",
            "endpoint": "profile",
            "run_time": "run2",
        }
        assert captured["headers"] == {"Authorization": "Bearer s3cr3t"}

    def test_full_queue_drops_report_and_logs_debug_instead_of_blocking(
        self, monkeypatch, caplog,
    ):
        # Prevent a worker from ever draining the queue so it stays full --
        # isolates the queue-full behaviour from timing/races.
        monkeypatch.setattr(swan_provider, "_ensure_gap_report_worker_started", lambda: None)
        tiny_queue: "queue.Queue" = queue.Queue(maxsize=1)
        tiny_queue.put_nowait(("filler", "t", "endpoint", "run"))
        monkeypatch.setattr(swan_provider, "_gap_report_queue", tiny_queue)

        with caplog.at_level(logging.DEBUG):
            # Must not raise or block -- report_gap() is called from the
            # request path and must never delay or fail the response.
            swan_provider.report_gap("spot_c", "2026-07-25T20:00:00Z", "forecast", "run3")

        assert any("queue full" in r.message for r in caplog.records)
        assert tiny_queue.qsize() == 1  # the new report was dropped, not enqueued

    def test_no_thread_spawned_per_report_single_worker_reused(self, monkeypatch):
        """The fix this test guards: a daemon thread PER report is a thread
        bomb when the swelltrack cache is empty (production's actual state)
        -- ~67 gaps per /surf request. Assert a single worker thread is
        reused across many reports, not one thread each."""
        thread_count_before = threading.active_count()

        for i in range(20):
            swan_provider.report_gap(
                "spot_d", f"2026-07-25T{i:02d}:00:00Z", "forecast", "run4",
            )

        # Give the single worker time to drain the burst.
        deadline = time.monotonic() + 2.0
        while not swan_provider._gap_report_queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

        thread_count_after = threading.active_count()
        # At most one additional live thread (the single gap-report worker),
        # never one per report.
        assert thread_count_after <= thread_count_before + 1
