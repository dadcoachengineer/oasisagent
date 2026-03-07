# oasis-agent

Autonomous Infrastructure Operations Agent — detects, diagnoses, and remediates failures across home infrastructure systems (Home Assistant, Docker, Proxmox) without human intervention.

## What it does

oasis-agent is a standalone Docker container that sits alongside your existing monitoring stack (Telegraf, EMQX, InfluxDB, Grafana) and closes the gap between **detection** and **resolution**:

1. **Detect** — Subscribes to failure events via EMQX/MQTT
2. **Classify** — Matches against a known-fixes registry (fast path) or routes to Claude API for diagnosis (slow path)
3. **Decide** — Applies guardrails: auto-fix (low risk), recommend (medium risk), escalate (high risk)
4. **Act** — Executes remediation via system APIs (HA REST, Docker socket, Proxmox API)
5. **Verify** — Confirms the fix holds; escalates if it doesn't
6. **Audit** — Logs everything to InfluxDB for Grafana dashboards

## Key design principles

- **Two-tier reasoning**: Known fixes resolve in milliseconds via lookup. Novel failures route to Claude API. The LLM is a fallback, not the hot path.
- **Safety first**: Security systems (locks, alarms, cameras) are permanently blocked from auto-remediation. Circuit breakers prevent remediation loops.
- **Full audit trail**: Every decision is logged to InfluxDB with complete context.
- **Extensible handlers**: Adding a new managed system requires only a new handler module + MQTT topic subscription.

## Architecture

```
EMQX (MQTT) ──► oasis-agent ──► HA REST API
                    │              Docker API
                    │              Proxmox API
                    │
                    ├──► InfluxDB (audit)
                    ├──► Claude API (diagnosis)
                    └──► Notifications (MQTT/email/push)
```

## Project structure

```
oasis-agent/
  core/           # MQTT listener, decision engine, audit, config, models
  handlers/       # System-specific remediation (HA, Docker, Proxmox)
  known_fixes/    # YAML registries of known failure → fix mappings
  notifications/  # Notification dispatcher
  tests/          # Test suite
  config.yaml     # Runtime configuration
  Dockerfile
  docker-compose.yml
```

## Phasing

- **Phase 1**: Core framework — MQTT listener, decision engine, HA handler, known-fixes registry, InfluxDB audit, circuit breaker
- **Phase 2**: Claude API integration, Docker handler, notifications, Grafana dashboard
- **Phase 3**: Proxmox handler, preventive scanner, verification loop, learning loop

## Prerequisites

| Dependency | Description |
|---|---|
| EMQX Broker | Running MQTT broker for event bus |
| InfluxDB v2 | Audit log storage |
| Home Assistant | REST API with long-lived access token |
| Claude API Key | For LLM-based diagnosis (Phase 2) |

## License

Private — All rights reserved.
