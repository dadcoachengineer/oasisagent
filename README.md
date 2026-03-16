<p align="center">
  <img src="docs/banner.svg" alt="OasisAgent — Autonomous infrastructure ops for home labs" width="800"/>
</p>

<p align="center">
  <a href="https://github.com/dadcoachengineer/oasisagent/actions"><img src="https://github.com/dadcoachengineer/oasisagent/actions/workflows/docker-publish.yml/badge.svg" alt="Build"></a>
  <a href="https://github.com/dadcoachengineer/oasisagent/pkgs/container/oasisagent"><img src="https://img.shields.io/badge/ghcr.io-oasisagent-blue?logo=docker" alt="GHCR"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/dadcoachengineer/oasisagent" alt="License"></a>
  <a href="https://github.com/dadcoachengineer/oasisagent/issues"><img src="https://img.shields.io/github/issues/dadcoachengineer/oasisagent" alt="Issues"></a>
</p>

<p align="center">
  Detects failures. Classifies with tiered LLM reasoning. Auto-remediates or escalates with full context.
</p>

---

OasisAgent bridges the gap between **monitoring** (you know something broke) and **resolution** (it's fixed). It sits alongside your existing stack — not inside it — and closes that gap automatically for known issues, and with intelligent diagnosis for novel ones.

<p align="center">
  <img src="docs/pipeline.svg" alt="OasisAgent data and analytics pipeline" width="800"/>
</p>

## Three-Tier Reasoning

| Tier | What it does | Latency | Cost |
|------|-------------|---------|------|
| **T0 — Known Fixes** | Pattern match against a YAML registry of 19 fix files. No LLM. | <1ms | Free |
| **T1 — Triage** | Local SLM classifies events, filters noise, packages context for T2. | 100-500ms | Your hardware |
| **T2 — Diagnosis** | Cloud model diagnoses novel failures with dependency-aware context, recommends multi-step remediation plans. | 5-45s | Per-token |

T0 handles the common cases instantly. T1 runs on your own hardware (Ollama, LM Studio, vLLM). T2 is invoked only when needed — Claude, GPT, Gemini, or any OpenAI-compatible endpoint via [LiteLLM](https://github.com/BerriAI/litellm).

### Advanced Reasoning (T2)

When T2 is invoked, it doesn't work blind. The decision engine assembles rich context before the LLM call:

- **Dependency-aware context** — A service topology graph tracks how your infrastructure is connected. BFS subgraph extraction gives T2 upstream/downstream/same-host dependency context for every event.
- **Multi-handler context assembly** — Each handler contributing context for an entity is called in parallel (5s timeout per handler, failures never block the pipeline).
- **Cross-domain correlation** — A background correlator detects incidents spanning multiple systems within a sliding window. Same-host, dependency-chain, and network-device rules cluster related events so T2 sees the full picture.
- **Plan-aware dispatch** — T2 can return multi-step remediation plans with dependency ordering. A `PlanExecutor` state machine runs steps sequentially, verifies each via handler, and persists every transition to SQLite. Plans resume across restarts.
- **Learning loop** — When T2 suggests a known fix pattern with high confidence, the learning loop writes it as a candidate YAML file and tracks occurrences. After reaching a configurable threshold, the operator is notified for promotion to the T0 registry.

## Safety Guardrails

All enforced in deterministic code — never in LLM prompts.

- **Risk tiers**: `AUTO_FIX` | `RECOMMEND` | `ESCALATE` | `BLOCK`
- **Blocked domains**: Security systems (locks, alarms, cameras) permanently excluded
- **Circuit breaker**: Max 3 attempts per entity per hour, global kill switch at 30% failure rate
- **Dry-run mode**: Log every decision without executing anything
- **Approval queue**: `RECOMMEND` actions require operator approval before execution — approve via web UI, Telegram inline buttons, Slack, or MQTT
- **Plan-level approval**: Multi-step plans with any non-`AUTO_FIX` step require whole-plan approval

## Quick Start

```bash
docker pull ghcr.io/dadcoachengineer/oasisagent:latest
docker run -d -p 8080:8080 \
  -v oasis-data:/data \
  ghcr.io/dadcoachengineer/oasisagent:latest
```

Open `http://localhost:8080` and the setup wizard walks you through:
1. Create an admin account
2. Configure core services (LLM endpoints, Home Assistant, audit)
3. Add your first integration

All configuration is managed through the web UI. Secrets are encrypted at rest in SQLite with Fernet encryption. No YAML editing required.

### Docker Compose

For a full stack with bundled InfluxDB:

```bash
git clone https://github.com/dadcoachengineer/oasisagent.git
cd oasisagent
cp .env.example .env    # fill in your values
docker compose --profile monitoring up -d
```

See [Deployment Options](#deployment-options) for Swarm and from-source setups.

### Prerequisites

| Service | Required | Notes |
|---------|----------|-------|
| Home Assistant | Recommended | Long-lived access token for state monitoring and remediation |
| MQTT broker | Recommended | EMQX, Mosquitto — for MQTT ingestion and notifications |
| Cloud LLM API key | Recommended | T2 reasoning — Claude, GPT, Gemini, or any OpenAI-compatible endpoint |
| Local LLM (Ollama) | Optional | T1 triage — or use a cloud endpoint via OpenRouter |
| InfluxDB v2 | Optional | Audit trail storage (every event, decision, and action is recorded) |

No single service is mandatory — OasisAgent adapts to what you have. Start with one integration and add more through the web UI.

## Supported Systems

### Ingestion Adapters (24)

| System | Adapter | Handler | Capabilities |
|--------|---------|---------|-------------|
| Home Assistant | HA WebSocket, Log Poller | **Live** | State monitoring, automation errors, log analysis, integration restarts, service calls |
| Docker/Portainer | Portainer API | **Live** | Container health, restart, logs, OOM/crash, stack status, resource monitoring |
| Proxmox VE | REST API | **Live** | Node status, VM/CT management, task monitoring, replication, ZFS health |
| UniFi Network | REST API | **Live** | Device status, AP health, UDMP telemetry, WAN failover, client blocking |
| Cloudflare | REST API | **Live** | Tunnel health, WAF events, SSL certificates, cache purge, DNS, IP blocking |
| Radarr/Sonarr/Prowlarr/Bazarr | REST API | — | Download health, indexer status, disk space, queue errors |
| Plex Media Server | REST API | — | Server reachability, library availability |
| Tautulli | REST API | — | Plex monitoring via Tautulli — server status, bandwidth |
| qBittorrent | REST API | — | Download health, errored/stalled torrents, connectivity |
| Tdarr | REST API | — | Transcoding worker health, queue progress |
| Overseerr | REST API | — | Request processing, server availability |
| Frigate NVR | REST API | — | Camera health, detection events, recording status |
| N8N | REST API | — | Workflow execution health, failed run detection |
| Nginx Proxy Manager | REST API | — | Proxy host status, SSL certificates, dead host detection |
| Vaultwarden | REST API | — | Service health, deep health with degraded detection and response time tracking |
| Uptime Kuma | Prometheus `/metrics` | — | Monitor status, response time thresholds, certificate expiry |
| Stalwart Mail | REST API | — | Mail server health, queue monitoring |
| Ollama | REST API | — | LLM server health, loaded model status |
| EMQX | REST API | — | MQTT broker health, alarms, listener status |
| Nextcloud | OCS API | — | Server health, cron staleness, maintenance mode |
| MQTT | Subscriber | — | Any MQTT topic — Zigbee2MQTT, Frigate, ESPresense, Valetudo, custom sensors |
| HTTP Poller | Generic | — | Any REST API — health checks, JMESPath extraction, threshold alerts |
| Webhook Receiver | HTTP POST | — | Push-based ingestion from any service |

### Preventive Scanners (5)

Run on configurable intervals with adaptive scheduling (faster polling after detecting issues):

- **Certificate Expiry** — TLS certificate checks with warning/critical thresholds
- **Disk Space** — Filesystem usage monitoring
- **Backup Freshness** — PBS and local backup age detection
- **HA Health** — Integration error state detection
- **Docker Health** — Unhealthy, exited, or restarting container detection

### Notification Channels (8)

| Channel | Interactive | Notes |
|---------|-------------|-------|
| Web UI | Approve/reject in dashboard | Built-in, always available |
| Telegram | Inline keyboard buttons | Bot token required |
| Slack | Webhook | Block Kit buttons planned |
| Discord | Webhook | Rich embeds |
| MQTT | Topic-based | For CLI/automation consumers |
| Email | — | SMTP |
| Webhook | — | HTTP POST JSON |

## Configuration

OasisAgent uses a **three-layer configuration model**:

| Layer | What | Where | Managed By |
|-------|------|-------|------------|
| **Bootstrap** | Port, data dir, secret key, log level | 4 env vars | Docker / orchestrator |
| **Runtime** | All integrations, handlers, notifications, scanners | SQLite (secrets encrypted at rest) | Web UI + REST API |
| **Content** | Known fix patterns, prompt templates | YAML files on disk | Volume mount / git |

### Bootstrap Environment Variables

Only 4 env vars are needed to start the agent:

| Variable | Default | Notes |
|----------|---------|-------|
| `OASIS_PORT` | `8080` | Web UI + API port |
| `OASIS_DATA_DIR` | `/data` | SQLite database and state |
| `OASIS_SECRET_KEY` | Auto-generated | Fernet key for secret encryption |
| `OASIS_LOG_LEVEL` | `info` | debug, info, warning, error |

Everything else — integrations, LLM endpoints, notification channels, scanners — is configured through the web UI after first launch.

### Legacy Environment Variables

For backward compatibility and CI/headless deployments, the agent also reads the full set of legacy env vars (`HA_TOKEN`, `MQTT_PASS`, `REASONING_LLM_API_KEY`, etc.). See [`.env.example`](.env.example) for the complete reference. If SQLite has no config and a `config.yaml` is present, it seeds from YAML on first boot.

## Deployment Options

### Docker Compose (single node)

```bash
cp .env.example .env   # fill in your values
docker compose up -d
```

To include bundled InfluxDB for audit logging:

```bash
docker compose --profile monitoring up -d
```

### Docker Swarm

```bash
docker stack deploy -c docker-compose.yml oasis
```

Set environment variables in your orchestrator (Portainer stack UI, etc.) or export them before deploying. The compose file includes deploy constraints, resource limits (512MB), and rolling update policy.

### From Source

```bash
git clone https://github.com/dadcoachengineer/oasisagent.git
cd oasisagent
pip install -e ".[dev]"
python -m oasisagent
```

## Web Admin UI

The HTMX-powered web interface provides full operational control:

- **Dashboard** — Live event feed, system stats, quick actions
- **Connectors** — Add/edit/delete any of the 24 ingestion adapters with guided forms
- **Services** — Configure LLM endpoints, handlers, InfluxDB, guardrails, circuit breaker, scanners
- **Notifications** — Email, Telegram, Slack, Discord, MQTT, Webhook channel management
- **Approvals** — Pending action queue with approve/reject for single actions and multi-step plans
- **Events** — Searchable event log with filtering by source, system, type, severity, and time range
- **Service Map** — Interactive topology graph showing service dependencies and health state
- **Known Fixes** — Browse and manage the T0 fix registry
- **Setup Wizard** — First-run guided setup for admin account, core services, and first integration
- **Dark Mode** — System-aware theme with manual toggle

## Observability

- **InfluxDB audit trail** — Every event, decision, action, and verification recorded with full context
- **Prometheus metrics** — `/metrics` endpoint for real-time alerting (events processed, queue depth, processing latency, LLM call duration)
- **Grafana dashboards** — Import [`dashboards/oasisagent-overview.json`](dashboards/) for event volume, decision distribution, and action results
- **Web dashboard** — Real-time stats and event feed in the admin UI

## Known Fixes Registry

The `known_fixes/` directory contains 19 YAML files covering all supported systems — instant, zero-cost resolution at the T0 tier:

```yaml
fixes:
  - id: ha-deprecated-kelvin
    match:
      system: homeassistant
      event_type: automation_error
      payload_contains: "kelvin"
    diagnosis: "HA deprecated 'kelvin' in favor of 'color_temp_kelvin'"
    action:
      type: recommend
      handler: homeassistant
      operation: notify
```

Each system has an **escalation ladder**: specific component fixes first, then broader coordinator patterns, then T1/T2 reasoning for novel failures. Contributing known fixes is the easiest way to improve OasisAgent — if T2 diagnosed something for you, add it to the registry so it resolves instantly next time.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design specification — data models, component interfaces, configuration schema, and the complete decision engine pipeline.

**Key modules:**

| Module | Purpose |
|--------|---------|
| `oasisagent/orchestrator.py` | Main event loop, component wiring, lifecycle management |
| `oasisagent/engine/decision.py` | Three-tier decision engine (T0/T1/T2 + guardrails) |
| `oasisagent/engine/plan_executor.py` | Multi-step remediation state machine |
| `oasisagent/engine/service_graph.py` | Service topology graph + dependency BFS |
| `oasisagent/engine/cross_correlator.py` | Cross-domain event correlation |
| `oasisagent/engine/context_assembly.py` | Multi-handler context gathering for T2 |
| `oasisagent/engine/learning.py` | T2-to-T0 candidate fix promotion |
| `oasisagent/db/config_store.py` | SQLite config backend with Fernet encryption |
| `oasisagent/web/app.py` | Single-process FastAPI (web UI + API + webhooks) |

## Contributing

Contributions welcome! The most impactful areas:

1. **Known fixes** — Add YAML entries for failure patterns you've encountered
2. **Integrations** — New adapters for services in your infrastructure stack
3. **Notification channels** — New alert/approval surfaces
4. **Bug reports** — [Open an issue](https://github.com/dadcoachengineer/oasisagent/issues) with reproduction steps

```bash
# Development setup
git clone https://github.com/dadcoachengineer/oasisagent.git
cd oasisagent
pip install -e ".[dev]"

# Run tests (3000+)
pytest

# Lint
ruff check .
```

Please open an issue before starting work on major features to discuss the approach.

## Roadmap

| Version | Scope | Status |
|---------|-------|--------|
| v0.1.x | Core framework — ingestion, decision engine, HA handler, known fixes, audit, circuit breaker | Complete |
| v0.2.x | T2 cloud reasoning, approval queue, verification loop, event correlation, notifications, Grafana, Prometheus | Complete |
| v0.3.0 | SQLite config backend, FastAPI, webhook receiver, HTTP poller, Proxmox + Portainer handlers | Complete |
| v0.3.1 | Web admin UI — HTMX dashboard, setup wizard, connectors, approval queue, event explorer | Complete |
| v0.3.2 | Telegram (interactive), Discord, Slack notification channels | Complete |
| v0.3.3 | UniFi + Cloudflare integrations, service topology, cross-domain correlation | Complete |
| v0.3.4 | Preventive scanners — certificates, disk, backups, HA health, Docker health | Complete |
| v0.3.5 | Dependency-aware T2 context, multi-handler context assembly, plan-aware dispatch, event timeline | Complete |
| **v1.0.0** | **Stable release — develop→main merge, learning loop, 4 new adapters, Vaultwarden deep health, dark mode** | **Current** |

## License

MIT — see [LICENSE](LICENSE) for details.
