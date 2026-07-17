# Expected volume mounts:
#   /etc/weewx-clearskies/          api.conf and optionally secrets.env
#   /etc/weewx/weewx.conf           operator's weewx configuration (read-only)
#   /data/weewx.sdb                 SQLite database when using SQLite backend (read-only)
#   /var/cache/weewx-clearskies/    skyfield ephemeris cache (persistent, writable)

# ── builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

# ADR-085: eccodes C library for GRIB2 processing (marine GRIB2 data).
# Installed in the builder stage; the shared library is copied to runtime below.
RUN apt-get update && apt-get install -y --no-install-recommends libeccodes-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml .
COPY README.md .
COPY weewx_clearskies_api/ weewx_clearskies_api/

# Install with [marine] extra so eccodes Python binding is included.
RUN pip install --no-cache-dir ".[marine]"

# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# ADR-085: eccodes shared library needed at runtime for GRIB2 processing.
# Only the runtime library — no headers or dev tools.
RUN apt-get update && apt-get install -y --no-install-recommends libeccodes0 \
    && rm -rf /var/lib/apt/lists/*

# Copy only the installed package artifacts; leave build tools behind.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/weewx-clearskies-api /usr/local/bin/weewx-clearskies-api

# System user — no home directory, no login shell, fixed UID for bind-mount
# permission alignment on the host side.
RUN useradd --system --uid 1000 --no-create-home --shell /usr/sbin/nologin clearskies

USER clearskies

# Health port (8081) binds loopback per ADR-030 and is not reachable from
# outside the container, so it is intentionally not exposed here.
EXPOSE 8765

# urllib.request is stdlib — no extra deps, no curl/wget required in the image.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/ready')"

ENTRYPOINT ["python", "-m", "weewx_clearskies_api"]
