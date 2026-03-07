# OasisAgent — Claude Code Project Context

## What This Project Is

OasisAgent is an autonomous infrastructure operations agent for home labs. It detects failures via multiple ingestion sources, classifies them using three-tier LLM reasoning (T0 lookup → T1 local SLM → T2 cloud model), and auto-remediates or escalates with full context.

This is a **public open-source project (MIT license)**. All code must be config-driven with no hardcoded IPs, hostnames, credentials, or topology assumptions.

## Critical References

- **ARCHITECTURE.md** — The authoritative design spec. All code must conform to the interfaces, data models, and patterns defined there. Read it before implementing anything.
- **README.md** — Public-facing project description. Keep it in sync with implementation.

## Implementation Rules

### Code Quality
- Python 3.11+, fully async (asyncio)
- Type hints on all function signatures
- Pydantic for config validation and data models
- All modules must have corresponding tests
- Use `ruff` for linting and formatting
- Production-quality code — no TODOs in merged code, no bare excepts, no print statements

### Architecture Constraints
- **The canonical Event model is the core data contract.** All ingestion adapters produce `Event` objects. The decision engine, handlers, and audit system all consume them. Do not create parallel data structures.
- **Ingestion adapters implement `IngestAdapter` ABC.** See ARCHITECTURE.md §4.
- **Handlers implement `Handler` ABC.** See ARCHITECTURE.md §8.
- **Notification channels implement `NotificationChannel` ABC.** See ARCHITECTURE.md §10.
- **The LLM client is role-based.** Code calls `llm.complete(role=LLMRole.TRIAGE, ...)` — never LiteLLM directly. See ARCHITECTURE.md §7.
- **Guardrails are deterministic code, not model prompts.** Risk tiers, blocked domains, and circuit breaker logic live in the decision engine, not in LLM prompt instructions.
- **T1 (triage) classifies and packages — it never decides to take action.** The decision engine applies risk tier logic based on T1's structured output.

### Config & Secrets
- All configuration in `config.yaml`, validated by Pydantic on startup
- Secrets via environment variables with `${VAR}` syntax in config
- Provide sensible defaults where possible (localhost endpoints, reasonable timeouts)
- `config.example.yaml` must stay in sync with any config schema changes

### Testing
- pytest + pytest-asyncio
- Unit tests for all business logic (decision engine, known_fixes matcher, circuit breaker, guardrails)
- Integration tests with mocked external services (MQTT broker, HA API, InfluxDB)
- Test the LLM client with mock responses — don't call real LLM endpoints in tests
- Known fixes YAML files should have corresponding test cases

### Project Structure
Follow the layout in ARCHITECTURE.md §12 exactly. The package is `oasisagent/` (not `oasis_agent/` or `src/`).

## Current Phase

**Phase 1** — Core framework. See ARCHITECTURE.md §14 for the complete checklist.

Priority order for implementation:
1. Project scaffolding — pyproject.toml, Dockerfile, docker-compose.yml
2. Config loading + validation (pydantic models matching §11 schema)
3. Core models — Event, Severity, ActionResult, TriageResult, DiagnosisResult
4. Event queue (asyncio.Queue with backpressure)
5. Ingestion adapters — MQTT, HA WebSocket, HA log poller
6. Known fixes registry — YAML loader + match engine
7. Decision engine — orchestrator tying T0/T1/T2 → guardrails → handler dispatch
8. Circuit breaker
9. LLM client — LiteLLM wrapper with role-based routing
10. T1 triage prompts (classify_event, summarize_context)
11. HA handler — notify, restart_integration, reload_automations, get_context
12. Audit — InfluxDB writer
13. Notifications — MQTT channel
14. Tests
15. Documentation — config.example.yaml, .env.example

## Dependencies

Core: pydantic, pyyaml, litellm, aiomqtt, aiohttp, influxdb-client[async]
Dev: pytest, pytest-asyncio, pytest-cov, ruff

## What NOT to Do

- Don't hardcode any infrastructure details (IPs, hostnames, org names)
- Don't let LLM models make safety decisions — guardrails are code
- Don't call LiteLLM directly outside the LLM client module
- Don't create Event-like objects outside the canonical model
- Don't skip tests for "simple" modules — everything gets tested
- Don't add Phase 2/3 features to Phase 1 scope (stub the handlers, don't implement them)
