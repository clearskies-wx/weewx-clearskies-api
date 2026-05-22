"""Tests for the Prometheus metrics implementation per ADR-031.

Coverage areas:
  1. /metrics endpoint on the health app (enabled / disabled)
  2. HealthSettings.metrics_enabled (env var + INI precedence)
  3. HTTP metrics middleware (http_requests_total, http_request_duration_seconds)
  4. Cache metrics for MemoryCache and RedisCache (hits, misses, expired)
  5. Provider HTTP metrics (success, 401 KeyInvalid, 429 QuotaExhausted)
  6. wire_db_metrics — DB query duration histogram with endpoint label

Design note — global prometheus_client registry:
  prometheus_client metrics are global singletons that accumulate across the
  entire test session.  Asserting absolute values (e.g. == 1.0) would make
  tests order-dependent.  Instead every counter/histogram test records a
  BEFORE value and asserts that AFTER > BEFORE (delta > 0).  This is the
  correct pattern for global-registry metrics in pytest sessions.
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import fakeredis
import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import Column, Float, Integer, MetaData, Table, create_engine, text
from sqlalchemy.pool import StaticPool

from weewx_clearskies_api.health import create_health_app
from weewx_clearskies_api.metrics import current_endpoint, wire_db_metrics

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample(name: str, labels: dict[str, str]) -> float:
    """Return a metric sample value or 0.0 when the label combination has
    never been observed (prometheus_client returns None in that case)."""
    val = REGISTRY.get_sample_value(name, labels)
    return val if val is not None else 0.0


# ---------------------------------------------------------------------------
# 1. /metrics endpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Health-app /metrics endpoint behaviour (ADR-031)."""

    def test_metrics_enabled_returns_200_text_plain(self) -> None:
        """/metrics returns 200 with text/plain content-type when enabled."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        response = client.get("/metrics")
        assert response.status_code == 200
        # Content-Type from prometheus_client is text/plain; version and charset vary.
        assert response.headers["content-type"].startswith("text/plain")

    def test_metrics_disabled_returns_404(self) -> None:
        """/metrics returns 404 when metrics_enabled=False (the default)."""
        client = TestClient(create_health_app(metrics_enabled=False), raise_server_exceptions=False)
        response = client.get("/metrics")
        assert response.status_code == 404

    def test_metrics_default_returns_404(self) -> None:
        """/metrics is absent on the health app created with default arguments."""
        client = TestClient(create_health_app(), raise_server_exceptions=False)
        response = client.get("/metrics")
        assert response.status_code == 404

    def test_metrics_response_is_prometheus_exposition_format(self) -> None:
        """/metrics body follows Prometheus text exposition format (# HELP / # TYPE lines)."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "# HELP" in body
        assert "# TYPE" in body

    def test_metrics_response_contains_http_requests_total(self) -> None:
        """/metrics body includes http_requests_total metric name."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "http_requests_total" in body

    def test_metrics_response_contains_provider_calls_total(self) -> None:
        """/metrics body includes provider_calls_total metric name."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "provider_calls_total" in body

    def test_metrics_response_contains_cache_hits_total(self) -> None:
        """/metrics body includes cache_hits_total metric name."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "cache_hits_total" in body

    def test_metrics_response_contains_db_query_duration_seconds(self) -> None:
        """/metrics body includes db_query_duration_seconds metric name."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "db_query_duration_seconds" in body

    def test_metrics_response_contains_http_request_duration_seconds(self) -> None:
        """/metrics body includes http_request_duration_seconds metric name."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "http_request_duration_seconds" in body

    def test_metrics_response_contains_cache_misses_total(self) -> None:
        """/metrics body includes cache_misses_total metric name."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "cache_misses_total" in body

    def test_metrics_response_contains_provider_call_duration_seconds(self) -> None:
        """/metrics body includes provider_call_duration_seconds metric name."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        body = client.get("/metrics").text
        assert "provider_call_duration_seconds" in body

    def test_health_live_still_works_when_metrics_enabled(self) -> None:
        """/health/live is unaffected when metrics endpoint is active."""
        client = TestClient(create_health_app(metrics_enabled=True), raise_server_exceptions=False)
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# 2. HealthSettings.metrics_enabled
# ---------------------------------------------------------------------------


class TestHealthSettingsMetricsEnabled:
    """HealthSettings.metrics_enabled respects env var and INI precedence."""

    @pytest.fixture(autouse=True)
    def _clean_env(self) -> None:
        """Remove CLEARSKIES_METRICS_ENABLED before and after each test."""
        os.environ.pop("CLEARSKIES_METRICS_ENABLED", None)
        yield  # type: ignore[misc]
        os.environ.pop("CLEARSKIES_METRICS_ENABLED", None)

    def test_default_is_false_when_env_unset_and_ini_empty(self) -> None:
        """metrics_enabled defaults to False when nothing is configured."""
        from weewx_clearskies_api.config.settings import HealthSettings

        settings = HealthSettings({})
        assert settings.metrics_enabled is False

    def test_env_true_enables_metrics(self) -> None:
        """CLEARSKIES_METRICS_ENABLED=true → metrics_enabled is True."""
        from weewx_clearskies_api.config.settings import HealthSettings

        os.environ["CLEARSKIES_METRICS_ENABLED"] = "true"
        settings = HealthSettings({})
        assert settings.metrics_enabled is True

    def test_env_false_disables_metrics(self) -> None:
        """CLEARSKIES_METRICS_ENABLED=false → metrics_enabled is False."""
        from weewx_clearskies_api.config.settings import HealthSettings

        os.environ["CLEARSKIES_METRICS_ENABLED"] = "false"
        settings = HealthSettings({})
        assert settings.metrics_enabled is False

    def test_env_one_enables_metrics(self) -> None:
        """CLEARSKIES_METRICS_ENABLED=1 → metrics_enabled is True."""
        from weewx_clearskies_api.config.settings import HealthSettings

        os.environ["CLEARSKIES_METRICS_ENABLED"] = "1"
        settings = HealthSettings({})
        assert settings.metrics_enabled is True

    def test_env_zero_disables_metrics(self) -> None:
        """CLEARSKIES_METRICS_ENABLED=0 → metrics_enabled is False."""
        from weewx_clearskies_api.config.settings import HealthSettings

        os.environ["CLEARSKIES_METRICS_ENABLED"] = "0"
        settings = HealthSettings({})
        assert settings.metrics_enabled is False

    def test_ini_true_enables_metrics_when_env_unset(self) -> None:
        """INI metrics_enabled = true is honoured when env var is absent."""
        from weewx_clearskies_api.config.settings import HealthSettings

        settings = HealthSettings({"metrics_enabled": "true"})
        assert settings.metrics_enabled is True

    def test_ini_one_enables_metrics_when_env_unset(self) -> None:
        """INI metrics_enabled = 1 is honoured when env var is absent."""
        from weewx_clearskies_api.config.settings import HealthSettings

        settings = HealthSettings({"metrics_enabled": "1"})
        assert settings.metrics_enabled is True

    def test_env_wins_over_ini_true_vs_false(self) -> None:
        """Env var CLEARSKIES_METRICS_ENABLED=false overrides INI metrics_enabled=true."""
        from weewx_clearskies_api.config.settings import HealthSettings

        os.environ["CLEARSKIES_METRICS_ENABLED"] = "false"
        settings = HealthSettings({"metrics_enabled": "true"})
        assert settings.metrics_enabled is False

    def test_env_wins_over_ini_false_vs_true(self) -> None:
        """Env var CLEARSKIES_METRICS_ENABLED=true overrides INI metrics_enabled=false."""
        from weewx_clearskies_api.config.settings import HealthSettings

        os.environ["CLEARSKIES_METRICS_ENABLED"] = "true"
        settings = HealthSettings({"metrics_enabled": "false"})
        assert settings.metrics_enabled is True


# ---------------------------------------------------------------------------
# 3. HTTP metrics middleware
# ---------------------------------------------------------------------------


def _make_metrics_app() -> FastAPI:
    """Minimal FastAPI app with MetricsMiddleware for middleware unit tests."""
    from weewx_clearskies_api.middleware.metrics import MetricsMiddleware

    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get("/api/v1/current")
    async def _current() -> JSONResponse:
        return JSONResponse({"temperature": 22.4})

    @app.get("/api/v1/archive/{year}")
    async def _archive(year: int) -> JSONResponse:
        return JSONResponse({"year": year})

    return app


class TestHTTPMetricsMiddleware:
    """MetricsMiddleware increments http_requests_total and records durations."""

    def test_successful_request_increments_http_requests_total(self) -> None:
        """A GET /api/v1/current request increments http_requests_total{method,endpoint,status}."""
        app = _make_metrics_app()
        client = TestClient(app, raise_server_exceptions=False)

        labels = {"method": "GET", "endpoint": "/api/v1/current", "status": "200"}
        before = _sample("http_requests_total", labels)

        resp = client.get("/api/v1/current")
        assert resp.status_code == 200

        after = _sample("http_requests_total", labels)
        assert after > before

    def test_successful_request_observes_http_request_duration_seconds(self) -> None:
        """A request observes at least one sample in http_request_duration_seconds."""
        app = _make_metrics_app()
        client = TestClient(app, raise_server_exceptions=False)

        labels = {"method": "GET", "endpoint": "/api/v1/current"}
        before = _sample("http_request_duration_seconds_count", labels)

        client.get("/api/v1/current")

        after = _sample("http_request_duration_seconds_count", labels)
        assert after > before

    def test_endpoint_label_uses_route_template_not_concrete_url(self) -> None:
        """Endpoint label uses /api/v1/archive/{year} template, not the concrete URL."""
        app = _make_metrics_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Template label — should be populated.
        template_labels = {"method": "GET", "endpoint": "/api/v1/archive/{year}", "status": "200"}
        # Concrete URL label — should NOT be populated.
        concrete_labels = {"method": "GET", "endpoint": "/api/v1/archive/2024", "status": "200"}

        before_template = _sample("http_requests_total", template_labels)
        before_concrete = _sample("http_requests_total", concrete_labels)

        client.get("/api/v1/archive/2024")

        after_template = _sample("http_requests_total", template_labels)
        after_concrete = _sample("http_requests_total", concrete_labels)

        assert after_template > before_template, "Template label must be incremented"
        assert after_concrete == before_concrete, "Concrete URL must not be used as label"

    def test_unmatched_path_uses_sentinel_label(self) -> None:
        """Requests to unmatched paths use '<unmatched>' sentinel, not the raw URL (ADR-031 cardinality)."""
        app = _make_metrics_app()
        client = TestClient(app, raise_server_exceptions=False)

        sentinel_labels = {"method": "GET", "endpoint": "<unmatched>", "status": "404"}
        raw_labels = {"method": "GET", "endpoint": "/wp-admin/evil-scanner-path", "status": "404"}

        before_sentinel = _sample("http_requests_total", sentinel_labels)
        before_raw = _sample("http_requests_total", raw_labels)

        client.get("/wp-admin/evil-scanner-path")

        after_sentinel = _sample("http_requests_total", sentinel_labels)
        after_raw = _sample("http_requests_total", raw_labels)

        assert after_sentinel > before_sentinel, "<unmatched> sentinel must be used for 404 paths"
        assert after_raw == before_raw, "Raw scanner URL must never appear as a label value"

    def test_method_label_is_get(self) -> None:
        """The method label is GET for a GET request."""
        app = _make_metrics_app()
        client = TestClient(app, raise_server_exceptions=False)

        get_labels = {"method": "GET", "endpoint": "/api/v1/current", "status": "200"}
        post_labels = {"method": "POST", "endpoint": "/api/v1/current", "status": "200"}

        before_get = _sample("http_requests_total", get_labels)
        before_post = _sample("http_requests_total", post_labels)

        client.get("/api/v1/current")

        after_get = _sample("http_requests_total", get_labels)
        after_post = _sample("http_requests_total", post_labels)

        assert after_get > before_get
        assert after_post == before_post

    def test_status_label_matches_response_status_code(self) -> None:
        """The status label records the actual HTTP response status code string."""
        from weewx_clearskies_api.middleware.metrics import MetricsMiddleware

        app = FastAPI()
        app.add_middleware(MetricsMiddleware)

        @app.get("/api/v1/not_found_route")
        async def _not_found() -> JSONResponse:
            return JSONResponse({"error": "not found"}, status_code=404)

        client = TestClient(app, raise_server_exceptions=False)

        labels_404 = {"method": "GET", "endpoint": "/api/v1/not_found_route", "status": "404"}
        before = _sample("http_requests_total", labels_404)

        client.get("/api/v1/not_found_route")

        after = _sample("http_requests_total", labels_404)
        assert after > before

    def test_multiple_requests_each_increment_counter(self) -> None:
        """Three requests to the same endpoint produce three counter increments."""
        app = _make_metrics_app()
        client = TestClient(app, raise_server_exceptions=False)

        labels = {"method": "GET", "endpoint": "/api/v1/current", "status": "200"}
        before = _sample("http_requests_total", labels)

        for _ in range(3):
            client.get("/api/v1/current")

        after = _sample("http_requests_total", labels)
        assert after - before >= 3.0

    def test_duration_sum_is_positive_after_request(self) -> None:
        """http_request_duration_seconds_sum is positive after a request (real I/O took time)."""
        app = _make_metrics_app()
        client = TestClient(app, raise_server_exceptions=False)

        labels = {"method": "GET", "endpoint": "/api/v1/current"}
        before_sum = _sample("http_request_duration_seconds_sum", labels)

        client.get("/api/v1/current")

        after_sum = _sample("http_request_duration_seconds_sum", labels)
        assert after_sum > before_sum


# ---------------------------------------------------------------------------
# 4. Cache metrics
# ---------------------------------------------------------------------------


class TestMemoryCacheMetrics:
    """MemoryCache.get() increments cache_hits_total / cache_misses_total correctly."""

    def test_memory_cache_miss_increments_misses_total(self) -> None:
        """MemoryCache.get() on a cold key increments cache_misses_total{backend=memory}."""
        from weewx_clearskies_api.providers._common.cache import MemoryCache

        cache = MemoryCache()
        labels = {"backend": "memory"}
        before = _sample("cache_misses_total", labels)

        result = cache.get("cold-key-12345")

        assert result is None
        after = _sample("cache_misses_total", labels)
        assert after > before

    def test_memory_cache_hit_increments_hits_total(self) -> None:
        """MemoryCache.get() on a warm key increments cache_hits_total{backend=memory}."""
        from weewx_clearskies_api.providers._common.cache import MemoryCache

        cache = MemoryCache()
        cache.set("warm-key", {"temperature": 22.5, "humidity": 65}, ttl_seconds=300)

        labels = {"backend": "memory"}
        before = _sample("cache_hits_total", labels)

        result = cache.get("warm-key")

        assert result == {"temperature": 22.5, "humidity": 65}
        after = _sample("cache_hits_total", labels)
        assert after > before

    def test_memory_cache_expired_entry_increments_misses_total(self) -> None:
        """MemoryCache.get() on an expired entry increments cache_misses_total{backend=memory}."""
        from weewx_clearskies_api.providers._common.cache import MemoryCache

        cache = MemoryCache()
        # Directly inject an entry whose expires_at is already in the past.
        key = "expired-entry-key"
        cache._cache[key] = ({"stale": True}, time.monotonic() - 1.0)  # expired 1s ago

        labels = {"backend": "memory"}
        before = _sample("cache_misses_total", labels)

        result = cache.get(key)

        assert result is None
        after = _sample("cache_misses_total", labels)
        assert after > before

    def test_memory_cache_set_then_get_returns_correct_value(self) -> None:
        """set() followed by get() within TTL returns the stored value (not a proxy for metrics)."""
        from weewx_clearskies_api.providers._common.cache import MemoryCache

        cache = MemoryCache()
        payload = {
            "alerts": [
                {
                    "event": "Severe Thunderstorm Warning",
                    "severity": "Severe",
                    "urgency": "Immediate",
                    "areas": ["Hampden County", "Hampshire County"],
                    "onset": "2026-05-22T14:00:00Z",
                    "expires": "2026-05-22T17:30:00Z",
                }
            ]
        }
        cache.set("alerts-key", payload, ttl_seconds=300)
        result = cache.get("alerts-key")
        assert result == payload


class TestRedisCacheMetrics:
    """RedisCache.get() increments cache_hits_total / cache_misses_total{backend=redis}.

    Uses fakeredis via unittest.mock.patch to avoid requiring a real Redis server.
    The patch replaces redis.Redis.from_url with a FakeRedis instance so that
    RedisCache.__init__'s ping() call succeeds and the _client attribute is a
    FakeRedis object that behaves identically to real redis-py.
    """

    def _make_redis_cache(self) -> object:
        """Build a RedisCache backed by fakeredis."""
        from weewx_clearskies_api.providers._common.cache import RedisCache

        fake_server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=fake_server)
        with patch("redis.Redis.from_url", return_value=fake_client):
            cache = RedisCache(url="redis://localhost:6379/0")
        return cache

    def test_redis_cache_miss_increments_misses_total(self) -> None:
        """RedisCache.get() on an absent key increments cache_misses_total{backend=redis}."""
        cache = self._make_redis_cache()
        labels = {"backend": "redis"}
        before = _sample("cache_misses_total", labels)

        result = cache.get("redis-cold-key-99999")  # type: ignore[union-attr]

        assert result is None
        after = _sample("cache_misses_total", labels)
        assert after > before

    def test_redis_cache_hit_increments_hits_total(self) -> None:
        """RedisCache.get() on a present key increments cache_hits_total{backend=redis}."""
        cache = self._make_redis_cache()

        # Populate the cache with a realistic forecast payload.
        forecast_payload = {
            "daily": [
                {
                    "date": "2026-05-22",
                    "high": 24.2,
                    "low": 14.1,
                    "conditions": "Partly Cloudy",
                    "pop": 20,
                }
            ]
        }
        cache.set("redis-warm-key", forecast_payload, ttl_seconds=600)  # type: ignore[union-attr]

        labels = {"backend": "redis"}
        before = _sample("cache_hits_total", labels)

        result = cache.get("redis-warm-key")  # type: ignore[union-attr]

        assert result == forecast_payload
        after = _sample("cache_hits_total", labels)
        assert after > before


# ---------------------------------------------------------------------------
# 5. Provider HTTP metrics
# ---------------------------------------------------------------------------


class TestProviderHTTPMetrics:
    """ProviderHTTPClient increments provider_calls_total and observes durations."""

    def _make_client(
        self,
        provider_id: str = "open_meteo",
        domain: str = "forecast",
    ) -> object:
        """Build a ProviderHTTPClient with no retries for deterministic tests."""
        from weewx_clearskies_api.providers._common.http import ProviderHTTPClient

        return ProviderHTTPClient(
            provider_id=provider_id,
            domain=domain,
            user_agent="weewx-clearskies-api/test",
            max_retries=0,
        )

    def test_successful_200_increments_cache_miss_success(self) -> None:
        """A 200 response increments provider_calls_total{outcome=cache_miss_success}."""
        client = self._make_client(provider_id="open_meteo", domain="forecast")
        labels = {
            "provider_id": "open_meteo",
            "domain": "forecast",
            "outcome": "cache_miss_success",
        }
        before = _sample("provider_calls_total", labels)

        with respx.mock:
            respx.get("https://api.open-meteo.com/v1/forecast").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "latitude": 42.375,
                        "longitude": -72.519,
                        "timezone": "America/New_York",
                        "daily": {"time": ["2026-05-22"], "temperature_2m_max": [24.2]},
                    },
                )
            )
            resp = client.get("https://api.open-meteo.com/v1/forecast")  # type: ignore[union-attr]

        assert resp.status_code == 200
        after = _sample("provider_calls_total", labels)
        assert after > before

    def test_successful_200_observes_provider_call_duration(self) -> None:
        """A 200 response adds an observation to provider_call_duration_seconds."""
        client = self._make_client(provider_id="open_meteo_dur", domain="forecast")
        count_labels = {"provider_id": "open_meteo_dur", "domain": "forecast"}
        before = _sample("provider_call_duration_seconds_count", count_labels)

        with respx.mock:
            respx.get("https://api.open-meteo.com/v1/current").mock(
                return_value=httpx.Response(200, json={"current": {"temperature_2m": 22.0}})
            )
            client.get("https://api.open-meteo.com/v1/current")  # type: ignore[union-attr]

        after = _sample("provider_call_duration_seconds_count", count_labels)
        assert after > before

    def test_401_response_increments_cache_miss_failure(self) -> None:
        """A 401 (KeyInvalid) increments provider_calls_total{outcome=cache_miss_failure}."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid

        client = self._make_client(provider_id="aeris_forecast", domain="forecast")
        labels = {
            "provider_id": "aeris_forecast",
            "domain": "forecast",
            "outcome": "cache_miss_failure",
        }
        before = _sample("provider_calls_total", labels)

        with respx.mock:
            respx.get("https://api.aerisapi.com/forecasts").mock(
                return_value=httpx.Response(
                    401,
                    json={"success": False, "error": {"code": "invalid_client"}},
                )
            )
            with pytest.raises(KeyInvalid):
                client.get("https://api.aerisapi.com/forecasts")  # type: ignore[union-attr]

        after = _sample("provider_calls_total", labels)
        assert after > before

    def test_403_response_increments_cache_miss_failure(self) -> None:
        """A 403 (KeyInvalid) increments provider_calls_total{outcome=cache_miss_failure}."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid

        client = self._make_client(provider_id="aeris_forecast_403", domain="forecast")
        labels = {
            "provider_id": "aeris_forecast_403",
            "domain": "forecast",
            "outcome": "cache_miss_failure",
        }
        before = _sample("provider_calls_total", labels)

        with respx.mock:
            respx.get("https://api.aerisapi.com/forecast_403").mock(
                return_value=httpx.Response(403, json={"success": False})
            )
            with pytest.raises(KeyInvalid):
                client.get("https://api.aerisapi.com/forecast_403")  # type: ignore[union-attr]

        after = _sample("provider_calls_total", labels)
        assert after > before

    def test_429_response_increments_cache_miss_failure(self) -> None:
        """A 429 (QuotaExhausted) increments provider_calls_total{outcome=cache_miss_failure}."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted

        client = self._make_client(provider_id="nws_alerts", domain="alerts")
        labels = {
            "provider_id": "nws_alerts",
            "domain": "alerts",
            "outcome": "cache_miss_failure",
        }
        before = _sample("provider_calls_total", labels)

        with respx.mock:
            respx.get("https://api.weather.gov/alerts").mock(
                return_value=httpx.Response(
                    429,
                    headers={"Retry-After": "60"},
                    json={"title": "Too Many Requests"},
                )
            )
            with pytest.raises(QuotaExhausted):
                client.get("https://api.weather.gov/alerts")  # type: ignore[union-attr]

        after = _sample("provider_calls_total", labels)
        assert after > before

    def test_success_does_not_increment_failure_counter(self) -> None:
        """A 200 response does NOT increment cache_miss_failure."""
        client = self._make_client(provider_id="open_meteo_clean", domain="aqi")
        failure_labels = {
            "provider_id": "open_meteo_clean",
            "domain": "aqi",
            "outcome": "cache_miss_failure",
        }
        before_fail = _sample("provider_calls_total", failure_labels)

        with respx.mock:
            respx.get("https://air-quality-api.open-meteo.com/v1/air-quality").mock(
                return_value=httpx.Response(200, json={"hourly": {}})
            )
            client.get("https://air-quality-api.open-meteo.com/v1/air-quality")  # type: ignore[union-attr]

        after_fail = _sample("provider_calls_total", failure_labels)
        assert after_fail == before_fail, "Failure counter must not increment on success"


# ---------------------------------------------------------------------------
# 6. wire_db_metrics
# ---------------------------------------------------------------------------


class TestWireDbMetrics:
    """wire_db_metrics attaches SQLAlchemy event listeners that record query durations."""

    def _make_engine_with_metrics(self) -> object:
        """Create an in-memory SQLite engine with wire_db_metrics attached."""
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        meta = MetaData()
        Table(
            "archive",
            meta,
            Column("dateTime", Integer, primary_key=True),
            Column("usUnits", Integer, nullable=False),
            Column("interval", Integer, nullable=False),
            Column("outTemp", Float, nullable=True),
        )
        meta.create_all(engine)
        wire_db_metrics(engine)
        return engine

    def test_query_execution_observes_db_query_duration(self) -> None:
        """Executing a query after wire_db_metrics adds a count to db_query_duration_seconds."""
        engine = self._make_engine_with_metrics()
        current_endpoint.set("/api/v1/archive")

        before = _sample("db_query_duration_seconds_count", {"endpoint": "/api/v1/archive"})

        with engine.connect() as conn:  # type: ignore[union-attr]
            conn.execute(text("SELECT 1"))

        after = _sample("db_query_duration_seconds_count", {"endpoint": "/api/v1/archive"})
        assert after > before

    def test_db_metrics_endpoint_label_from_context_var(self) -> None:
        """db_query_duration_seconds endpoint label comes from the current_endpoint ContextVar."""
        engine = self._make_engine_with_metrics()

        # Set a distinctive endpoint label.
        current_endpoint.set("/api/v1/current")
        before_current = _sample("db_query_duration_seconds_count", {"endpoint": "/api/v1/current"})
        # A different endpoint should not be incremented.
        before_other = _sample(
            "db_query_duration_seconds_count", {"endpoint": "/api/v1/records"}
        )

        with engine.connect() as conn:  # type: ignore[union-attr]
            conn.execute(text("SELECT 42"))

        after_current = _sample("db_query_duration_seconds_count", {"endpoint": "/api/v1/current"})
        after_other = _sample(
            "db_query_duration_seconds_count", {"endpoint": "/api/v1/records"}
        )

        assert after_current > before_current, "Expected /api/v1/current label to increment"
        assert after_other == before_other, "/api/v1/records label must not increment"

    def test_db_metrics_default_endpoint_label_is_unknown(self) -> None:
        """When ContextVar has not been set, the endpoint label falls back to 'unknown'."""
        engine = self._make_engine_with_metrics()

        # Explicitly reset the ContextVar to its default.
        current_endpoint.set("unknown")
        before = _sample("db_query_duration_seconds_count", {"endpoint": "unknown"})

        with engine.connect() as conn:  # type: ignore[union-attr]
            conn.execute(text("SELECT 99"))

        after = _sample("db_query_duration_seconds_count", {"endpoint": "unknown"})
        assert after > before

    def test_db_duration_sum_is_positive_after_query(self) -> None:
        """db_query_duration_seconds_sum grows after a query (real I/O took measurable time)."""
        engine = self._make_engine_with_metrics()
        current_endpoint.set("/api/v1/archive_sum_check")

        before_sum = _sample(
            "db_query_duration_seconds_sum", {"endpoint": "/api/v1/archive_sum_check"}
        )

        with engine.connect() as conn:  # type: ignore[union-attr]
            conn.execute(text("SELECT dateTime FROM archive LIMIT 10"))

        after_sum = _sample(
            "db_query_duration_seconds_sum", {"endpoint": "/api/v1/archive_sum_check"}
        )
        # Even a trivial SQLite query has non-zero wall-clock time.
        assert after_sum > before_sum

    def test_multiple_queries_accumulate_in_db_duration_count(self) -> None:
        """Three queries produce three count increments in db_query_duration_seconds."""
        engine = self._make_engine_with_metrics()
        current_endpoint.set("/api/v1/multi_query_test")

        before = _sample(
            "db_query_duration_seconds_count", {"endpoint": "/api/v1/multi_query_test"}
        )

        with engine.connect() as conn:  # type: ignore[union-attr]
            for _ in range(3):
                conn.execute(text("SELECT 1"))

        after = _sample(
            "db_query_duration_seconds_count", {"endpoint": "/api/v1/multi_query_test"}
        )
        assert after - before >= 3.0
