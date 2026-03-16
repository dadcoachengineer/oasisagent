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
- Pydantic for config validation and data models
- Use `ruff` for linting and formatting

### Architecture Constraints
- **The canonical Event model is the core data contract.** All ingestion adapters produce `Event` objects. The decision engine, handlers, and audit system all consume them. Do not create parallel data structures.
- **Ingestion adapters implement `IngestAdapter` ABC.** See ARCHITECTURE.md §4.
- **Handlers implement `Handler` ABC.** See ARCHITECTURE.md §8.
- **Notification channels implement `NotificationChannel` ABC.** See ARCHITECTURE.md §10.
- **Interactive notification channels extend `InteractiveNotificationChannel` ABC** for bidirectional approval flows (Telegram, Slack, web UI).
- **The LLM client is role-based.** Code calls `llm.complete(role=LLMRole.TRIAGE, ...)` — never LiteLLM directly. See ARCHITECTURE.md §7.
- **Guardrails are deterministic code, not model prompts.** Risk tiers, blocked domains, and circuit breaker logic live in the decision engine, not in LLM prompt instructions.
- **T1 (triage) classifies and packages — it never decides to take action.** The decision engine applies risk tier logic based on T1's structured output.

### Config & Secrets
- **UI-first configuration**: Web UI is the primary config surface, not YAML
- **3-layer config model**: Bootstrap (4 env vars) → Runtime (SQLite + Fernet encryption) → Content (YAML known fixes)
- Bootstrap env vars: `OASIS_PORT`, `OASIS_DATA_DIR`, `OASIS_SECRET_KEY`, `OASIS_LOG_LEVEL`
- `config.yaml` is optional import/export, not required for startup
- Secrets stored in SQLite with Fernet encryption at rest
- All config managed through `oasisagent/db/config_store.py`

### Database
- SQLite with WAL mode for concurrent reads during web UI
- Numbered migrations in `oasisagent/db/migrations/` (currently 13 migrations, 001–013)
- Schema version tracked in `_meta` table
- Migrations discovered via `pkgutil.iter_modules` matching pattern `^\d{3}_\w+$`
- No Alembic — migrations are simple async functions: `async def migrate(db: aiosqlite.Connection)`

### Testing
- pytest + pytest-asyncio (2868 tests as of v0.3.5)
- Unit tests for all business logic (decision engine, known_fixes matcher, circuit breaker, guardrails)
- Integration tests with mocked external services (MQTT broker, HA API, InfluxDB)
- Test the LLM client with mock responses — don't call real LLM endpoints in tests
- Known fixes YAML files should have corresponding test cases

### Project Structure
Follow the layout in ARCHITECTURE.md §12 exactly. The package is `oasisagent/` (not `oasis_agent/` or `src/`).

## Current State (v0.3.5)

### Architecture
- **Single process**: FastAPI serves web UI + webhook receiver + REST API on one port
- **Web admin UI**: HTMX-powered dashboard, setup wizard, config CRUD, event explorer, approval queue, service map, notification feed
- **19 ingestion adapters**: MQTT, HA WebSocket, HA log poller, HTTP poller, Portainer, Proxmox, UniFi, Cloudflare, Servarr (Radarr/Sonarr/etc.), Plex, qBittorrent, Tdarr, Tautulli, Overseerr, Frigate, N8N, NPM, Vaultwarden, Uptime Kuma
- **6 handlers**: Home Assistant, Docker, Portainer, Proxmox, UniFi, Cloudflare
- **8 notification channels**: MQTT, Email, Webhook, Telegram (interactive), Discord, Slack, Web UI (interactive)
- **5 scanners**: Certificate expiry, disk space, backup freshness, HA health, Docker health
- **15 known fix YAML files** covering all supported systems

### Decision Engine Pipeline
- T0 (known fixes lookup) → T1 (SLM triage) → T2 (cloud reasoning)
- **Dependency-aware context**: Service topology graph with BFS-based subgraph extraction for T2
- **Multi-handler context assembly**: Lazy entity context gathering, only invoked at T2
- **Plan-aware dispatch**: T2 can produce multi-step `RemediationPlan` with dependency ordering. `PlanExecutor` runs steps sequentially with handler verification. Whole-plan approval for non-AUTO_FIX plans.
- **Event correlation**: Same-entity dedup window + cross-domain correlation
- **Circuit breaker**: Per-entity and global failure rate tracking

### Key Engine Modules
- `oasisagent/engine/decision.py` — Decision engine (T0/T1/T2 + guardrails)
- `oasisagent/engine/plan_executor.py` — Multi-step remediation state machine
- `oasisagent/engine/context_assembly.py` — Multi-handler context gathering for T2
- `oasisagent/engine/service_graph.py` — Service topology graph + dependency BFS
- `oasisagent/engine/correlator.py` — Same-entity event correlation
- `oasisagent/engine/cross_correlator.py` — Cross-domain event correlation
- `oasisagent/engine/circuit_breaker.py` — Per-entity/global circuit breaker
- `oasisagent/engine/guardrails.py` — Risk tier enforcement
- `oasisagent/engine/known_fixes.py` — YAML known fix registry + matcher

### Web UI Routes (10 route files)
`oasisagent/ui/routes/`: approvals, auth_routes, connectors, dashboard, events, known_fixes, notifications, service_map, setup_routes, users

### Approval System
- `PendingAction` model supports both single-action and plan-level approval (`plan_id` field)
- `PendingQueue` is dual-layer: SQLite source of truth + in-memory cache
- CAS (compare-and-swap) for all status transitions
- Approval surfaces: Web UI, Telegram, Slack, MQTT

## Dependencies

Core: pydantic, pyyaml, litellm, aiomqtt, aiohttp, influxdb-client[async], fastapi, uvicorn, aiosqlite, cryptography, jinja2, python-jose, jmespath
Dev: pytest, pytest-asyncio, pytest-cov, ruff

## What NOT to Do

- Don't let LLM models make safety decisions — guardrails are code
- Don't call LiteLLM directly outside the LLM client module
- Don't create Event-like objects outside the canonical model
- Don't access `process.env` directly — use the config store / Pydantic models
- Don't add migrations without bumping the number sequentially (currently at 013)
- Don't bypass the PendingQueue's CAS transitions — always use approve()/reject()
