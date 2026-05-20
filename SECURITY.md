# Security — weewx-clearskies-api

This repository is part of [Clear Skies](https://github.com/inguy24/weewx-clearskies-stack), distributed AS-IS under [GPL v3](LICENSE). There is no support window, no LTS, and no security backport policy — only the current release is available. See [ADR-018](https://github.com/inguy24/weewx-clearskies-stack/blob/master/docs/decisions/ADR-018-api-versioning-policy.md) for the versioning and AS-IS policy.

---

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

**Security tab → Advisories → "Report a vulnerability"**

Or open a GitHub issue prefixed with `[security]` if private reporting is unavailable.

---

## Trust model

clearskies-api is a **read-only data service** for a weather station. It publishes publicly available environmental data. There are no user accounts, no write operations, and no payment or health data. The threat model reflects this.

**Threat boundaries:**

1. **Internet → reverse proxy** — TLS termination, request filtering. The operator controls this layer. Apache, Nginx, and Caddy are all suitable.
2. **Reverse proxy → clearskies-api** — loopback or LAN. Optionally authenticated with a shared secret header (see below).
3. **clearskies-api → database** — read-only database user enforced at startup.
4. **clearskies-api → external providers** — HTTPS outbound. No inbound connections accepted from providers.

---

## Authentication

### No end-user authentication

clearskies-api provides no user login, session management, or access control. Weather data is public information. Operators who need access control (password-protected site, private station) add it at the reverse proxy layer:

- Apache `mod_auth_basic` with `.htpasswd`
- [Authelia](https://www.authelia.com/) for SSO
- [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/applications/configure-apps/) for zero-trust access

This is consistent with every other weewx skin (Belchertown, Seasons, Weather Eye) — none of them implement user logins.

### Optional proxy shared secret (cross-host deploys)

When the dashboard and API run on different hosts, a shared secret prevents direct access to the API from the LAN, bypassing the reverse proxy.

**When it applies:** when `[api] bind_host` is set to a non-loopback address. Single-host deployments where the API binds to `127.0.0.1` or `::1` do not need a shared secret — loopback is the trust boundary.

**Setup:**

Generate a secret:

```bash
openssl rand -hex 32
```

Add to `/etc/weewx-clearskies/secrets.env` on the API host (mode 0600):

```bash
WEEWX_CLEARSKIES_PROXY_SECRET=<generated-value>
```

Configure the reverse proxy to inject the header on every request to the API:

```nginx
# Nginx
proxy_set_header X-Clearskies-Proxy-Auth "<secret>";
```

```apache
# Apache
RequestHeader set X-Clearskies-Proxy-Auth "<secret>"
```

```caddy
# Caddy
reverse_proxy api-host:8765 {
    header_up X-Clearskies-Proxy-Auth {env.CLEARSKIES_PROXY_SECRET}
}
```

**What happens without it:** when `bind_host` is non-loopback and the secret is unset, the service starts but logs a loud `WARNING` at startup and every 60 seconds. Anyone on the LAN who can reach the API port can read weather data directly.

**Constant-time comparison** — the middleware uses `hmac.compare_digest` to prevent timing side channels. A mismatch returns 401 with no body.

---

## Database security

The database user must have **SELECT privileges only**. clearskies-api enforces this at startup with a write-probe: it attempts to insert a row into the archive table and fails-closed (exits with a CRITICAL log) if the insert succeeds.

```sql
-- Correct grant (SELECT only)
GRANT SELECT ON weewx.* TO 'clearskies_ro'@'127.0.0.1';
GRANT SELECT ON weewx.* TO 'clearskies_ro'@'::1';
```

The database password is loaded from the `WEEWX_CLEARSKIES_DB_PASSWORD` environment variable, not from `api.conf`. The config file has a secret-leak guard: any INI key whose name ends in `_KEY`, `_SECRET`, `_TOKEN`, or `_PASSWORD` causes the service to refuse to start.

---

## Input validation

All query parameters and request bodies are validated by Pydantic models with `extra="forbid"`. Unknown query parameters return 422. The Pydantic model is wired through a FastAPI `Depends()` function that passes the full query string, so `extra="forbid"` actually fires (the pattern is documented in `rules/coding.md`).

SQL queries use SQLAlchemy's parameterized query interface — no string interpolation.

---

## Secret handling

- **API keys and passwords** are loaded from environment variables (typically `secrets.env`), not from `api.conf`.
- **Secret-leak guard**: the config loader checks every INI leaf key against a name pattern. Any key matching `_(KEY|SECRET|TOKEN|PASSWORD)$` causes a FATAL startup error.
- **Log redaction**: the JSON logging layer strips values that look like credentials from log records before they are written. Auth headers and SQL bind parameters are redacted.

---

## Security headers

clearskies-api sets the following response headers on every API response:

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |

HSTS and full CSP are set at the reverse proxy layer, not inside the FastAPI service.

---

## Rate limiting

Requests are rate-limited per source IP (default: 60 requests per minute). The rate limiter uses in-process storage. Multi-worker deployments need Redis (`CLEARSKIES_CACHE_URL`) for the limit to apply across workers.

The `/health/live` and `/health/ready` endpoints are on a separate loopback-only port (`8081` by default) and are not rate-limited.

---

## Request size limit

Request bodies are limited to 1 MiB by default (`[api] max_request_bytes = 1048576`). Requests exceeding this limit return 413 Problem+JSON.

---

## Dependency auditing

The CI pipeline runs `pip-audit` on every pull request and on a nightly cron schedule. The `uv.lock` file pins all transitive dependencies to exact versions.

---

## Process hardening (systemd)

The example systemd unit in [INSTALL.md](INSTALL.md) includes these hardening options:

```ini
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/cache/weewx-clearskies
PrivateTmp=true
```

Operators may add further restrictions (`ProtectHome`, `RestrictAddressFamilies`, etc.) based on their environment.

---

## Known limitations and accepted risks

| Item | Status |
|---|---|
| Rate limit not effective in multi-worker deploys without Redis | Document; acceptable at v0.1 single-worker default |
| Shared secret is plaintext on the LAN | Acceptable for home LAN deployments; operators on adversarial networks should use a single-host deploy or add TLS between proxy and API |
| No revocation or expiry on the proxy secret | Operators rotate manually (`openssl rand -hex 32`; update both sides) |
