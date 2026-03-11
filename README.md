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
| **T0 — Known Fixes** | Pattern match against a YAML registry. No LLM. | <1ms | Free |
| **T1 — Triage** | Local SLM classifies events, filters noise, packages context. | 100-500ms | Your hardware |
| **T2 — Diagnosis** | Cloud reasoning model diagnoses novel failures, recommends actions. | 5-45s | Per-token |

T0 handles the common cases instantly. T1 runs on your own hardware (Ollama, LM Studio, vLLM). T2 is invoked only when needed — Claude, GPT, Gemini, or any OpenAI-compatible endpoint via [LiteLLM](https://github.com/BerriAI/litellm).

## Safety Guardrails

All enforced in deterministic code — never in LLM prompts.

- **Risk tiers**: `AUTO_FIX` | `RECOMMEND` | `ESCALATE` | `BLOCK`
- **Blocked domains**: Security systems (locks, alarms, cameras) permanently excluded
- **Circuit breaker**: Max 3 attempts per entity per hour, global kill switch at 30% failure rate
- **Dry-run mode**: Log every decision without executing anything
- **Approval queue**: `RECOMMEND` actions require operator approval (web UI, Telegram, Slack, or MQTT) before execution

## Quick Start

```bash
git clone https://github.com/dadcoachengineer/oasisagent.git
cd oasisagent
cp .env.example .env
# Edit .env with your HA token, MQTT credentials, LLM keys, etc.
docker compose up -d
```

All configuration is driven by environment variables. Docker Compose loads them from the `.env` file automatically. The same `docker-compose.yml` works for both single-node (`docker compose up`) and Swarm (`docker stack deploy`) deployments. See [Deployment Options](#deployment-options) for details.

### Prerequisites

| Service | Required | Notes |
|---------|----------|-------|
| Home Assistant | Yes | Long-lived access token required |
| MQTT broker | Yes | EMQX, Mosquitto, or any standard broker |
| InfluxDB v2 | Recommended | Audit trail storage |
| Local LLM (Ollama) | Recommended | T1 triage — or use a cloud endpoint |
| Cloud LLM API key | Optional | T2 reasoning — Claude, GPT, Gemini, OpenRouter |

## Supported Systems

| System | Status | Capabilities |
|--------|--------|-------------|
| Home Assistant | **Live** | State monitoring, automation errors, log analysis, integration restarts, service calls |
| Docker/Portainer | v0.3.0 | Container health, restart, logs, OOM/crash detection via Portainer API |
| Proxmox VE/PBS | v0.3.0 | VM/CT management, node monitoring, backup verification, ZFS health |
| Radarr/Sonarr | v0.3.3 | Download health, indexer status, disk space, queue errors |
| UniFi Network | v0.3.3 | Device status, AP health, WAN failover, client tracking |
| Cloudflare | v0.3.3 | Tunnel health, WAF events, DNS, SSL |
| 30+ more | [Planned](docs/research/PHASE3-PLAN.md) | Plex, EMQX, Stalwart, Synology, N8N, Ollama, Zigbee2MQTT, Frigate, and more |

Adding a new system = implement the [handler interface](ARCHITECTURE.md). No core changes needed.

## Ingestion Sources

Multiple adapters produce the same canonical event model:

- **MQTT** — Subscribe to topics on any broker (zigbee2mqtt, frigate, ESPresence, valetudo, custom sensors)
- **HA WebSocket** — Real-time state changes, automation failures, service call errors
- **HA Log Poller** — WebSocket `system_log/list` with pattern matching against structured entries
- **Webhook Receiver** — HTTP endpoint for push-based ingestion (Radarr, Sonarr, Plex, Proxmox) *(v0.3.0)*
- **HTTP Poller** — Periodic REST API polling with JMESPath extraction (any service with a health API) *(v0.3.0)*

## Configuration

### Current Release (v0.2.x)

All settings are passed as **environment variables**. The `config.yaml` baked into the Docker image uses `${VAR}` interpolation to read them at startup.

**Required** — agent will not start without these:

```bash
HA_TOKEN=your_ha_long_lived_access_token
MQTT_PASS=your_mqtt_password
REASONING_LLM_API_KEY=your_cloud_llm_key
INFLUXDB_TOKEN=your_influxdb_token
```

**Optional** — sensible defaults are provided:

```bash
# Home Assistant
HA_URL=http://localhost:8123
HA_WS_URL=ws://localhost:8123/api/websocket

# MQTT
MQTT_BROKER=mqtt://localhost:1883
MQTT_USER=oasisagent

# LLM: T1 Triage (defaults to local Ollama)
TRIAGE_LLM_BASE_URL=http://localhost:11434/v1
TRIAGE_LLM_MODEL=qwen2.5:7b
TRIAGE_LLM_API_KEY=not-needed
TRIAGE_LLM_TIMEOUT=5

# LLM: T2 Reasoning
REASONING_LLM_BASE_URL=https://api.anthropic.com
REASONING_LLM_MODEL=claude-sonnet-4-5-20250929

# InfluxDB
INFLUX_URL=http://localhost:8086
INFLUX_ORG=myorg
INFLUX_BUCKET=oasisagent

# Safety — starts in dry-run mode (log decisions, don't execute)
DRY_RUN=true
```

See [`.env.example`](.env.example) for the full reference. For advanced config options (log patterns, guardrails tuning, circuit breaker thresholds), see [`config.example.yaml`](config.example.yaml).

### Upcoming (v0.3.0+)

v0.3.0 adds a **UI-first configuration model**. Bootstrap with 4 environment variables, then configure everything else through the web admin UI:

| Layer | What | Where |
|-------|------|-------|
| **Bootstrap** | Port, data dir, secret key, log level | 4 env vars |
| **Runtime** | Integrations, services, notifications, scanners | SQLite (secrets encrypted at rest) — managed via web UI |
| **Content** | Known fixes, prompt templates | YAML files on disk |

Existing configurations can be imported via `oasisagent config import seed.yaml`.

## Deployment Options

The same [`docker-compose.yml`](docker-compose.yml) works for all Docker-based deployments. It pulls the published image from GHCR and reads all configuration from environment variables.

### Docker Compose (single node)

```bash
cp .env.example .env   # fill in your values
docker compose up -d
```

To include a bundled InfluxDB for audit logging:

```bash
docker compose --profile monitoring up -d
```

### Docker Swarm

```bash
docker stack deploy -c docker-compose.yml oasis
```

Set environment variables in your orchestrator (Portainer stack UI, etc.) or export them before deploying. The compose file includes deploy constraints, resource limits, and rolling update policy.

| Variable | Required | Default |
|----------|----------|---------|
| `HA_TOKEN` | Yes | — |
| `HA_URL` | No | `http://localhost:8123` |
| `HA_WS_URL` | No | `ws://localhost:8123/api/websocket` |
| `MQTT_BROKER` | No | `mqtt://localhost:1883` |
| `MQTT_USER` | No | `oasisagent` |
| `MQTT_PASS` | Yes | — |
| `TRIAGE_LLM_BASE_URL` | No | `http://localhost:11434/v1` |
| `TRIAGE_LLM_MODEL` | No | `qwen2.5:7b` |
| `TRIAGE_LLM_API_KEY` | No | `not-needed` |
| `REASONING_LLM_API_KEY` | Yes | — |
| `REASONING_LLM_BASE_URL` | No | `https://api.anthropic.com` |
| `REASONING_LLM_MODEL` | No | `claude-sonnet-4-5-20250929` |
| `INFLUXDB_TOKEN` | Yes | — |
| `INFLUX_URL` | No | `http://localhost:8086` |
| `DRY_RUN` | No | `true` |

### From Source

```bash
git clone https://github.com/dadcoachengineer/oasisagent.git
cd oasisagent
pip install -e ".[dev]"
cp .env.example .env   # fill in your values
oasisagent
```

## Observability

- **InfluxDB audit trail** — Every event, decision, action, and verification recorded
- **Grafana dashboards** — Import [`dashboards/oasisagent-overview.json`](dashboards/) for event volume, decision distribution, and action results
- **Prometheus metrics** — `/metrics` endpoint for real-time alerting (events processed, queue depth, processing latency, LLM call duration)
- **Web admin dashboard** — Real-time event feed, approval queue, event explorer, connector management *(v0.3.1)*

## Known Fixes Registry

The `known_fixes/` directory contains YAML files that power the T0 tier — instant, zero-cost resolution:

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

Contributing known fixes is the easiest way to improve OasisAgent. If T1/T2 diagnosed a failure for you, add it to the registry so it resolves instantly next time.

## Contributing

Contributions welcome! The most impactful areas:

1. **Known fixes** — Add YAML entries for failure patterns you've encountered
2. **Plugins** — Community handlers, adapters, and notification channels via the plugin system *(v0.3.6)*
3. **Integrations** — Service-specific adapters for your infrastructure stack
4. **Known fix contributions** — If T1/T2 diagnosed a failure for you, add it to the registry so it resolves instantly next time

```bash
# Development setup
git clone https://github.com/dadcoachengineer/oasisagent.git
cd oasisagent
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .
```

Please [open an issue](https://github.com/dadcoachengineer/oasisagent/issues) before starting work on major features to discuss the approach.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design specification — data models, component interfaces, configuration schema, and phasing details.

## Roadmap

| Phase | Version | Scope | Status |
|-------|---------|-------|--------|
| 1 | v0.1.x | Core framework — ingestion, decision engine, HA handler, known fixes, audit, circuit breaker | **Complete** |
| 2 | v0.2.x | T2 cloud reasoning, approval queue, verification loop, event correlation, email/webhook notifications, Grafana dashboards, Prometheus metrics | **Complete** |
| 3 | v0.3.0 | Foundation — SQLite config backend, FastAPI scaffold, webhook receiver, HTTP poller, Proxmox + Docker handlers | In progress |
| 3 | v0.3.1 | Web admin UI — HTMX dashboard, setup wizard, connectors, approval queue, event explorer | Planned |
| 3 | v0.3.2 | Messaging — Telegram, Slack, Discord notification + approval channels | Planned |
| 3 | v0.3.3 | Networking — UniFi, Cloudflare, Radarr/Sonarr integrations | Planned |
| 3 | v0.3.4 | Preventive scanning — certificates, disk space, backup freshness, health sweeps | Planned |
| 3 | v0.3.5 | Learning loop — auto-generate T0 known fix candidates from T2 diagnoses | Planned |
| 3 | v0.3.6 | Plugin system, multi-instance coordination, Tier 3 integrations | Planned |
| — | v1.0.0 | Production release | Target |

See the [Phase 3 plan](docs/research/PHASE3-PLAN.md) for the full integration catalog covering 40+ services and the [epic tracker](https://github.com/dadcoachengineer/oasisagent/issues/70) for issue-level progress.

## License

MIT — see [LICENSE](LICENSE) for details.
