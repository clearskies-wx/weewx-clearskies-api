# Installation — weewx-clearskies-api

This document covers installing and running clearskies-api. For the complete stack (API + realtime service + dashboard + reverse proxy), see [weewx-clearskies-stack](https://github.com/inguy24/weewx-clearskies-stack).

---

## Supported environments

| Environment | Recommended install path | Notes |
|---|---|---|
| Debian / Ubuntu (native) | pip + systemd | Recommended for Linux operators already running weewx natively |
| Raspberry Pi OS | pip + systemd | Same as Debian/Ubuntu; Pi OS is Debian-based |
| LXD container (Ubuntu 24.04) | pip + systemd | Supported; used in development |
| Docker / docker-compose | docker-compose via stack repo | Simplest path; reverse proxy included |
| Proxmox VM (Ubuntu 24.04 guest) | pip + systemd or Docker | Same as native Ubuntu |
| macOS | pip | `launchd` service management on macOS; Docker Desktop also works |
| Windows | Docker Desktop with stack repo | Native Python install on Windows is unsupported |

---

## System requirements

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.12 | Earlier versions are not supported |
| pip | 23+ | Bundled with Python 3.12 |
| weewx | 5.x | Archive database must exist; one archive record required before starting |
| Database | SQLite or MariaDB 10.6+ | Matches weewx's own database support |
| OS | Linux (Debian/Ubuntu/RHEL/Pi OS) or macOS | Windows: use Docker |

---

## Native install (pip + systemd)

### 1. Create a read-only database user

clearskies-api enforces a read-only database user at startup and refuses to start if the user has write access.

**SQLite** — the weewx `.sdb` file is used read-only. No separate user needed; ensure the `clearskies-api` process user can read the file.

**MariaDB/MySQL** — create a dedicated SELECT-only user:

```sql
-- Run as the MariaDB root user
CREATE USER IF NOT EXISTS 'clearskies_ro'@'127.0.0.1' IDENTIFIED BY '<password>';
GRANT SELECT ON weewx.* TO 'clearskies_ro'@'127.0.0.1';

-- Also grant from IPv6 loopback
CREATE USER IF NOT EXISTS 'clearskies_ro'@'::1' IDENTIFIED BY '<password>';
GRANT SELECT ON weewx.* TO 'clearskies_ro'@'::1';

FLUSH PRIVILEGES;
```

Replace `weewx` with your database name and choose a strong password.

### 2. Install the package

```bash
pip install weewx-clearskies-api
```

Or, to install into a virtual environment (recommended):

```bash
python3.12 -m venv /opt/weewx-clearskies/venv
/opt/weewx-clearskies/venv/bin/pip install weewx-clearskies-api
```

### 3. Create the configuration directory

```bash
sudo mkdir -p /etc/weewx-clearskies
sudo mkdir -p /var/cache/weewx-clearskies/skyfield
```

### 4. Create the config file

```bash
# Find the example config bundled with the package
python3 -c "import weewx_clearskies_api; import pathlib; \
  print(pathlib.Path(weewx_clearskies_api.__file__).parent.parent / 'etc/api.conf.example')"

# Copy and edit
sudo cp <path-from-above> /etc/weewx-clearskies/api.conf
sudo $EDITOR /etc/weewx-clearskies/api.conf
```

Alternatively, the example is also available in the repository at `etc/api.conf.example`.

See [CONFIG.md](CONFIG.md) for a full description of every option.

### 5. Create the secrets file

Credentials must **not** go in `api.conf`. Place them in a mode-0600 file:

```bash
sudo tee /etc/weewx-clearskies/secrets.env <<'EOF'
WEEWX_CLEARSKIES_DB_USER=clearskies_ro
WEEWX_CLEARSKIES_DB_PASSWORD=<your-db-password>
EOF
sudo chmod 0600 /etc/weewx-clearskies/secrets.env
```

For cross-host deploys (API on a different host than the dashboard), also add:

```bash
WEEWX_CLEARSKIES_PROXY_SECRET=$(openssl rand -hex 32)
```

See [SECURITY.md](SECURITY.md) for details on the proxy secret.

### 6. Configure the reverse proxy

clearskies-api binds to `127.0.0.1:8765` by default. A reverse proxy must forward requests to it.

**Nginx example:**

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name weather.example.com;

    location /api/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

For TLS, add a certbot certificate or switch to Caddy (see stack repo for a complete Caddy example).

**Apache example:**

```apache
<VirtualHost *:80>
    ServerName weather.example.com

    ProxyPass /api/ http://127.0.0.1:8765/api/
    ProxyPassReverse /api/ http://127.0.0.1:8765/api/
    ProxyPreserveHost On
</VirtualHost>
```

Enable required modules: `a2enmod proxy proxy_http`.

### 7. Install the systemd unit

```bash
sudo tee /etc/systemd/system/weewx-clearskies-api.service <<'EOF'
[Unit]
Description=weewx-clearskies-api
After=network.target

[Service]
Type=simple
User=weewx
Group=weewx
EnvironmentFile=/etc/weewx-clearskies/secrets.env
ExecStart=/usr/local/bin/weewx-clearskies-api
Restart=on-failure
RestartSec=5
# Harden the process — see SECURITY.md
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/cache/weewx-clearskies
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable weewx-clearskies-api
sudo systemctl start weewx-clearskies-api
sudo systemctl status weewx-clearskies-api
```

Adjust `User=` and `Group=` to match the account under which you run weewx. The `EnvironmentFile` line loads `secrets.env` into the service environment before the process starts.

If using a virtual environment, change `ExecStart` to:

```
ExecStart=/opt/weewx-clearskies/venv/bin/weewx-clearskies-api
```

### 8. Verify

```bash
# Health endpoint (loopback only)
curl http://127.0.0.1:8081/health/live
# Expected: {"status": "ok"}

curl http://127.0.0.1:8081/health/ready
# Expected: {"status": "ok"} once DB is connected

# API endpoint (via reverse proxy)
curl http://weather.example.com/api/v1/station
# Expected: JSON object with station name, lat/lon, timezone

# Swagger UI
# Open http://weather.example.com/api/v1/docs in a browser
```

---

## Docker Compose (full stack)

The easiest path is the stack repo, which ships a pre-configured `docker-compose.yaml` including clearskies-api, clearskies-realtime, the dashboard, and a Caddy reverse proxy:

```
https://github.com/inguy24/weewx-clearskies-stack
```

To run only clearskies-api in Docker without the full stack, a Docker image is published at `ghcr.io/inguy24/weewx-clearskies-api`. The image follows the same configuration conventions as the native install.

---

## Updating

**Native (pip):**

```bash
pip install -U weewx-clearskies-api
sudo systemctl restart weewx-clearskies-api
```

Configuration at `/etc/weewx-clearskies/api.conf` is outside the Python package and is preserved automatically.

**Docker:**

```bash
docker compose pull
docker compose up -d
```

The bind-mounted `/etc/weewx-clearskies/` directory is preserved across image updates.

Read [CHANGELOG.md](CHANGELOG.md) before upgrading. It documents any manual steps required, config-file migrations, and breaking changes.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Service exits immediately with `FATAL: No configuration file found` | `api.conf` not at `/etc/weewx-clearskies/api.conf` | Create or copy the example config |
| `FATAL: write-probe succeeded` at startup | DB user has write access | Grant SELECT-only as shown in step 1 |
| `FATAL: Schema reflection failed` | weewx has never run, or wrong `[database]` settings | Run weewx at least once; check path/host/name in `api.conf` |
| `FATAL: config key ... looks like a secret` | A credential was placed in `api.conf` | Move it to `secrets.env` |
| `/health/ready` returns `{"status": "degraded"}` | DB connection not yet established | Check DB credentials and connectivity |
| API returns 401 | `WEEWX_CLEARSKIES_PROXY_SECRET` is set but the request didn't carry the header | Configure the reverse proxy to inject the header (see SECURITY.md) |
| Skyfield ephemeris download fails | No internet access on first run | Pre-download `de421.bsp` and place it in `[almanac] ephemeris_directory` |

Check the service logs for structured JSON entries:

```bash
journalctl -u weewx-clearskies-api -f
```
