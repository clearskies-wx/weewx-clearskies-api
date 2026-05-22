"""Endpoint latency benchmarks per ADR-033 performance budget.

Run with::

    pytest tests/benchmarks/ -m benchmark --benchmark-only

Do NOT run in CI — these require weather-dev with a real weewx database.
The benchmark marker excludes them from the default ``pytest`` invocation.

Latency targets (p95, from ADR-033 §API latency targets):
  - Archive read (current/today/recent): < 100 ms
  - Archive aggregation (charts):        < 500 ms
  - Provider response — cache hit:       < 50 ms

These are aspirational targets, not release gates (ADR-033 §Targets, not
gates).  pytest-benchmark reports stats (min/max/mean/stddev/p95) but does
not fail the run based on timing values.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.benchmark
class TestArchiveReadBenchmark:
    """Archive read endpoints — ADR-033 target: p95 < 100 ms."""

    def test_current_endpoint(self, benchmark: pytest.FixtureRequest, benchmark_client: TestClient) -> None:
        """Benchmark GET /api/v1/current.

        Measures the full HTTP round-trip through the FastAPI app, including
        middleware chain and DB query for the most-recent archive row.
        """
        benchmark(benchmark_client.get, "/api/v1/current")

    def test_archive_recent(self, benchmark: pytest.FixtureRequest, benchmark_client: TestClient) -> None:
        """Benchmark GET /api/v1/archive (default limit).

        Measures paged archive reads with the default query window.
        """
        benchmark(benchmark_client.get, "/api/v1/archive")


@pytest.mark.benchmark
class TestArchiveAggregationBenchmark:
    """Archive aggregation endpoints — ADR-033 target: p95 < 500 ms."""

    def test_charts_day(self, benchmark: pytest.FixtureRequest, benchmark_client: TestClient) -> None:
        """Benchmark GET /api/v1/charts/day.

        Measures chart aggregation over a 24-hour window, the most common
        client request and the widest time-range aggregation the Now page uses.
        """
        benchmark(benchmark_client.get, "/api/v1/charts/day")


@pytest.mark.benchmark
class TestProviderCacheHitBenchmark:
    """Provider response — cache hit — ADR-033 target: p95 < 50 ms."""

    def test_forecast_cache_hit(self, benchmark: pytest.FixtureRequest, benchmark_client: TestClient) -> None:
        """Benchmark GET /api/v1/forecast after cache warm.

        The first call populates the provider cache; subsequent calls
        (what benchmark() measures in its loop) exercise the cache-hit path
        only, isolating in-process serialisation latency from upstream I/O.
        """
        # Warm the cache before the measurement loop starts.
        benchmark_client.get("/api/v1/forecast")
        # Measure cache-hit round-trips.
        benchmark(benchmark_client.get, "/api/v1/forecast")
