# Expected volume mounts:
#   /etc/weewx-clearskies/          api.conf and optionally secrets.env
#   /etc/weewx/weewx.conf           operator's weewx configuration (read-only)
#   /data/weewx.sdb                 SQLite database when using SQLite backend (read-only)
#   /var/cache/weewx-clearskies/    skyfield ephemeris cache (persistent, writable)

# ── builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

COPY pyproject.toml .
COPY weewx_clearskies_api/ weewx_clearskies_api/

RUN pip install --no-cache-dir .

# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

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
