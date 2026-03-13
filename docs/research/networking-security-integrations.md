# Networking & Security Service Integration Research

Research date: 2026-03-10

This document captures API capabilities, monitoring endpoints, actionable operations, and failure modes for six networking/security services under consideration for OasisAgent integration.

---

## 1. UniFi (Ubiquiti)

UniFi exposes three distinct API surfaces: Network (switches, APs, routing), Protect (cameras, NVR), and Gateway (firewall, WAN, VPN). All run on the same UniFi OS console.

### 1.1 API Type & Authentication

**Two API tiers exist:**

| API | Base URL | Auth | Notes |
|-----|----------|------|-------|
| **Site Manager API** (cloud) | `https://api.ui.com/v1/` or `/ea/` | API Key (Bearer token) | Official, documented at [developer.ui.com](https://developer.ui.com/site-manager-api/gettingstarted). Aggregated cross-site data. |
| **Local Controller API** (on-prem) | `https://{controller}/api/s/{site}/...` | Session cookie via `POST /api/auth/login` | Community-documented. Full device control. On UDM/UCG devices, prefix all paths with `/proxy/network`. |

**Authentication flow (local):**
1. `POST /api/auth/login` with `{"username": "...", "password": "..."}` — returns session cookie.
2. Include cookie on all subsequent requests.
3. Sessions expire; re-auth required.

**Authentication (Site Manager):**
1. Generate API key in Network > Control Plane > Integrations (requires Network Application 9.1.105+).
2. Pass as `Authorization: Bearer {key}` header.

### 1.2 Key Monitoring Endpoints & Events

**Network (local controller):**

| Endpoint | Returns |
|----------|---------|
| `GET /api/s/{site}/stat/device` | All device status — model, version, uptime, load, temperatures, port table, radio stats |
| `GET /api/s/{site}/stat/device-basic` | Lightweight — MAC + type only |
| `GET /api/s/{site}/stat/sta` | Connected clients — signal, TX/RX, hostname, IP |
| `GET /api/s/{site}/rest/alarm` | Alerts/alarms (filter: `?archived=false`) |
| `GET /api/s/{site}/stat/health` | Subsystem health summaries (WAN, LAN, WLAN) |
| `GET /api/s/{site}/stat/routing` | Routing table |
| `GET /api/s/{site}/rest/firewallrule` | User-defined firewall rules |
| `GET /api/s/{site}/rest/firewallgroup` | Firewall groups |
| `GET /api/s/{site}/stat/dpi` | Deep packet inspection stats |

**WebSocket events (local):** `wss://{controller}/wss/s/{site}/events` — real-time device state changes, client connect/disconnect, alerts.

**Site Manager API:**

| Endpoint | Returns |
|----------|---------|
| `GET /ea/devices` | All devices across sites with status |
| `GET /ea/sites` | Site list with ISP info, device counts |
| Internet Health Metrics | WAN latency, uptime, speed |

**Protect API (local, reverse-engineered):**

| Endpoint | Returns |
|----------|---------|
| `GET /proxy/protect/api/bootstrap` | Full system state — all cameras, NVR config, users, current device states |
| `WSS /proxy/protect/ws/updates?lastUpdateId={id}` | Real-time binary-encoded stream: camera health, motion events, doorbell rings, status changes |
| Camera objects in bootstrap | Per-camera: connection state, recording mode, video settings, uptime, firmware |
| NVR object in bootstrap | Storage health (per-disk good/bad), capacity, recording retention |

The Protect websocket uses a binary protocol (not JSON) to minimize bandwidth. The `lastUpdateId` comes from the bootstrap response.

### 1.3 Actionable Operations

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Restart device | `POST` | `/api/s/{site}/cmd/devmgr` with `{"cmd": "restart", "mac": "..."}` |
| Disable/enable port | `PUT` | `/api/s/{site}/rest/device/{id}` (modify port_overrides) |
| Block/unblock client | `POST` | `/api/s/{site}/cmd/stamgr` with `{"cmd": "block-sta", "mac": "..."}` |
| Force provision | `POST` | `/api/s/{site}/cmd/devmgr` with `{"cmd": "force-provision", "mac": "..."}` |
| Archive alarm | `PUT` | `/api/s/{site}/rest/alarm/{id}` |
| Set locate LED | `POST` | `/api/s/{site}/cmd/devmgr` with `{"cmd": "set-locate", "mac": "..."}` |

### 1.4 Failure Modes Worth Detecting

- **Device offline/disconnected** — device `state` != 1 in stat/device
- **WAN failover** — health subsystem WAN status change, gateway `wan1`/`wan2` link state
- **AP radio failure** — radio stats showing zero clients + `"radio_table"` anomalies
- **Port down** — `port_table[n].up == false` on switches
- **High CPU/memory** — `"system-stats"` in device payload: `cpu > 90%`, `mem > 90%`
- **Firmware mismatch** — `upgradable == true` across fleet
- **NVR disk failure** — disk health sensor reporting "bad" in Protect bootstrap
- **Camera disconnected** — camera `state` in bootstrap not "CONNECTED"
- **Recording stopped** — camera `recordingSettings.mode` mismatch vs actual recording state
- **Storage full** — NVR storage capacity nearing 100%
- **IPS/IDS alerts** — threat management events via alarms endpoint
- **VPN tunnel down** — VPN status in gateway health

---

## 2. Cloudflare / Cloudflared (Argo Tunnel)

### 2.1 API Type & Authentication

| Detail | Value |
|--------|-------|
| API | REST, `https://api.cloudflare.com/client/v4/` |
| Auth options | API Token (scoped, preferred) or Global API Key + Email |
| Header | `Authorization: Bearer {token}` or `X-Auth-Key` + `X-Auth-Email` |
| Rate limits | 1200 requests/5 min per user |
| Docs | [developers.cloudflare.com/api](https://developers.cloudflare.com/api/) |

### 2.2 Key Monitoring Endpoints & Events

**DNS:**

| Endpoint | Returns |
|----------|---------|
| `GET /zones/{zone_id}/dns_records` | All DNS records for a zone |
| `GET /zones/{zone_id}/dns_analytics/report` | DNS query volume, response codes, latency |

**Tunnels (Zero Trust):**

| Endpoint | Returns |
|----------|---------|
| `GET /accounts/{account_id}/cfd_tunnel` | List all tunnels with status (active/inactive/degraded) |
| `GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}` | Single tunnel detail — connection count, status, connectors |
| `GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/connections` | Active connections per connector/replica |

Note: As of Dec 2025, list endpoints no longer return deleted tunnels by default. Use `?is_deleted=true` to include them.

As of Feb 2026, the `cloudflared proxy-dns` command has been removed from new releases.

**WAF & Security:**

| Endpoint | Returns |
|----------|---------|
| `GET /zones/{zone_id}/security/events` | WAF events — action taken, rule matched, source IP, timestamp |
| GraphQL Analytics API | More flexible query interface for security analytics; avoids dashboard pagination limits |
| `GET /zones/{zone_id}/firewall/events` (Logpull) | Firewall event logs (Enterprise) |

**Analytics:**

| Endpoint | Returns |
|----------|---------|
| `GET /zones/{zone_id}/analytics/dashboard` | Requests, bandwidth, threats, page views |
| GraphQL: `httpRequests1hGroups` | Hourly request/bandwidth aggregates |

### 2.3 Actionable Operations

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Create/update DNS record | `POST`/`PUT` | `/zones/{zone_id}/dns_records` |
| Purge cache | `POST` | `/zones/{zone_id}/purge_cache` |
| Create tunnel | `POST` | `/accounts/{account_id}/cfd_tunnel` |
| Delete tunnel | `DELETE` | `/accounts/{account_id}/cfd_tunnel/{tunnel_id}` |
| Update tunnel config | `PUT` | `/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations` |
| Create WAF rule | `POST` | `/zones/{zone_id}/firewall/rules` |
| Block IP (Access Rule) | `POST` | `/zones/{zone_id}/firewall/access_rules/rules` |

### 2.4 Failure Modes Worth Detecting

- **Tunnel disconnected** — tunnel status != "active", zero active connections
- **Tunnel degraded** — fewer connectors/replicas than expected
- **DNS propagation failure** — record creation succeeded but resolution fails (requires external check)
- **WAF spike** — sudden increase in blocked requests indicating attack
- **Certificate expiry** — edge certificate approaching expiration
- **Origin unreachable** — 522/523/524 error codes in analytics
- **Rate limiting triggered** — 429 responses in analytics
- **Zero Trust policy violation** — Access audit logs showing denied requests

---

## 3. NextDNS

### 3.1 API Type & Authentication

| Detail | Value |
|--------|-------|
| API | REST, `https://api.nextdns.io/` |
| Auth | API Key as `X-Api-Key` header |
| Key location | Bottom of account page at nextdns.io |
| Docs | [nextdns.github.io/api](https://nextdns.github.io/api/) |
| Rate limits | Not publicly documented |

### 3.2 Key Monitoring Endpoints & Events

| Endpoint | Returns |
|----------|---------|
| `GET /profiles` | List all profiles (configurations) |
| `GET /profiles/{id}/analytics/status` | Query counts by status: allowed, blocked, relayed, default |
| `GET /profiles/{id}/analytics/protocols` | Breakdown by protocol (DoH, DoT, DNS) |
| `GET /profiles/{id}/analytics/dnssec` | DNSSEC validation stats |
| `GET /profiles/{id}/analytics/encryption` | Encrypted vs unencrypted query ratio |
| `GET /profiles/{id}/analytics/domains` | Top queried domains |
| `GET /profiles/{id}/analytics/blockedDomains` | Top blocked domains |
| `GET /profiles/{id}/logs` | Query logs with filtering |
| `GET /profiles/{id}/logs/stream` | **SSE (Server-Sent Events)** real-time log stream |
| `GET /profiles/{id}/logs/download` | Bulk log export (redirects to file URL) |

**Time series:** Append `;series` to any analytics endpoint (e.g., `.../analytics/status;series`) to get time-bucketed data instead of aggregates.

**Date filtering:** Supports ISO 8601, Unix timestamps (seconds or milliseconds), and relative formats (`-6h`, `-1d`, `-3M`, `now`).

**Pagination:** Uses cursor-based pagination with `limit` and `cursor` parameters.

### 3.3 Actionable Operations

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Add to denylist | `POST` | `/profiles/{id}/denylist` |
| Remove from denylist | `DELETE` | `/profiles/{id}/denylist/{domain}` |
| Add to allowlist | `POST` | `/profiles/{id}/allowlist` |
| Update privacy settings | `PATCH` | `/profiles/{id}/privacy` |
| Update blocklists | `PUT` | `/profiles/{id}/privacy/blocklists` |
| Update security settings | `PATCH` | `/profiles/{id}/security` |
| Add DNS rewrite | `POST` | `/profiles/{id}/rewrites` |

**API patterns:** Object endpoints (e.g., `/privacy`) support GET + PATCH. Array endpoints (e.g., `/privacy/blocklists`) support GET + PUT + POST, with child items supporting PATCH + DELETE.

### 3.4 Failure Modes Worth Detecting

- **Resolution failures** — spike in SERVFAIL or timeout responses in logs
- **Blocking anomalies** — sudden drop in blocked percentage (misconfiguration) or spike (false positives)
- **Unencrypted queries** — encryption analytics showing non-DoH/DoT traffic (misconfigured client)
- **Query volume anomaly** — unusual spike could indicate malware/C2 beaconing
- **Profile misconfiguration** — security settings changed unexpectedly
- **DNSSEC failures** — validation failures in dnssec analytics
- **Latency degradation** — can be inferred from log stream timestamps vs query timestamps

---

## 4. fail2ban

### 4.1 API Type & Authentication

fail2ban has **no REST API**. All interaction is via:

| Interface | Details |
|-----------|---------|
| `fail2ban-client` CLI | Communicates with `fail2ban-server` over a Unix socket (`/var/run/fail2ban/fail2ban.sock`) |
| SQLite database | `fail2ban.sqlite3` — can be queried directly for ban history (risk of lock contention) |
| Log files | `/var/log/fail2ban.log` — structured text, parseable for ban/unban events |
| systemd journal | `journalctl -u fail2ban` — same data if systemd logging is enabled |

**Authentication:** Unix socket permissions (root or fail2ban group). No token/key auth.

### 4.2 Key Monitoring Commands & Events

| Command | Returns |
|---------|---------|
| `fail2ban-client status` | List of all jails |
| `fail2ban-client status {jail}` | Currently banned IPs, total banned count, total failed count, filter file list |
| `fail2ban-client get {jail} bantime` | Ban duration for jail |
| `fail2ban-client get {jail} maxretry` | Failure threshold |
| `fail2ban-client get {jail} findtime` | Window for counting failures |
| `fail2ban-client banned` | All currently banned IPs across all jails (fail2ban 0.11+) |

**Log patterns to parse:**

```
# Ban event
2026-03-10 12:00:00,000 fail2ban.actions [PID]: NOTICE [sshd] Ban 192.168.1.100

# Unban event
2026-03-10 12:30:00,000 fail2ban.actions [PID]: NOTICE [sshd] Unban 192.168.1.100

# Found (pre-ban match)
2026-03-10 11:59:00,000 fail2ban.filter [PID]: INFO [sshd] Found 192.168.1.100
```

### 4.3 Actionable Operations

| Operation | Command |
|-----------|---------|
| Unban IP | `fail2ban-client set {jail} unbanip {ip}` |
| Ban IP manually | `fail2ban-client set {jail} banip {ip}` |
| Reload jail | `fail2ban-client reload {jail}` |
| Restart fail2ban | `fail2ban-client restart` |
| Add ignore IP | `fail2ban-client set {jail} addignoreip {ip}` |
| Remove ignore IP | `fail2ban-client set {jail} delignoreip {ip}` |

### 4.4 Failure Modes Worth Detecting

- **Jail down** — jail not listed in `fail2ban-client status`
- **fail2ban service stopped** — process not running / socket unavailable
- **Ban storm** — high rate of bans indicating brute-force attack
- **Repeated ban/unban cycling** — same IP being banned and unbanned repeatedly (ban duration too short)
- **Database locked** — SQLite contention causing missed bans
- **Filter not matching** — zero "Found" entries despite known bad traffic (regex outdated)
- **Action failure** — ban action (iptables/nftables) failing silently

### 4.5 Integration Pattern for OasisAgent

Since fail2ban has no API, the recommended integration approach:
1. **Ingestion:** Tail `/var/log/fail2ban.log` (or subscribe to journald) and parse ban/unban events into OasisAgent `Event` objects.
2. **Status polling:** Periodically run `fail2ban-client status` and per-jail status via subprocess.
3. **Actions:** Execute `fail2ban-client` commands via subprocess with appropriate error handling.
4. **Alternative:** Use fail2ban's built-in action system to publish ban/unban events to MQTT, which OasisAgent already ingests.

---

## 5. Bitwarden / Vaultwarden (Self-Hosted)

### 5.1 API Type & Authentication

| Detail | Value |
|--------|-------|
| API | Bitwarden-compatible REST API (implemented by Vaultwarden in Rust) |
| Auth (vault) | OAuth2 token via `POST /identity/connect/token` with `grant_type=password` |
| Auth (admin panel) | Admin token set via `ADMIN_TOKEN` env var, accessed at `/admin` |
| Health endpoint | `GET /alive` or `GET /api/alive` — **unauthenticated** |
| Database | SQLite (default), MySQL, or PostgreSQL |

Vaultwarden is API-compatible with all official Bitwarden clients (browser extensions, desktop, mobile, CLI).

### 5.2 Key Monitoring Endpoints & Events

| Endpoint / Signal | Returns |
|-------------------|---------|
| `GET /alive` | Simple health check — 200 if running |
| `GET /api/alive` | Alternate health check path |
| Admin panel `/admin` | Users list, organizations, diagnostics |
| Container logs | All authentication attempts including failures ("Username or password is incorrect" at `/identity/connect/token`) |
| Database (`events` table) | Login events, vault item access, org changes (Bitwarden event types) |

**No dedicated monitoring API exists.** Monitoring relies on:
- HTTP health checks against `/alive`
- Docker container health/logs
- Database queries for event audit trail
- Reverse proxy access logs for failed auth attempts

### 5.3 Actionable Operations

| Operation | Method |
|-----------|--------|
| Check health | `GET /alive` |
| Backup database | Copy SQLite file (or use `BACKUP` command); backup attachments directory |
| User management (admin) | Via admin panel API at `/admin/users` |
| Disable user | Admin panel |
| Delete user | Admin panel |
| Invite user | Admin panel |

**Backup strategy:** The critical files are the SQLite database (`db.sqlite3`), the `attachments/` directory, and the `sends/` directory. Automated backups should copy these on a schedule and verify integrity.

### 5.4 Failure Modes Worth Detecting

- **Service down** — `/alive` returns non-200 or times out
- **Brute-force login attempts** — repeated "incorrect" messages in logs for same identity
- **Database corruption** — SQLite integrity check failure
- **Backup staleness** — backup file age exceeds threshold
- **Disk space exhaustion** — attachments/database growth
- **SSL certificate issues** — if Vaultwarden serves HTTPS directly (rare; usually behind reverse proxy)
- **High memory usage** — Vaultwarden process memory growth (leak indicator)
- **Unauthorized admin access** — requests to `/admin` from unexpected IPs

---

## 6. Nginx Proxy Manager (NPM)

### 6.1 API Type & Authentication

| Detail | Value |
|--------|-------|
| API | REST (underdocumented), base path `/api/` |
| Auth | JWT token via `POST /api/tokens` |
| Token request body | `{"identity": "admin@example.com", "secret": "password", "expiry": "1y"}` |
| Token usage | `Authorization: Bearer {jwt}` header |
| Docs | No official API docs; schemas in [GitHub repo](https://github.com/NginxProxyManager/nginx-proxy-manager/tree/develop/backend/schema) |

**Important:** The Swagger/OpenAPI spec is incomplete. The audit log in the web UI shows the exact JSON payloads for any operation, which is the most reliable way to reverse-engineer endpoints.

### 6.2 Key Monitoring Endpoints & Events

| Endpoint | Returns |
|----------|---------|
| `GET /api/nginx/proxy-hosts` | All proxy host configs — domain names, forward host/port, SSL status, enabled state |
| `GET /api/nginx/redirection-hosts` | Redirection host configs |
| `GET /api/nginx/dead-hosts` | 404 hosts (custom 404 pages) |
| `GET /api/nginx/streams` | TCP/UDP stream configs |
| `GET /api/nginx/certificates` | SSL certificates — provider, expiration date, domains, status |
| `GET /api/nginx/access-lists` | Access control lists |
| `GET /api/audit-log` | Admin action history |
| `GET /api/reports/hosts` | Host status summary |

**Additional monitoring signals:**
- Nginx access logs: `/data/logs/proxy-host-{n}_access.log`
- Nginx error logs: `/data/logs/proxy-host-{n}_error.log`
- Let's Encrypt renewal logs in container output

### 6.3 Actionable Operations

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Create proxy host | `POST` | `/api/nginx/proxy-hosts` |
| Update proxy host | `PUT` | `/api/nginx/proxy-hosts/{id}` |
| Delete proxy host | `DELETE` | `/api/nginx/proxy-hosts/{id}` |
| Enable/disable host | `POST` | `/api/nginx/proxy-hosts/{id}/enable` or `/disable` |
| Request SSL cert | `POST` | `/api/nginx/certificates` |
| Renew SSL cert | Via Let's Encrypt auto-renewal or manual trigger |
| Create access list | `POST` | `/api/nginx/access-lists` |

### 6.4 Failure Modes Worth Detecting

- **SSL certificate expiry** — certificate `expires_on` approaching current date (known bug: auto-renewal sometimes fails)
- **Proxy host unreachable** — upstream returning 502/503/504 errors in access logs
- **Certificate renewal failure** — Let's Encrypt errors in container logs
- **Service down** — NPM API returning non-200 on health check
- **Configuration drift** — proxy host count or settings changed unexpectedly
- **Access log anomalies** — spike in 4xx/5xx errors, unusual request patterns
- **Disk space** — log file growth, especially if log rotation is not configured
- **Inactive SSL status** — known issue where certificates show "Inactive" despite being in use on redirection hosts

---

## Integration Priority Assessment

For OasisAgent Phase 2 ingestion adapters, ranked by monitoring value and API maturity:

| Priority | Service | Rationale |
|----------|---------|-----------|
| 1 | **UniFi Network** | Official API with API key auth. Rich device/network health data. WebSocket events for real-time. Core infrastructure. |
| 2 | **Cloudflare** | Mature REST API with token auth. Tunnel health is critical for external access. WAF events valuable for threat detection. |
| 3 | **NextDNS** | Clean REST API with SSE streaming. DNS is foundational; resolution failures affect everything. |
| 4 | **Nginx Proxy Manager** | REST API exists (underdocumented). SSL expiry and upstream health are high-value signals. |
| 5 | **fail2ban** | No API — requires log tailing or subprocess calls. High security value but integration is more complex. Could use MQTT action to publish events. |
| 6 | **UniFi Protect** | Reverse-engineered binary websocket protocol. Complex integration but valuable for physical security monitoring. |
| 7 | **Vaultwarden** | Minimal monitoring surface (health check + logs). Low-frequency events. Simple uptime check sufficient initially. |

---

## Sources

### UniFi
- [Getting Started with the Official UniFi API (Ubiquiti Help Center)](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API)
- [UniFi Site Manager API — Getting Started (Developer Portal)](https://developer.ui.com/site-manager-api/gettingstarted)
- [UniFi Site Manager API — List Devices](https://developer.ui.com/site-manager-api/list-devices)
- [UniFi Controller API (Community Wiki)](https://ubntwiki.com/products/software/unifi-controller/api)
- [UniFi API Best Practices (GitHub)](https://github.com/uchkunrakhimow/unifi-best-practices)
- [UniFi Protect API — hjdhjd/unifi-protect (GitHub)](https://github.com/hjdhjd/unifi-protect)
- [UniFi Protect — Home Assistant Integration](https://www.home-assistant.io/integrations/unifiprotect/)

### Cloudflare
- [Cloudflare API Overview](https://developers.cloudflare.com/api/)
- [Zero Trust Tunnels API](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/)
- [List All Tunnels API](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/methods/list/)
- [WAF Security Events](https://developers.cloudflare.com/waf/analytics/security-events/)
- [WAF Security Analytics](https://developers.cloudflare.com/waf/analytics/security-analytics/)
- [Cloudflare Tunnel Docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
- [Tunnel Common Errors](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/troubleshoot-tunnels/common-errors/)
- [Cloudflare Logs (Logpull)](https://developers.cloudflare.com/logs/)
- [Firewall Event Log Fields](https://developers.cloudflare.com/logs/reference/log-fields/zone/firewall_events/)

### NextDNS
- [NextDNS API Documentation](https://nextdns.github.io/api/)
- [NextDNS API Help Center](https://help.nextdns.io/t/y4hdw19/api)

### fail2ban
- [fail2ban GitHub Repository](https://github.com/fail2ban/fail2ban)
- [fail2ban-client Man Page](https://www.mankier.com/1/fail2ban-client)
- [fail2ban Monitoring API Discussion (GitHub Issue #861)](https://github.com/fail2ban/fail2ban/issues/861)
- [Monitoring fail2ban Logs](https://www.the-art-of-web.com/system/fail2ban-log/)
- [fail2ban ArchWiki](https://wiki.archlinux.org/title/Fail2ban)

### Vaultwarden / Bitwarden
- [Vaultwarden GitHub Repository](https://github.com/dani-garcia/vaultwarden)
- [Vaultwarden Health Check Discussion](https://github.com/dani-garcia/vaultwarden/discussions/5117)
- [Vaultwarden REST API Discussion](https://github.com/dani-garcia/vaultwarden/discussions/4241)
- [Vaultwarden API Reference (DeepWiki)](https://deepwiki.com/dani-garcia/vaultwarden/3-api-reference)

### Nginx Proxy Manager
- [Nginx Proxy Manager Official Site](https://nginxproxymanager.com/)
- [NPM API Discussion (GitHub)](https://github.com/NginxProxyManager/nginx-proxy-manager/discussions/3527)
- [NPM REST API Usage (GitHub)](https://github.com/NginxProxyManager/nginx-proxy-manager/discussions/3265)
- [NPM Authentication (DeepWiki)](https://deepwiki.com/NginxProxyManager/nginx-proxy-manager/5.2-authentication)
- [NPM Users API (DeepWiki)](https://deepwiki.com/NginxProxyManager/nginx-proxy-manager/9.7-users-api)
