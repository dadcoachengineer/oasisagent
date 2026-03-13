# OasisAgent Phase 3 — Comprehensive Plan

*Generated 2026-03-10 from extensive API research across 40+ services.*
*Updated 2026-03-11 incorporating CTO review (PHASE3-CTO-REVIEW.md).*

---

## Table of Contents

1. [Environment Inventory](#1-environment-inventory)
2. [Integration Catalog](#2-integration-catalog)
3. [Architecture Features](#3-architecture-features)
4. [Implementation Roadmap](#4-implementation-roadmap)
5. [New Ingestion Patterns](#5-new-ingestion-patterns)
6. [Known Fixes Expansion](#6-known-fixes-expansion)
7. [Dependency Changes](#7-dependency-changes)
8. [Architecture Decision Records](#8-architecture-decision-records)

---

## 1. Environment Inventory

Every service running in the home lab, grouped by function.

### Infrastructure & Virtualization
| Service | Protocol | Auth | Push? | Notes |
|---------|----------|------|-------|-------|
| Proxmox VE (PVE01, PVE02) | REST (port 8006) | API token | Yes (webhooks, PVE 8.3+) | Full VM/CT CRUD, ZFS, cluster, tasks |
| Proxmox Backup Server (PBS) | REST (port 8007) | API token | Yes (webhooks) | Backup jobs, verification, datastore health |
| Portainer | REST | API key / JWT | Partial (Docker event stream) | Multi-environment Docker management |
| Synology NAS | REST (`SYNO.*`) + SNMP | Session cookie | No | SMART, RAID, UPS, packages |
| TuringPi | REST (BMC) | Basic (fw v2.0+) | No | Per-slot power, USB routing, UART |
| PiKVM | REST | Basic / session | No | OCR on video stream, ATX power, HID |

### Networking & Security
| Service | Protocol | Auth | Push? | Notes |
|---------|----------|------|-------|-------|
| UniFi Network | REST (local controller) | Session cookie | No | Devices, clients, alerts, DPI |
| UniFi Protect | REST + binary WebSocket | Session cookie | Yes (WS events) | Camera health, recordings, motion |
| UniFi Gateway | Via UniFi Network API | Session cookie | No | WAN, firewall, VPN |
| Cloudflare | REST v4 | Bearer token | No | DNS, tunnels, WAF, analytics |
| Cloudflared (Argo Tunnel) | Via Cloudflare API | Bearer token | No | Tunnel health, reconnections |
| NextDNS | REST + SSE | API key | Yes (SSE log stream) | Query logs, blocking stats |
| fail2ban | None (CLI + logs) | N/A | No | Jail status, ban/unban events |
| Bitwarden (Vaultwarden) | `/alive` health only | None | No | Basic uptime monitoring |
| Nginx Proxy Manager | REST (undocumented) | JWT | No | Proxy hosts, SSL cert expiry |

### Smart Home & IoT
| Service | Protocol | Auth | Push? | Notes |
|---------|----------|------|-------|-------|
| Home Assistant (Prod/Dev/Test) | WebSocket + REST | Long-lived token | Yes (WS) | Already integrated (Phase 1-2) |
| Zigbee2MQTT | MQTT only | Broker auth | Yes (MQTT) | Bridge health, device availability, OTA |
| Z-Wave JS UI | REST + WebSocket | Optional | Yes (WS) | Controller health, dead nodes, heal |
| Frigate NVR | REST + MQTT + Prometheus | None (proxy) | Yes (MQTT) | Camera FPS, detection, Coral TPU |
| ESPresence | MQTT only | Broker auth | Yes (MQTT) | Room presence, node telemetry |
| Valetudo | REST + MQTT (Homie) | Basic auth | Yes (MQTT) | Robot status, errors, consumables |
| Ecowitt | Local HTTP + MQTT (via ecowitt2mqtt) | None | Yes (HTTP push) | Sensors, batteries, data gaps |
| Hyperion | JSON-RPC (WS/HTTP) | Token | Yes (WS subscribe) | LED device, grabber, instances |
| WLED (multiple) | JSON HTTP + MQTT | None | Partial (MQTT) | Heap, WiFi signal, power draw |
| ESPHome / ESPConnect | Native protobuf + REST + SSE | API key | Yes (native + SSE) | Device status, OTA, WiFi RSSI |
| BirdNET | REST + MQTT + Prometheus | None | Yes (MQTT/SSE) | Detection events, audio capture |

### Media Stack
| Service | Protocol | Auth | Push? | Notes |
|---------|----------|------|-------|-------|
| Plex | REST + webhooks | Token header | Yes (webhooks, Plex Pass) | Sessions, transcode, DB health |
| qBittorrent | REST | Cookie session | No | Transfer stats, torrent errors, disk |
| Radarr | REST (OpenAPI) + webhooks | API key | Yes (webhooks) | Health, queue, indexers, disk |
| Sonarr | REST (OpenAPI) + webhooks | API key | Yes (webhooks) | Health, queue, indexers, disk |

### Databases & Storage
| Service | Protocol | Auth | Push? | Notes |
|---------|----------|------|-------|-------|
| MariaDB | MySQL wire + MaxScale REST | DB auth / Basic | No | Replication, connections, slow queries |
| InfluxDB v2 | REST + Prometheus | Token | No | Health, tasks, cardinality |
| Backblaze B2 | REST (S3-compat) | App key | Yes (event notifications) | Backup freshness, lifecycle |

### Automation & AI
| Service | Protocol | Auth | Push? | Notes |
|---------|----------|------|-------|-------|
| N8N | REST | API key | Partial (error workflows) | Executions, workflow status |
| Ollama | REST | None | No | Model health, VRAM, OOM detection |
| Stable Diffusion (A1111/ComfyUI) | REST + WebSocket | Basic / None | Partial (ComfyUI WS) | Queue, VRAM, model load |
| Jupyter | REST | Token | No | Kernels, sessions, memory |

### Communication & Productivity
| Service | Protocol | Auth | Push? | Notes |
|---------|----------|------|-------|-------|
| EMQX | REST (OpenAPI) + Prometheus | API key/secret | No | Stats, clients, alarms, listeners |
| Stalwart Mail | REST + JMAP + Prometheus | Bearer / Basic | No | Queue depth, delivery failures, TLS |
| Nextcloud | OCS REST + WebDAV | Basic / App password | No | Server health, cron, disk, sync |
| Mealie | REST (FastAPI) | Bearer token | No | Basic health only |
| Traccar | REST + WebSocket | Basic / session | Yes (WS + webhooks) | Device status, geofences |
| Uptime Kuma | Socket.IO + Prometheus | Session | Yes (push monitors) | Monitor status, response times |
| Code Server | `/healthz` only | Password cookie | No | Basic health only |

---

## 2. Integration Catalog

### Tier 1 — High Value, Build First (v0.3.0)

These provide the most monitoring value relative to implementation effort.

#### Proxmox VE Handler
- **Ingestion**: Webhook receiver (PVE 8.3+ notification framework) + HTTP polling for node/VM/CT status
- **Handler ops**: `notify`, `get_context` (node stats, task logs, VM status)
- **Known fixes**: VM stuck in locked state, ZFS scrub errors, backup failures, storage migration issues, HA fence events
- **Key endpoints**: `/api2/json/nodes/{node}/status`, `/api2/json/nodes/{node}/qemu`, `/api2/json/nodes/{node}/lxc`, `/api2/json/cluster/tasks`
- **Why first**: Foundation infrastructure — if Proxmox is down, everything else is affected

#### Docker/Portainer Handler
- **Ingestion**: Docker event stream via Portainer API (`/api/endpoints/{id}/docker/events`)
- **Handler ops**: `restart_container`, `get_logs`, `get_context`
- **Known fixes**: Container OOM kill, unhealthy loop, volume mount failure, image pull failure
- **Key endpoints**: `/api/endpoints/{id}/docker/containers/json`, `/api/endpoints/{id}/docker/events`
- **Why early**: Second-most-critical infrastructure; most services run as containers

#### Webhook Receiver Ingestion Adapter
- **Purpose**: Generic HTTP endpoint that accepts JSON payloads from services with webhook support
- **Consumers**: Radarr, Sonarr, Plex, Proxmox notifications, Backblaze events, Traccar
- **Design**: Routes mounted at `/ingest/webhook/{source}` on the main FastAPI app (single process with web UI)
- **Why foundational**: Enables push-based ingestion from ~6 services with one adapter

#### Servarr Adapter (Radarr + Sonarr)
- **Ingestion**: Webhooks (On Health Issue, On Grab, On Import) + HTTP polling `/api/v3/health`
- **Known fixes**: Download client unreachable, indexer down, root folder missing, disk space low, import failures
- **Why together**: Nearly identical API (Servarr family) — one adapter covers both

#### MQTT Topic Expansion
- **Already have**: MQTT ingestion adapter
- **Expand topics**: Zigbee2MQTT bridge state + device availability, Frigate events + stats, ESPresence telemetry, Valetudo events, WLED status, BirdNET sightings
- **Implementation**: Config-driven topic → event_type mapping via connector CRUD API
- **Why easy**: MQTT adapter already exists; just needs topic routing config

### Tier 2 — Medium Value (v0.3.3–v0.3.4)

#### UniFi Network Integration
- **Ingestion**: HTTP polling (device status, alerts, client list)
- **Auth challenge**: Local controller uses session cookies (login → POST `/api/login` → SID cookie). UDM/UCG prefix endpoints with `/proxy/network`
- **Known fixes**: AP disconnected, switch port down, ISP failover, rogue DHCP, high client count
- **Key endpoints**: `/api/s/{site}/stat/device`, `/api/s/{site}/stat/sta`, `/api/s/{site}/rest/alarm`

#### Cloudflare/Cloudflared Integration
- **Ingestion**: HTTP polling tunnel health + WAF events
- **Known fixes**: Tunnel disconnected, DNS propagation failure, WAF rule trigger spike, SSL cert renewal failure
- **Key endpoints**: `/client/v4/accounts/{id}/cfd_tunnel`, `/client/v4/zones/{id}/security/events`

#### Plex Integration
- **Ingestion**: Webhooks (Plex Pass) for events, HTTP polling `/status/sessions` and `/transcode/sessions`
- **Known fixes**: Database corruption (`admin.database.corrupted` webhook), transcode stall, library scan stuck
- **Requires**: Plex Pass for webhooks; polling fallback without it

#### Stalwart Mail Integration
- **Ingestion**: HTTP polling health + queue depth
- **Known fixes**: Queue backing up, delivery failures, TLS cert expiry, DKIM/DMARC failures
- **Key endpoints**: `/healthz/ready`, `/api/queue/messages`, `/metrics`

#### EMQX Broker Integration
- **Ingestion**: HTTP polling stats + alarms
- **Known fixes**: Client connection spike, listener down, message drop rate increase, memory pressure
- **Key endpoints**: `/api/v5/stats`, `/api/v5/alarms`, `/api/v5/nodes`
- **Meta-monitoring**: OasisAgent monitors its own message broker

### Tier 3 — Lower Priority / Simpler (v0.3.6)

#### Synology NAS
- **Ingestion**: REST polling (`SYNO.Core.Storage.*`) or SNMP
- **Known fixes**: RAID degradation, disk SMART failure, volume >85% full, UPS on battery
- **Complexity**: Session-based auth with SYNO API is fiddly; SNMP may be more reliable

#### N8N Workflow Monitoring
- **Ingestion**: HTTP polling `/api/v1/executions?status=error` + error workflow MQTT forwarding
- **Known fixes**: Workflow deactivated after repeated failures, execution timeout, credential expired

#### Nextcloud Health
- **Ingestion**: HTTP polling `/ocs/v2.php/apps/serverinfo/api/v1/info`
- **Known fixes**: Maintenance mode stuck, cron not running, disk space low

#### fail2ban
- **Ingestion**: Configure fail2ban action to publish to MQTT on ban/unban
- **Known fixes**: Brute force attack detected, jail disabled, excessive bans from single IP

#### Ollama Health
- **Ingestion**: HTTP polling `/` health + `/api/ps` loaded models
- **Known fixes**: Process crashed (OOM), model evicted, inference timeout

#### Simple Health Checks (Mealie, Code Server, Bitwarden, Jupyter)
- **Ingestion**: HTTP polling health endpoints
- **Minimal**: `/api/app/about`, `/healthz`, `/alive`, `/api/status`
- **Value**: Low — Uptime Kuma already covers these. Only add if T1/T2 correlation with other failures adds diagnostic value

#### ESPHome / WLED / Hyperion / Ecowitt / Valetudo / BirdNET
- **Ingestion**: Mostly via MQTT (already subscribed topics) or periodic HTTP health polls
- **Known fixes**: Battery low, device offline >5min, firmware mismatch, consumable expired
- **Note**: Many of these surface through HA entities already — direct integration adds value only when HA is itself the problem

---

## 3. Architecture Features

### 3.0 Configuration Architecture — UI-First (CTO Required Change)

**Decision**: The web UI is the primary configuration surface. Operators should never need to edit YAML or manage dozens of env vars to configure integrations.

**Three-layer config model**:

| Layer | What | Where | Managed By |
|-------|------|-------|------------|
| Bootstrap | Port, data dir, secret key, log level | 3–4 env vars | `docker-compose.yml` |
| Runtime config | All integrations, core services (MQTT, InfluxDB), notification channels, scanner settings | SQLite (secrets encrypted with Fernet via `OASIS_SECRET_KEY`) | Web UI + REST API |
| Content | Known fixes YAML, prompt templates | Files on disk (mountable volume) | Git / file mount |

**Bootstrap env vars (exhaustive list)**:

- `OASIS_PORT` — Listen port (default: `8080`)
- `OASIS_DATA_DIR` — SQLite + data directory (default: `/data`)
- `OASIS_SECRET_KEY` — Fernet key for encrypting secrets at rest (auto-generated on first run if missing)
- `OASIS_LOG_LEVEL` — Logging level (default: `info`)

Everything else — MQTT broker URL, InfluxDB endpoint, HA token, Proxmox connections, Telegram bot token, polling intervals — is configured through the web UI and stored in SQLite.

**Secrets handling**: All tokens, passwords, and API keys entered through the UI are encrypted at rest using Fernet symmetric encryption (from the `cryptography` package, already a transitive dependency). The `OASIS_SECRET_KEY` env var is the sole root of trust. Secrets are decrypted in-memory only when an adapter or handler needs them.

**First-run experience**: Container starts → setup wizard:

1. Admin account creation (username, password, optional TOTP)
2. Core services (MQTT broker URL + credentials, InfluxDB endpoint + token)
3. "Add your first integration" → connectors page

**config.yaml becomes optional import/export**:

```bash
# Seed database for automated/headless deployment
oasisagent config import seed.yaml

# Export current config for backup or migration
oasisagent config export > backup.yaml

# Normal operation — no config file needed
docker run -e OASIS_SECRET_KEY=... -v oasis_data:/data oasisagent
```

**SQLite migration strategy**:

- `schema_version` integer stored in the database
- On startup, check version and run sequential migration functions
- No Alembic — a `migrations/` directory with numbered Python scripts (`001_initial.py`, `002_add_scanner_config.py`)
- `config export` embeds schema version in YAML for version mismatch detection on import

**Impact**: SQLite schema + connector CRUD API must be built in v0.3.0 (not deferred). The web UI in v0.3.1 becomes a frontend to APIs that already exist.

### 3.1 Web Admin UI

**Stack decision**: FastAPI + HTMX + Jinja2 (not React/Svelte SPA)

**Rationale**: Zero JavaScript build toolchain. No npm, no webpack. HTMX returns HTML fragments from FastAPI endpoints. Alpine.js (3KB) for client-side interactivity. Tailwind via CDN. This keeps the project Python-only, reducing contributor friction.

**Single process**: The web UI, webhook receiver, and all HTTP endpoints run as a **single FastAPI application, single uvicorn process**. One port, one health check, one TLS termination point.

- Webhook routes: `/ingest/webhook/{source}`
- Admin UI routes: `/` (or `/admin/`)
- REST API routes: `/api/v1/`

**Real-time streaming**: SSE (Server-Sent Events) via `sse-starlette`, not WebSocket. The dashboard needs server→client streaming only. SSE auto-reconnects natively, works through proxies, and is simpler to test.

**Auth**: `passlib` (bcrypt), `pyotp` (TOTP 2FA), `PyJWT` (sessions). User store in SQLite.

**Pages**:
- **Dashboard**: SSE stream from event queue + InfluxDB history
- **Connectors**: Add/configure/test integrations (ingestion adapters, handlers). All connector config stored in SQLite with secrets encrypted via Fernet
- **Approval Queue**: HTTP CRUD on `PendingAction` objects
- **Event Explorer**: InfluxDB queries with filters
- **Known Fixes Browser**: View loaded fix registry (content managed via YAML files on disk)
- **Users**: Admin-only CRUD + 2FA enrollment
- **Setup Wizard**: First-run flow (admin account → core services → first integration)

**New files**: `oasisagent/ui/` (app.py, auth.py, routes/, templates/, static/), `oasisagent/db/` (schema.py, migrations/, crypto.py)

### 3.2 Messaging Integrations

**Priority order**:

1. **Telegram** (lowest friction, `aiogram` v3)
   - Inline keyboards for approve/reject
   - Rate limit: 1 msg/sec to same chat, digest low-severity events
   - Bot commands: `/status`, `/queue`, `/approve <id>`, `/reject <id>`, `/mute <duration>`
   - Runs polling loop as asyncio task alongside orchestrator

2. **Slack** (`slack-bolt` AsyncApp, Socket Mode)
   - Socket Mode = no public endpoint needed (perfect for home lab)
   - Block Kit for rich message formatting
   - Interactive components route approval decisions via callback

3. **Discord** (webhook-only for Phase 3)
   - POST embeds to webhook URL using existing `aiohttp`
   - No bot needed = zero infrastructure
   - Notification only, no interactive approval

4. **WhatsApp** — **DROPPED from Phase 3 scope** (see [ADR-001](#adr-001-whatsapp-dropped))

**InteractiveNotificationChannel ABC** (interface contract):

```python
class InteractiveNotificationChannel(NotificationChannel):
    """Notification channel that supports interactive approval responses."""

    async def send_approval_request(self, pending: PendingAction) -> str:
        """Send a message with approve/reject buttons.

        Returns a message/reference ID for tracking.
        """

    async def start_listener(
        self,
        callback: Callable[[str, ApprovalDecision], Awaitable[None]],
    ) -> None:
        """Start listening for interactive responses (button clicks, commands).

        The callback receives (action_id, decision). It is provided by the
        approval queue manager. Channels do NOT route approvals through MQTT
        unless the non-interactive path already does.
        """

    async def stop_listener(self) -> None:
        """Stop listening. Must handle disconnect + auto-reconnect with
        the same backoff pattern as other adapters. Pending approvals that
        expire during disconnect follow existing timeout rules.
        """
```

**Relationship**: `InteractiveNotificationChannel` extends `NotificationChannel` (adds interactive methods, doesn't replace). The notification dispatcher checks `isinstance` to decide whether to call `send()` or `send_approval_request()`.

### 3.3 Preventive Scanning

**Scheduling**: Simple `asyncio.sleep(interval)` loop — no APScheduler dependency needed.

**Scan types** (each produces `Event` objects with `source: "scanner"`):
- **Certificate expiry**: stdlib `ssl` module, warn at 30 days, error at 7
- **Disk space**: `shutil.disk_usage()` on mounted paths, linear extrapolation for trending
- **Backup freshness**: Check file timestamps or query backup APIs (PBS, Backblaze)
- **HA integration health**: Query integration states via existing handler
- **Docker container health**: Query via existing Docker handler
- **Service health sweeps**: HTTP health checks against all configured endpoints

**Adaptive intervals**: After finding issues, temporarily reduce scan interval (15min → 5min). Return to normal after N clean scans.

### 3.4 Learning Loop (T2→T0 Promotion)

**Trigger**: T2 returns `DiagnosisResult` with `suggested_known_fix` populated + action executes successfully + `handler.verify()` confirms fix worked.

**Process**:
1. Extract match criteria from original Event
2. Extract action from `RecommendedAction`
3. Default risk tier to `RECOMMEND` (never auto-promote to `AUTO_FIX`)
4. Write to `known_fixes/candidates/` as YAML with `_meta` block
5. Notify operator for review

**Promotion thresholds** (configurable via web UI):
- `min_confidence`: 0.8 (T2 confidence threshold)
- `min_verified_count`: 3 (must work N times before promotion candidate)
- `auto_promote`: false (human review always required in v1)

**Versioning**: File-based with `_meta.status` (candidate/promoted/rejected). No git integration — the agent never shells out to git. External git hooks can track the `candidates/` directory if desired.

**Inspiration**: Karpathy's autoresearch pattern — experiment loop with measurable objective function, human reviews accumulated results.

### 3.5 Plugin System

**Approach**: Directory-based discovery with `importlib.import_module()` + ABC validation.

**Plugin structure**:
```
plugins/
  my-handler/
    plugin.yaml          # name, version, type, requires
    handler.py           # implements Handler ABC
```

**Loading**: Scan `plugins/` at startup → parse `plugin.yaml` → import module → validate ABC compliance → register with appropriate registry.

**Error isolation**: All plugin calls wrapped in try/except + `asyncio.wait_for(timeout=...)`. A crashed plugin gets disabled, doesn't crash the agent.

**Future**: Add `entry_points` support for pip-installable plugins when the community grows.

### 3.6 Multi-Instance Coordination

**Approach**: MQTT-based leader election using retained messages + LWT.

**Protocol**:
1. Subscribe to `oasis/cluster/leader` (retained)
2. If empty or stale, publish claim with `{instance_id, timestamp}`
3. Read back — if your ID, you're leader; otherwise, standby
4. Leader publishes heartbeat every N seconds (retain=true)
5. LWT auto-fires on ungraceful disconnect

**Known limitations** (acceptable for home lab):
- Not a consensus algorithm — brief split-brain possible during partition
- Mitigated by leader_timeout lease + existing event dedup in decision engine

**Critical note**: The existing event dedup in the decision engine is load-bearing for multi-instance. Verify dedup prevents double-action in the failover test matrix.

**Fallback**: Redis `SET key value NX EX ttl` if MQTT leader election proves unreliable in production.

---

## 4. Implementation Roadmap

### v0.3.0 — Foundation + Config Backend (Weeks 1–3)
- [ ] **SQLite schema + connector CRUD API** — database models, Fernet encryption for secrets, REST endpoints for connector management
- [ ] **Config import/export CLI** — `oasisagent config import seed.yaml`, `oasisagent config export`
- [ ] **SQLite migration framework** — `schema_version` + numbered migration scripts in `migrations/`
- [ ] **First-run setup wizard API** — endpoints for admin account creation, core service config, first integration
- [ ] **Bootstrap config** — replace `config.yaml` startup with 4 env vars (`OASIS_PORT`, `OASIS_DATA_DIR`, `OASIS_SECRET_KEY`, `OASIS_LOG_LEVEL`)
- [ ] **FastAPI application scaffold** — single uvicorn process hosting API, webhook receiver, and (later) UI
- [ ] Webhook receiver ingestion adapter (routes at `/ingest/webhook/{source}`)
- [ ] HTTP polling ingestion adapter (generic, config-driven with JMESPath extraction)
- [ ] Proxmox VE handler (REST API client, known fixes)
- [ ] Docker/Portainer handler (event stream + container ops)
- [ ] MQTT topic expansion (Zigbee2MQTT, Frigate, ESPresence, Valetudo)
- [ ] Tests for all new adapters, handlers, config backend, and migrations

### v0.3.1 — Web Admin UI (Weeks 4–6)
- [ ] HTMX + Jinja2 templates + Alpine.js + Tailwind CDN
- [ ] Auth (bcrypt + TOTP + JWT in httpOnly cookies)
- [ ] Setup wizard pages (first-run flow)
- [ ] Dashboard page (SSE event stream + InfluxDB history)
- [ ] Connectors page (add/configure/test integrations — frontend to connector CRUD API)
- [ ] Approval queue page
- [ ] Event explorer page
- [ ] Known fixes browser (read-only view of YAML content)
- [ ] User management (admin-only)
- [ ] Tests for auth, routes, SSE

### v0.3.2 — Messaging (Weeks 7–8)
- [ ] `InteractiveNotificationChannel` ABC (with specified interface contract)
- [ ] Telegram channel (aiogram v3, inline keyboards, bot commands)
- [ ] Slack channel (slack-bolt, Socket Mode, Block Kit)
- [ ] Discord webhook channel
- [ ] Tests with mocked APIs

### v0.3.3 — Networking Integrations (Weeks 9–10)
- [ ] UniFi Network adapter (session auth, device/alert polling)
- [ ] Cloudflare adapter (tunnel health, WAF events)
- [ ] Servarr adapter (Radarr + Sonarr health + webhooks)
- [ ] Known fixes for all new integrations

### v0.3.4 — Preventive Scanning (Weeks 11–12)
- [ ] Scanner framework (asyncio loop, config-driven checks)
- [ ] Certificate expiry scanner
- [ ] Disk space scanner with trending
- [ ] Backup freshness scanner (PBS, Backblaze, file timestamps)
- [ ] Service health sweep scanner
- [ ] Tests

### v0.3.5 — Learning Loop (Weeks 13–14)
- [ ] Candidate fix generator (T2 → YAML)
- [ ] Confidence scoring
- [ ] Promotion workflow (file-based + notification)
- [ ] Learning config in SQLite
- [ ] Tests

### v0.3.6 — Plugin System + Multi-Instance + Tier 3 Integrations (Weeks 15–16)
- [ ] Plugin loader (directory scan, importlib, ABC validation)
- [ ] Plugin error isolation
- [ ] Plugin config schema extension
- [ ] MQTT leader election
- [ ] Standby mode
- [ ] Failover testing (verify event dedup prevents double-action)
- [ ] Remaining Tier 3 integrations (Stalwart, EMQX, Synology, N8N, Nextcloud, Ollama)
- [ ] Documentation

### v1.0.0 — Release
- [ ] Full test pass (target: 90%+ coverage)
- [ ] ARCHITECTURE.md update for all new components
- [ ] README.md update
- [ ] Migration guide from v0.2.x → v1.0
- [ ] Tag v1.0.0

---

## 5. New Ingestion Patterns

Phase 1-2 has three ingestion adapters: MQTT, HA WebSocket, HA Log Poller. Phase 3 needs two new generic adapters.

### 5.1 Webhook Receiver

Mounted as routes on the main FastAPI app (same process as web UI — **not** a separate port).

Webhook source configuration is stored in SQLite and managed via the connectors page in the web UI. Each source defines:
- Source name/identifier
- Auth method (secret header, API key verification, or none)
- Event type mapping (source event name → OasisAgent event_type + severity)

### 5.2 HTTP Poller

Configuration stored in SQLite and managed via the connectors page.

**Extraction library**: `jmespath` (well-maintained, intuitive syntax, no transitive dependencies).

**Pydantic models**:

```python
class AuthConfig(BaseModel):
    type: Literal["none", "basic", "token", "session"]
    username: Optional[str] = None
    password: Optional[SecretStr] = None   # Encrypted at rest in SQLite
    header: Optional[str] = None           # For token auth
    value: Optional[SecretStr] = None      # Token value

class ExtractMapping(BaseModel):
    entity_id: str                         # JMESPath expression
    severity_expr: Optional[str] = None    # JMESPath → severity string
    event_type: str                        # Static event type
    payload_expr: Optional[str] = None     # JMESPath → payload subset

class ThresholdConfig(BaseModel):
    value_expr: str                        # JMESPath → numeric value
    warning: float                         # Emit WARNING above this
    critical: float                        # Emit ERROR above this
    entity_id: str                         # Static or JMESPath
    event_type: str = "threshold_exceeded"

class HttpPollerTarget(BaseModel):
    name: str
    url: str
    auth: Optional[AuthConfig] = None
    interval: int = 60                     # seconds
    system: str
    mode: Literal["health_check", "extract", "threshold"] = "health_check"
    extract: Optional[ExtractMapping] = None    # for mode=extract
    threshold: Optional[ThresholdConfig] = None # for mode=threshold
```

**Response modes**:
- `health_check`: HTTP 200 = ok, anything else = emit event. No JMESPath needed.
- `extract`: Apply JMESPath expressions to response JSON, build Event from extracted fields.
- `threshold`: Extract a numeric value, emit event when it crosses a configured threshold (e.g., disk usage > 85%).

---

## 6. Known Fixes Expansion

Known fixes remain as YAML files on disk (content layer, not runtime config). Managed via git/file mount, browsed via web UI.

New YAML files to create in `known_fixes/`:

### `known_fixes/proxmox.yaml`
- `pve-vm-locked` — VM stuck in locked state after failed migration
- `pve-zfs-scrub-errors` — ZFS scrub found errors
- `pve-backup-failed` — Backup job failed (disk full, VM locked, timeout)
- `pve-storage-thin-provision` — Thin-provisioned storage >85% full
- `pve-node-unreachable` — Cluster node not responding
- `pve-ha-fence` — HA manager fenced a node
- `pbs-verification-failed` — Backup verification found corruption
- `pbs-datastore-full` — Datastore >90% used
- `pbs-gc-running-long` — Garbage collection running >2 hours

### `known_fixes/docker.yaml`
- `docker-oom-kill` — Container killed by OOM
- `docker-restart-loop` — Container restarting >3 times in 5 min
- `docker-unhealthy` — Container health check failing
- `docker-volume-mount-failed` — Volume mount error
- `docker-image-pull-failed` — Image pull failure (registry unreachable, auth)

### `known_fixes/network.yaml`
- `unifi-ap-disconnected` — Access point offline
- `unifi-switch-port-down` — Switch port link down
- `unifi-wan-failover` — Primary WAN down, failover active
- `cloudflare-tunnel-down` — Argo tunnel disconnected
- `nextdns-resolution-failure` — DNS resolution failing

### `known_fixes/media.yaml`
- `plex-database-corrupted` — Plex database corruption detected
- `plex-transcode-stall` — Transcode session speed <0.5x sustained
- `radarr-download-client-unreachable` — Download client connection failed
- `radarr-no-indexers` — All indexers unavailable
- `sonarr-disk-space-low` — Root folder below threshold
- `qbit-connection-firewalled` — qBittorrent connection status = firewalled
- `qbit-disk-full` — qBittorrent free disk space critical

### `known_fixes/services.yaml`
- `stalwart-queue-backing-up` — Mail queue depth growing
- `stalwart-delivery-failures` — Repeated delivery failures to domain
- `emqx-alarm-active` — EMQX broker alarm triggered
- `emqx-listener-down` — EMQX listener not accepting connections
- `n8n-workflow-deactivated` — Workflow deactivated after failures
- `ollama-process-crashed` — Ollama not responding (OOM)
- `nextcloud-cron-stale` — Background jobs not running
- `mariadb-replication-broken` — Slave SQL/IO thread stopped

### `known_fixes/iot.yaml`
- `z2m-bridge-offline` — Zigbee2MQTT bridge offline
- `z2m-device-offline` — Zigbee device unavailable >5 min
- `zwave-controller-crash` — Z-Wave JS driver/USB disconnect
- `zwave-dead-node` — Z-Wave node marked dead
- `frigate-camera-fps-zero` — Camera FPS dropped to 0
- `frigate-detector-overload` — Detection FPS degraded, high skipped FPS
- `wled-low-heap` — WLED free heap <10KB (instability)
- `wled-wifi-weak` — WLED WiFi signal <30%
- `valetudo-error-state` — Robot vacuum in error state
- `valetudo-consumable-expired` — Consumable at 0%
- `ecowitt-sensor-battery-low` — Weather sensor battery low
- `esphome-device-offline` — ESPHome device unreachable

---

## 7. Dependency Changes

### New Dependencies (Phase 3)

| Package | Purpose | Version | Size |
|---------|---------|---------|------|
| `fastapi` | Web UI + API + webhook receiver (single process) | >=0.115 | Core |
| `uvicorn` | ASGI server for FastAPI | >=0.32 | Core |
| `jinja2` | HTML templates | >=3.1 | Core |
| `sse-starlette` | Server-Sent Events for dashboard | >=2.0 | Tiny |
| `passlib[bcrypt]` | Password hashing | >=1.7 | Small |
| `pyotp` | TOTP 2FA | >=2.9 | Tiny |
| `PyJWT` | JWT session tokens | >=2.8 | Small |
| `aiosqlite` | Async SQLite for config + user DB | >=0.20 | Small |
| `cryptography` | Fernet encryption for secrets at rest | (transitive dep) | Already present |
| `jmespath` | HTTP poller response extraction | >=1.0 | Tiny |
| `aiogram` | Telegram bot framework | >=3.25 | Medium |
| `slack-bolt` | Slack bot framework | >=1.20 | Medium |

### Dependencies NOT Adding
| Package | Why Not |
|---------|---------|
| React/Svelte/npm | HTMX keeps project Python-only |
| APScheduler | asyncio.sleep loop is sufficient |
| Pluggy | ABC validation simpler for our plugin model |
| Redis | MQTT leader election first; Redis only as fallback |
| WhatsApp SDK | Dropped from scope (ADR-001) |
| PostgreSQL driver | SQLite sufficient for config + user storage |
| Alembic | Numbered migration scripts are sufficient |

### Existing Dependencies Leveraged
- `aiohttp` — HTTP polling adapter, Discord webhooks, all REST API clients
- `aiomqtt` — Expanded MQTT topic subscriptions
- `pydantic` — Config validation for all new sections
- `influxdb-client[async]` — Audit trail for all new event sources

---

## 8. Architecture Decision Records

### ADR-001: WhatsApp Dropped from Phase 3 Scope {#adr-001-whatsapp-dropped}

**Status**: Accepted (2026-03-10)

**Context**: Phase 3 originally included WhatsApp Business API integration as a notification channel.

**Decision**: Drop WhatsApp from Phase 3 scope entirely.

**Rationale**:
- Meta has effectively killed the self-hosted (on-premise) WhatsApp Business API. Cloud API is the only viable path.
- Message templates must go through Meta's approval process (24–48 hours per template). Every notification format change requires re-approval.
- The 24-hour messaging window means you can only send template messages to users who haven't messaged the bot recently. For a monitoring system that sends unsolicited alerts, this is a significant constraint.
- Per-message cost ($0.005–0.05 depending on region and message type) adds ongoing OpEx for a home lab tool.
- No interactive components for approvals — would be notification-only even if implemented.
- Telegram and Slack fully cover the interactive approval use case with zero per-message cost.

**Alternative**: If an operator needs WhatsApp alerts, the existing webhook notification channel can POST to WhatsApp Cloud API with minimal custom code. This doesn't need first-class integration.

**Consequences**: §17.4 in ARCHITECTURE.md should be updated to reflect this decision.

### ADR-002: UI-First Configuration (CTO Decision, 2026-03-11) {#adr-002-ui-first-config}

**Status**: Accepted

**Context**: The original Phase 3 plan continued the Phase 1-2 pattern of YAML-file-based configuration with env var interpolation.

**Decision**: Make the web UI the primary configuration surface. Runtime config lives in SQLite with Fernet-encrypted secrets. YAML config becomes optional import/export.

**Rationale**: Every comparable tool in the home lab ecosystem (Home Assistant, Grafana, Portainer, Uptime Kuma) uses a web UI for integration config. Requiring YAML editing for 40+ services with 100+ config values is a UX regression. The HA model (minimal bootstrap + UI-managed integrations) is the right pattern.

**Consequences**:
- SQLite schema + connector CRUD API must ship in v0.3.0 (before UI exists)
- `config.yaml` no longer required for startup — only 4 bootstrap env vars
- Existing `config.yaml` / `config.example.yaml` usage must have a migration path
- Secrets encrypted at rest with Fernet, decrypted in-memory only when needed

### ADR-003: Single FastAPI Process (CTO Decision, 2026-03-11) {#adr-003-single-process}

**Status**: Accepted

**Context**: The original plan ran the webhook receiver on port 9090 and the web admin UI on a separate port.

**Decision**: Run everything as a single FastAPI application, single uvicorn process, single port.

**Rationale**: Two processes doubles container port management, health checks, and process supervision for no benefit. Webhook routes, admin UI, and REST API all mount on the same app.

### ADR-004: No Git Integration in Learning Loop (CTO Decision, 2026-03-11) {#adr-004-no-git-learning}

**Status**: Accepted

**Context**: The learning loop spec included git-based versioning (commit candidates, `git mv` to promote).

**Decision**: Remove git integration. Use file-based `_meta.status` (candidate/promoted/rejected) only.

**Rationale**: The agent should never shell out to git. File-based status tracking is simpler and more portable. External git hooks can track the `candidates/` directory if desired.

---

## Research Files

Detailed API research for individual services is available in:
- `docs/research/infrastructure-service-apis.md` — Proxmox, Portainer, Synology, TuringPi, PiKVM
- `docs/research/networking-security-integrations.md` — UniFi, Cloudflare, NextDNS, fail2ban, Vaultwarden, NPM

Research summaries from agents (not persisted to disk, captured in this plan):
- Media stack: Plex, qBittorrent, Radarr, Sonarr, Stalwart, N8N, Ollama, EMQX
- Smart home/IoT: Zigbee2MQTT, Z-Wave JS UI, Frigate, ESPresence, Valetudo, Ecowitt, Hyperion, WLED, ESPHome, BirdNET
- Databases/storage/misc: MariaDB, InfluxDB, Backblaze, Mealie, Traccar, Uptime Kuma, Jupyter, Nextcloud, Stable Diffusion, Code Server
- Phase 3 features: Web UI, messaging, learning loop, preventive scanning, plugins, multi-instance
