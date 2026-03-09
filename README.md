# OasisAgent

Autonomous infrastructure operations agent for home labs. Detects failures, classifies them with tiered LLM reasoning, and auto-remediates or escalates with full context.

OasisAgent bridges the gap between **monitoring** (you know something broke) and **resolution** (it's fixed). It sits alongside your existing stack — not inside it — and closes that gap automatically for known issues, and with intelligent diagnosis for novel ones.

## How It Works

```
Event Source → Ingestion → Decision Engine → Handler → Verify → Audit
                               │
                    ┌──────────┼──────────┐
                    T0         T1          T2
                  Lookup    Local SLM   Cloud LLM
                  (<1ms)   (100-500ms)  (5-45s)
```

**Three-tier reasoning:**

- **T0 — Known Fixes**: Pattern match against a YAML registry. Instant. No LLM. Covers the common cases.
- **T1 — Triage**: A local small language model classifies events, filters noise, and packages context. Runs on your hardware at near-zero cost.
- **T2 — Diagnosis**: A cloud reasoning model (Claude, GPT, Gemini, or self-hosted) diagnoses novel failures and recommends actions. Invoked only when T1 can't handle it.

**Safety guardrails (enforced in code, not prompts):**

- Risk tiers: `AUTO_FIX` → `RECOMMEND` → `ESCALATE` → `BLOCK`
- Blocked domains: Security systems (locks, alarms, cameras) are permanently excluded
- Circuit breaker: Max 3 attempts per entity per hour, global kill switch at 30% failure rate
- Dry-run mode: Log every decision without executing anything
- Kill switch: Disable all automated actions instantly

## Supported Systems

| System | Phase | Capabilities |
|--------|-------|-------------|
| Home Assistant | 1 | Automation errors, state monitoring, log analysis, integration restarts |
| Docker | 2 | Container health, restart, stats, log collection, OOM/crash detection |
| Proxmox | 3 | VM/CT management, node monitoring |

Additional systems can be added by implementing the handler interface.

## Ingestion Sources

OasisAgent doesn't require a single event pipeline. It supports multiple ingestion adapters that all produce the same canonical event model:

- **MQTT** — Subscribe to topics on any MQTT broker
- **HA WebSocket** — Real-time state changes, automation failures, service call errors
- **HA Log Poller** — Pattern-match against Home Assistant logs
- **More planned** — Docker events, Proxmox tasks, webhook receiver, InfluxDB alerts

## LLM Configuration

OasisAgent is **provider-agnostic**. You bring your own endpoints for both tiers:

```yaml
llm:
  triage:
    base_url: http://your-ollama-host:11434/v1   # Any OpenAI-compatible endpoint
    model: qwen2.5:7b
    api_key: ${TRIAGE_LLM_API_KEY:-not-needed}

  reasoning:
    base_url: https://api.anthropic.com            # Or openai, openrouter, etc.
    model: claude-sonnet-4-5-20250929       # Or claude-opus-4-6, gpt-4o, gemini-2.0, etc.
    api_key: ${REASONING_LLM_API_KEY}
```

Any OpenAI-compatible API works — Ollama, LM Studio, vLLM, llama.cpp, or any cloud provider via [LiteLLM](https://github.com/BerriAI/litellm). Mix and match as you like.

## Quick Start

### Prerequisites

- Docker and Docker Compose
- An MQTT broker (EMQX, Mosquitto, etc.)
- Home Assistant with a long-lived access token
- InfluxDB v2 (for audit logging)
- A local LLM endpoint (Ollama recommended) for T1 triage
- A cloud LLM API key for T2 reasoning (optional — T1 can run standalone)

### Deploy

```bash
# Clone the repo
git clone https://github.com/dadcoachengineer/oasisagent.git
cd oasisagent

# Configure
cp config.example.yaml config.yaml
cp .env.example .env
# Edit config.yaml and .env with your endpoints, tokens, and preferences

# Run
docker-compose up -d

# Check logs
docker-compose logs -f oasisagent
```

### Configuration

All configuration is in `config.yaml` with secrets in environment variables. See `config.example.yaml` for a fully documented reference.

Key sections:
- `agent` — Global settings including event queue size, correlation window, metrics port
- `ingestion` — Which event sources to enable and how to connect
- `llm` — T1 (triage) and T2 (reasoning) endpoints and models
- `handlers` — Which managed systems to enable and their API connections
- `guardrails` — Safety controls, blocked domains, circuit breaker settings
- `audit` — InfluxDB connection for the audit trail
- `notifications` — Where to send alerts (MQTT, email, webhook)

## Approval Queue

Actions classified as `RECOMMEND` risk tier require operator approval before execution. The approval queue uses MQTT for communication:

```bash
# List pending actions
oasisagent queue list --broker mqtt://localhost:1883

# Approve a specific action
oasisagent queue approve <action-id>

# Reject an action
oasisagent queue reject <action-id>
```

Pending actions expire automatically after a configurable timeout (default: 30 minutes).

## Observability

OasisAgent ships with three observability layers:

- **InfluxDB audit trail** — Every event, decision, action, and verification is recorded for full accountability
- **Grafana dashboards** — Import `dashboards/oasisagent-overview.json` for event volume, decision distribution, action results, and more
- **Prometheus metrics** — Enable `agent.metrics_port` to expose `/metrics` for real-time alerting (events, decisions, actions, queue depth, processing latency)

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design specification, including data models, component interfaces, configuration schema, and phasing details.

## Known Fixes Registry

The `known_fixes/` directory contains YAML files mapping known failure patterns to fixes. These power the T0 (instant lookup) tier:

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

Contributing known fixes is one of the easiest ways to improve OasisAgent for everyone. If you've hit a failure that the agent diagnosed via T1/T2, consider adding it to the registry so it resolves instantly next time.

## Dependencies

Core runtime:

| Package | Purpose |
|---------|---------|
| `pydantic` | Config validation and data models |
| `pyyaml` | Config file parsing |
| `litellm` | Provider-agnostic LLM client |
| `aiomqtt` | MQTT ingestion and notifications |
| `aiohttp` | HTTP clients (HA, Docker, webhook) and Prometheus metrics server |
| `aiosmtplib` | Email notifications via SMTP |
| `prometheus_client` | Prometheus metrics exposition |
| `influxdb-client[async]` | InfluxDB audit trail |

Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`

All dependencies are declared in `pyproject.toml`. Install with `pip install .` or `pip install -e ".[dev]"` for development.

## Roadmap

- **Phase 1** (v0.1.0): Core framework — ingestion, decision engine, HA handler, known fixes, audit, circuit breaker
- **Phase 2** (v0.2.0): T2 cloud reasoning, approval queue + CLI, verification loop, event correlation, Docker handler, email/webhook notifications, Grafana dashboards, Prometheus metrics
- **Phase 3**: Proxmox handler, web admin UI, messaging integrations, preventive scanning, learning loop (auto-promote T2 diagnoses to T0)

## Contributing

Contributions welcome. The most impactful areas:

1. **Known fixes** — Add YAML entries for failure patterns you've encountered
2. **Ingestion adapters** — New event sources
3. **Handlers** — Support for additional managed systems
4. **Notification channels** — ntfy, Pushover, Slack, Discord, etc.

Please open an issue before starting work on major features to discuss the approach.

## License

MIT — see [LICENSE](LICENSE) for details.
