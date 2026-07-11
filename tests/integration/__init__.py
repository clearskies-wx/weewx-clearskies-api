"""Phase 8 (T8.1) end-to-end marine integration test suite.

Every test module under this package is marked ``@pytest.mark.integration``
and ``@pytest.mark.live_network`` (both registered in ``pyproject.toml``).
These tests call *real* NOAA services (NDBC, CO-OPS, WaveWatch III/ERDDAP,
NWS zones/marine text/SRF) over the live internet — they are intentionally
excluded from the fast unit-test run and from CI
(``.github/workflows/release.yml`` runs
``pytest -m "not integration and not live_network and not benchmark"``).

Run this suite explicitly, from a machine with outbound internet access:

    pytest tests/integration -m "integration and live_network"

Per PROVIDER-MANUAL §11 ("No live-network tests in CI") and the T8.1 task
brief, these are deliberately NOT run as part of this authoring round —
they are exercised during T8.2 deploy/smoke.
"""
