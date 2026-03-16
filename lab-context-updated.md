# Oasis Home Lab — Claude Project Context

> **Purpose**: This file gives Claude full context about Jason's home lab infrastructure,
> monitoring stack, architectural decisions, and ongoing projects. Drop this in any
> Claude Code project root or provide it to a Cowork session to resume with full context.
>
> **Last updated**: March 15, 2026

---

## Operator Profile

- **Name**: Jason Shearer
- **GitHub**: dadcoachengineer
- **Email**: jshearer78@gmail.com
- **Working style**: Systems thinker, detail-oriented, outcome-driven. Thinks in architecture, tradeoffs, constraints, and second-order effects. Values correctness, clarity, and pragmatism. Iterates: first principles → rough design → refinement → implementation.
- **Communication preference**: Structured, skimmable answers. Be opinionated. Call out risks and hidden complexity early. Offer alternatives with tradeoffs. Treat code as production-quality. Favor maintainability, observability, extensibility.
- **Role for Claude**: Senior engineer / architect / strategist. Challenge suboptimal solutions respectfully. If a better question should be asked, ask it.

---

## Infrastructure Topology

### Network (UniFi UDMP — "Oasis")

| VLAN | Name | Subnet | Purpose |
|------|------|--------|---------|
| 1 | Infrastructure | 192.168.0.0/22 | Management — PVE nodes, Swarm VMs, NAS, db-server |
| 2 | Internet Only | 192.168.20.0/24 | Guest/isolated internet access |
| 3 | Lighting | 192.168.4.0/24 | Smart lighting (Govee, etc.) |
| 4 | No Internet | 192.168.5.0/24 | IoT isolation — no external access |
| 50 | Client | 192.168.50.0/24 | User devices — laptops, phones |
| 100 | DMZ | 192.168.100.0/24 | External-facing — Cloudflared CT 107 |
| 300 | Cisco Lab | — | Lab — third-party gateway |

### Key Devices

| Device | IP | Role |
|--------|-----|------|
| UDMP (UniFi Dream Machine Pro) | 192.168.1.1 | Gateway, UniFi Network Controller |
| UNVR (UniFi NVR) | 192.168.1.48 | UniFi Protect NVR, standalone appliance |
| Synology DS1621+ NAS | 192.168.1.101 (files.shearer.live) | NFS storage, Hyper Backup |
| pve01 (Beelink SEi12 MAX) | 192.168.1.106 | Proxmox node 1, i7-12700H, 32GB DDR5 |
| pve02 (Beelink SEi12 MAX) | — | Proxmox node 2, i7-12700H, 32GB DDR5 |
| pvedocker01 (VM 101) | 192.168.1.103 | Docker Swarm Manager (Leader), 16GB RAM, 8 vCPUs |
| pvedocker02 (VM 102) | 192.168.1.105 | Docker Swarm Manager, 16GB RAM, 8 vCPUs |
| ML01 | 192.168.1.130 | Ubuntu + RTX 4060 Ti 16GB (AI/ML, Frigate, Ollama) |
| RPi4 | (various) | Z-Wave + Zigbee radios, Telegraf edge agent |
| PBS | 192.168.1.102 | Proxmox Backup Server |
| NPM | 192.168.1.120 | Nginx Proxy Manager (intermediary for DMZ→LXC routing) |

**DNS warning**: `pve01` hostname from Docker Swarm nodes resolves to 192.168.1.102 (PBS), NOT actual Proxmox host at 192.168.1.106. Use IP addresses for cross-host operations.

### Proxmox Cluster

| ID | Type | Node | Purpose |
|----|------|------|---------|
| VM 100 | QEMU | pve01 | Home Assistant OS |
| VM 101 | QEMU | pve01 | pvedocker01 (Swarm Manager) |
| VM 102 | QEMU | pve02 | pvedocker02 (Swarm Manager) |
| CT 107 | LXC | — | Cloudflared (VLAN 100/DMZ) |
| CT 108 | LXC | pve01 | Vaultwarden (1.35.4, Debian 13, 192.168.2.100) |
| CT 109 | LXC | pve01 | Traccar + MariaDB 11.8 (6.12.2, Debian 13, 192.168.2.200) |
| CT 111 | LXC | — | EMQX (MQTT broker) |
| CT 115 | LXC | pve01 | db-server (Postgres 16 + Redis 7, 192.168.1.115) |
| CT 116 | LXC | pve01 | Immich (v2.5.6, Postgres 16, Debian 13, 192.168.2.250) |

- **pve03 PLANNED**: Beelink SEi12 MAX to restore 3-node HA quorum
- **Turing Pi 2 DECOMMISSIONED** (2026-03-04): 3x RK1 ARM64, all eMMCs dead, board powered off
- Storage per node: ISO, PBS (backup), local (vztmpl), local-zfs (images, rootdir). Both nodes cgroup-mode 2.

### Docker Swarm (2 nodes — x86_64 only)

Both nodes connect to db-server (192.168.1.115) over TCP (5432/6379) and mount Synology NFS for configs/uploads/media.

**NFS Configuration (updated 2026-03-15)**:
- Both nodes mount `192.168.1.101:/volume1/docker` at `/nfs/docker`
- fstab: `192.168.1.101:/volume1/docker /nfs/docker nfs4 auto,nofail,noatime,intr,actimeo=1800 0 0`
- NFSv4.1 with native locking (`vers=4.1`, `local_lock=none`)
- **CRITICAL**: Previously used `nfs` with `nolock` which caused SQLite corruption (BirdNET-Go). Fixed 2026-03-15.

---

## Storage Architecture — Three-Tier Model

| Tier | Storage | Location | Use Case |
|------|---------|----------|----------|
| T1 — Database | ZFS on local SSD | Proxmox LXC | MySQL, Postgres, Redis |
| T2 — App State | NFS | Synology NAS | Configs, uploads, user files |
| T3 — Bulk | NFS | Synology NAS | Media, recordings, downloads |

### DB Server (CT 115)

- 4 cores, 8GB RAM, 20GB root + 80GB ZFS data zvol at `/var/lib/dbdata`
- PostgreSQL 16 + Redis 7 (MySQL removed 2026-03-03)
- ZFS replication pve01 → pve02 every 15 min (job 115-0)
- Proxmox HA enabled, auto-migrate on node failure
- **Active databases**: Nextcloud (Postgres), Affine (Postgres), Nextcloud (Redis session/cache)
- **Redis note**: `stop-writes-on-bgsave-error no` — RDB snapshots fail in unprivileged LXC on ZFS (fork sees read-only fs)
- Nightly backup cron on both PVE hosts via `pct exec`, 14-day retention

### Migrated Services (off NFS, onto local ZFS)

| Service | Target | Date | Notes |
|---------|--------|------|-------|
| Vaultwarden | CT 108 (192.168.2.100) | Feb 2026 | SQLite on local ZFS, HA + replication |
| Traccar | CT 109 (192.168.2.200) | Mar 2026 | Self-contained MariaDB 11.8.3 |
| Immich | CT 116 (192.168.2.250) | Mar 2026 | Self-contained Postgres 16, privileged LXC for NFS mount |
| Nextcloud DB | CT 115 (192.168.1.115) | Mar 2026 | App stays in Swarm, DB on CT 115 |

### Still on NFS (acceptable risk or pending migration)

| Service | DB Type | Status |
|---------|---------|--------|
| BirdNET-Go | SQLite | **REPAIRED** (2026-03-15). Host bind mounts, NFSv4.1 locking |
| Affine | Postgres | Pending migration to CT 115 |
| Uptime Kuma | SQLite | Low risk, keep on NFS |
| Homarr | SQLite | Low risk, keep on NFS |
| InfluxDB (TIG) | Custom | Deployed on NFS (2026-03-04) |

### NFS Volume Patterns (three inconsistent, standardization pending)

| Pattern | Stacks |
|---------|--------|
| Named NFS volume with hostname (`files.shearer.live`) | govee2mqtt, homepage, frigate, ai-stack |
| Named NFS volume with IP (`192.168.1.101`) | emqx, fail2ban, homeassistant-swarm |
| Bind mount to fstab NFS (`/nfs/docker/...`) | traccar, code-server, qbittorrentvpn, tdarr, birdnet-go |

Target: Standardize all to named NFS volumes with DNS hostname + `nfsvers=4.1,hard,intr`.

---

## Monitoring Stack Architecture

### Data Flow

```
Telegraf (system metrics) ──────► InfluxDB v2 (bucket: telegraf)
                                      │
Unpoller (UniFi metrics) ──────► InfluxDB v2 (bucket: unpoller)
                                      │
npmgrafstats (NPM logs)  ──────► InfluxDB v2 (bucket: npmgrafstats)
                                      │
                                      ▼
                                   Grafana
```

### TIG Stack (deployed 2026-03-04)

- **InfluxDB**: `influxdb:2.7`, org `oasis`, buckets: `telegraf` (90-day retention), `npmgrafstats`, `unpoller`
- **Telegraf**: `telegraf:1.33`, global mode on both Swarm nodes (system/Docker/ping/HTTP inputs)
- **Grafana**: `grafana/grafana:11.4.0`, auto-provisioned with 3 datasources

### Datasource UIDs (Grafana)

| Datasource | UID | Bucket | Type |
|-----------|-----|--------|------|
| Telegraf | `PF6CD3C7EE64CF7D1` | telegraf | InfluxDB Flux |
| Unpoller | `bff2ieydhhj40e` | unpoller | InfluxDB Flux |

### Critical Grafana Patterns (learned the hard way)

1. **Datasource references MUST be objects**: `{"type": "influxdb", "uid": "PF6CD3C7EE64CF7D1"}`, never strings like `"Telegraf"`
2. **Query key is `query` + `language: "flux"`**, NOT `expr` (that's Prometheus)
3. **Color modes**: Valid values are `fixed`, `shades`, `thresholds`, `palette-classic`. The value `"background"` is NOT valid for `fieldConfig.defaults.color.mode` and will crash the entire dashboard page
4. **UID caching**: Same dashboard UID won't update on re-import. Bump UIDs when making changes (e.g., `system-health-v7` → `system-health-v8`)
5. **Clean legends with `keep()`**: Grafana concatenates ALL non-time/value columns into legend labels. Always use `keep(columns: ["_time", "_value", "<display_tag>"])` to strip metadata
6. **`map()` loses group keys**: Creating new records with `map(fn: (r) => ({...}))` loses Flux group keys. Use `{r with ...}` to preserve them, or re-`group()` after
7. **stat panels with per-entity cards**: Use `group(columns: ["entity_tag"])` + `last()` + `keep()` + re-`group()` to get one card per entity

### InfluxDB Measurements

**Telegraf bucket (`telegraf`)**:
- `cpu`, `mem`, `disk`, `diskio`, `net`, `system` — standard host metrics
- `docker`, `docker_container_*` — Docker container metrics
- `proxmox_node`, `proxmox_vm`, `proxmox_ct` — Proxmox metrics (note: CPU is float 0-1, multiply by 100 for %)
- `emqx_vm_cpu_use`, `emqx_vm_memory`, `emqx_connections`, `emqx_messages_*` — EMQX broker metrics (tag: `source=emqx`)
- `http_response` — Service liveness checks (tag: `server=<service_name>`)
- `stalwart_*` — Mail server metrics
- `synology_*` — NAS metrics
- `protect_nvr`, `protect_camera` — UNVR/Protect metrics (via exec script, not yet deployed)

**Unpoller bucket (`unpoller`)**:
- `clients` — Connected client metrics
- `subsystems` — Controller subsystem health
- `uap`, `uap_radios`, `uap_vaps` — Access point metrics
- `usg`, `usg_networks`, `usg_wan_ports` — Gateway metrics
- `usw`, `usw_ports` — Switch metrics
- `wan` — WAN performance metrics
- **NO protect/camera measurements** — Unpoller doesn't collect these for InfluxDB

### Flux Query Patterns (Reference)

```flux
// Host CPU (inverted idle → usage, hostname as legend)
from(bucket: "telegraf")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "cpu")
  |> filter(fn: (r) => r["_field"] == "usage_idle")
  |> filter(fn: (r) => r["cpu"] == "cpu-total")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({_time: r._time, _value: 100.0 - r._value, _field: r.host}))
  |> group(columns: ["_field"])

// Proxmox CPU (float 0-1 → percentage, grouped by node)
from(bucket: "telegraf")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "proxmox_node")
  |> filter(fn: (r) => r["_field"] == "cpu")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({_time: r._time, _value: r._value * 100.0, _field: r.node}))
  |> group(columns: ["_field"])

// WAN Throughput (interface + direction labels)
from(bucket: "unpoller")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "usg_wan_ports")
  |> filter(fn: (r) => r["_field"] == "rx_bytes-r" or r["_field"] == "tx_bytes-r")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value", "_field", "ifname"])
  |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: r.ifname + " " + (if r._field == "rx_bytes-r" then "RX" else "TX")}))
  |> group(columns: ["_field"])

// Service Health Grid (per-service stat cards)
from(bucket: "telegraf")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "http_response")
  |> filter(fn: (r) => r["_field"] == "http_response_code")
  |> group(columns: ["server"])
  |> last()
  |> keep(columns: ["_time", "_value", "server"])
  |> group(columns: ["server"])
```

---

## Dashboards Built

All dashboards are Grafana JSON files. Current working versions:

| Dashboard | File | UID | Datasource |
|-----------|------|-----|------------|
| System Health Overview | `system-health-v8.json` | `system-health-v8` | Telegraf |
| UniFi Overview | `unifi-overview-dashboard.json` | v4 | Unpoller |
| UniFi Switches | `unifi-switches-dashboard.json` | v4 | Unpoller |
| UniFi Wireless | `unifi-wireless-dashboard.json` | v2 | Unpoller |
| UniFi Clients | `unifi-clients-dashboard.json` | v2 | Unpoller |
| UDMP | `udmp-dashboard.json` | — | Unpoller |
| Infrastructure Health | `infra-health-dashboard.json` | — | Telegraf |
| Docker | `docker-dashboard.json` | — | Telegraf |
| Proxmox | `proxmox-dashboard.json` | — | Telegraf |
| DB Services | `db-services-dashboard-v2.json` | v2 | Telegraf |
| EMQX | `emqx-dashboard.json` | — | Telegraf |
| Stalwart Mail | `stalwart-dashboard.json` | — | Telegraf |
| Synology NAS | `synology-dashboard.json` | — | Telegraf |
| Home Assistant | `homeassistant-dashboard.json` | — | Telegraf |
| UNVR / Protect | `unvr-protect-dashboard.json` | `unvr-protect-v1` | Telegraf |

---

## UNVR / Protect Monitoring

Unpoller does NOT support Protect metrics for InfluxDB — only Loki event log forwarding exists.

### Solution: Telegraf exec + Protect API

- `protect-metrics.sh` — Shell script that authenticates to the UNVR Protect API (`/proxy/protect/api/bootstrap`), parses JSON with `jq`, outputs InfluxDB line protocol
- Measurements: `protect_nvr` (NVR health, storage, camera counts) and `protect_camera` (per-camera status, uptime, recording state, bitrate, FPS)

**Prerequisites (not yet completed)**: Create local read-only user on UNVR at 192.168.1.48, set `UNVR_USER` and `UNVR_PASS` env vars, install `jq` and `bc` on Telegraf host.

---

## Active Project: oasis-agent

### What It Is

Autonomous infrastructure operations agent for home labs. Detects failures via multiple ingestion sources, classifies them using three-tier LLM reasoning (T0 lookup → T1 local SLM → T2 cloud model), and auto-remediates or escalates with full context.

**Repo**: https://github.com/dadcoachengineer/oasisagent
**Current version**: v0.2.7 (as of March 15, 2026)
**License**: MIT (public open-source)
**Stack**: Python 3.11+, fully async (asyncio), Pydantic, LiteLLM, aiomqtt, aiohttp, FastAPI

### Current Phase: Phase 3 — DB-First Config + Web UI

Phase 1 (core framework) and Phase 2 (LLM integration, Docker handler, notifications) are complete.

Phase 3 focuses on:
- SQLite as runtime source of truth (DB-first configuration)
- FastAPI web UI for configuration management
- TypeMeta registry pattern for mapping DB types to Pydantic models
- Orchestrator two-phase startup: `_build_infrastructure()` (sync, config-driven singletons) + `_build_db_components()` (async, DB-driven user components)
- **Active issue #210**: Migrating from YAML/config-driven component building to DB-first component building in the orchestrator. Plan reviewed and approved 2026-03-15.

### Architecture

```
EMQX (event bus) → oasis-agent → HA REST API (read config, apply fix)
                        │        → Docker API (restart containers)
                        │        → Proxmox API (restart VMs/CTs)
                        │        → LLM Client (diagnose novel failures)
                        ▼
                    InfluxDB (audit log) → Grafana (remediation dashboard)
```

### Key Design Decisions

1. **Three-tier reasoning**: T0 known fixes via YAML lookup (fast path, no LLM). T1 local SLM for triage/classification. T2 cloud model for novel failures. LLM is fallback, not hot path.
2. **Risk tiers**: AUTO-FIX (low risk, known patterns) → RECOMMEND (medium risk, notify with diagnosis) → ESCALATE (high risk, don't touch) → BLOCK (security systems, never auto-fix)
3. **Circuit breaker**: Max 3 attempts/entity/hour. 15-min cooldown between retries. Global kill switch at 30% failure rate.
4. **Blocked domains**: `lock.*`, `alarm_control_panel.*`, `camera.*`, `cover.*` — never auto-remediated
5. **Handler plugin model**: Each managed system is a handler module. Same interface: receive event → fetch context → diagnose → decide → act → verify → audit
6. **LLM client is role-based**: Code calls `llm.complete(role=LLMRole.TRIAGE, ...)` — never LiteLLM directly
7. **Guardrails are code, not prompts**: Risk tiers, blocked domains, and circuit breaker logic live in the decision engine
8. **DB-first config (Phase 3)**: Three-layer config model: Bootstrap (env vars) → Runtime (SQLite) → Content (files). UI-first configuration directive.

### Key Files

| Path | Purpose |
|------|---------|
| `oasisagent/orchestrator.py` | Core startup, component lifecycle, restart logic |
| `oasisagent/db/registry.py` | TypeMeta registry — maps DB types to Pydantic models and classes |
| `oasisagent/db/schema.py` | SQLite migration system, WAL mode |
| `oasisagent/db/config_store.py` | CRUD operations, secret encryption, config loading |
| `ARCHITECTURE.md` | Authoritative design spec — read before implementing anything |
| `PHASE3-CTO-REVIEW.md` | CTO review, approved with required changes |

---

## ML01 — AI/ML Workstation

| Component | Part |
|-----------|------|
| CPU | Intel Core i7-12700K (8P+4E cores, 125-190W) |
| Motherboard | ASUS ROG Strix B760-I Gaming WiFi (Mini-ITX) |
| GPU | NVIDIA RTX 4060 Ti 16GB (165W) |
| RAM | ~31GB DDR5 |
| Storage | 1.8TB NVMe |
| Cooler | Noctua NH-L9i-17xx (LOW-PROFILE, rated ~65W — UNDERSIZED) |
| Case | SilverStone SG13B-Q (Mini-ITX, 61mm max cooler) |

**Thermal issue**: CPU hits 87C under load (Frigate + Ollama). NH-L9i rated ~65W vs CPU pulling 125-190W. Thermally constrained.

**GPU allocation**: Ollama (5.3GB) + Frigate embeddings (1GB) + ONNX detectors (432MB) + ffmpeg decode x10 (1.6GB) = ~8.3GB / 16GB VRAM. GPU at 99% utilization.

**iGPU disabled**: Enabling Intel UHD 770 caused NVMe boot failure (suspected PCIe lane sharing on Mini-ITX board). Reverted.

**Upgrade path**: Phase 1 (case+cooler swap ~$220) → Phase 2 (ATX board+PSU ~$400) → Phase 3 (RTX 5090 ~$2000+)

---

## Key Corrections & Hard-Won Lessons

### NFS & Storage
- **NFS `nolock` corrupts SQLite** — Both Swarm nodes had `nolock` in fstab, disabling POSIX file locking. Fixed 2026-03-15: NFSv4.1 with native locking. BirdNET-Go's `birdnet.db` was unrecoverable.
- **Grafana SQLite on NFS** causes `database is locked` during rapid restarts. `GF_DATABASE_WAL=true` mitigates.
- **Grafana runs as UID 472** — NFS dirs must be `chown 472:472` or Grafana can't start.
- **NFS inside privileged LXC** works via `/etc/fstab`. Synology NFS needs "No mapping" squash + "Allow users to access mounted subfolders".
- **NFS inside unprivileged LXC** does NOT work (UID mapping). Backups run on PVE host via `pct exec`.

### Docker Swarm
- **Swarm does NOT rebalance at runtime** — placement-only, fire-and-forget. Use `--reserve-cpu` / `--reserve-memory` for scheduling hints.
- **swarm-launcher has no `LAUNCH_MEMORY`** — mount custom compose at `/docker-compose.yml` inside launcher for child container limits.
- **qbittorrent-nox memory leak** — grows unbounded to 10GB+. Capped at 4GB via compose `mem_limit`.
- **Apache prefork `MaxRequestWorkers: 150`** too high for Nextcloud on shared VM. Tuned to 32 workers.

### Proxmox & LXCs
- **Redis RDB snapshots fail in unprivileged LXC on ZFS** — `stop-writes-on-bgsave-error no` workaround.
- **UFW rule deletion shifts numbering** — always re-verify `ufw status numbered` after deletions.
- **Cloudflared LXC (CT 107)** is on VLAN 100 (DMZ) — cannot reach 192.168.2.x directly. Routes through NPM.

### Credentials & Security
- **PIA VPN credentials** were exposed in `docker service inspect`. Rotated 2026-03-04.
- **`openssl rand -base64`** produces shell-unsafe characters — use `openssl rand -hex 16` for env vars.
- **Nextcloud config.php** persists DB settings from initial install — Docker env vars do NOT override on restart.
- **GitOps credential rotation** in progress — see Notion checklist. Red items (Stripe, GitHub PAT, Docker Hub PAT) still pending.

### TIG Stack
- **InfluxDB v2 ignores v1 env vars** — must use `DOCKER_INFLUXDB_INIT_*` prefix.
- **Telegraf config was empty** on NFS mount — had to write `telegraf.conf` from scratch.
- **Grafana datasource tokens** need `INFLUX_TOKEN` in Grafana's own environment for provisioning interpolation.

---

## Backlog (prioritized)

### High
- [ ] Third PVE node (pve03) — restore 3-node HA quorum
- [x] TIG stack deployed (2026-03-04)
- [x] BirdNET-Go repaired (2026-03-15)
- [ ] Affine DB migration to CT 115
- [ ] OasisAgent #210 — DB-first component building (plan approved)

### Medium
- [ ] NFS volume standardization (three patterns → one)
- [ ] Migrate all Docker NFS volumes to host bind mounts (future project)
- [ ] Nextcloud FPM migration (Apache → PHP-FPM + nginx)
- [ ] Swarm resource reservations for heavy services
- [ ] ML01 Phase 1 upgrade (case + cooler)
- [ ] GitOps credential rotation (Red items still pending)

### Low
- [ ] Remove decommissioned stacks from GitHub repo
- [ ] Archive old NFS database directories
- [ ] UNVR Protect monitoring deployment
- [ ] Flush swap on pvedocker01

---

## Notion Workspace (Home Lab section)

| Page | URL | Purpose |
|------|-----|---------|
| Home Lab (parent) | `31593bcfa9c980e09f4cf08f9179331b` | Top-level container |
| Storage Architecture | `31593bcfa9c9813daec7cc9d4859a34d` | Tiered storage plan, stack audit, migration playbook, backlog |
| Docker Swarm Runbook | `30e93bcfa9c9818191d5eb40cba1f2e1` | Portainer compose, topology, incident log |
| GitOps Credential Rotation | `30e93bcfa9c981a6a6fcd6191375bd39` | Secret extraction and rotation checklist |
| DB Server Runbook (v2) | `31593bcfa9c98141bdc2d96031bf4b2d` | CT 115 as-built, corrections, config files |
| DB Server Runbook (v1) | `31593bcfa9c9810fbe23d26f8593bd3e` | Original plan (pre-build) |
| Frigate NVR | `31c93bcfa9c98167bf5bc736042b9ae4` | Configuration, hardware acceleration |
| ML01 Hardware | `32193bcfa9c9810f9a5bd9291d9df522` | Thermal analysis, GPU allocation, upgrade plan |

---

## File Inventory

### Monitoring Configs

| File | Purpose |
|------|---------|
| `rpi4-telegraf.conf` | Telegraf config for RPi4 remote agent |
| `rpi4-telegraf-compose.yml` | Docker Compose for RPi4 Telegraf |
| `unvr-telegraf.conf` | Telegraf config for UNVR/Protect monitoring |
| `protect-metrics.sh` | Protect API polling script (InfluxDB line protocol output) |

### Project Artifacts

| File | Purpose |
|------|---------|
| `oasis-agent-prd.docx` | Full PRD for the autonomous operations agent |
| `lab-context.md` | This file — portable project context |
