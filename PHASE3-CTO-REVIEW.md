# Phase 3 Plan — CTO Review & Required Changes

*Reviewed 2026-03-11. Send to CC for integration into PHASE3-PLAN.md and ARCHITECTURE.md before implementation begins.*

---

## 1. Overall Verdict

**Approved with required architectural changes.** The research quality, dependency discipline, and integration tiering are strong. The changes below must be integrated before CC starts executing v0.3.0.

---

## 2. Required Changes

### 2.1 Configuration Architecture — UI-First, Not YAML-First

**Decision**: The web UI is the primary configuration surface for OasisAgent. Operators should never need to edit YAML or manage dozens of env vars to configure integrations.

**Three-layer config model**:

| Layer | What | Where | Managed By |
|-------|------|-------|------------|
| Bootstrap | Port, data dir, secret key, log level | 3–5 env vars | `docker-compose.yml` |
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

**Rationale**: Every comparable tool in the home lab ecosystem (Home Assistant, Grafana, Portainer, Uptime Kuma) uses a web UI for integration config. Requiring YAML editing for 40+ services with 100+ config values is a regression from the UX these operators already expect. The HA model (minimal YAML bootstrap + UI-managed integrations) is the right pattern for this audience.

**Impact on plan**: The SQLite schema and connector CRUD API must be built in v0.3.0 (not deferred to v0.3.2). The web UI in v0.3.2 becomes a frontend to APIs that already exist.

---

### 2.2 Webhook Receiver + Web UI — Single Process

The webhook receiver (§5.1, port 9090) and web admin UI (§3.1) are both FastAPI apps. They must run as a **single FastAPI application, single uvicorn process**.

- Webhook routes mount under `/ingest/webhook/{source}`
- Admin UI routes mount under `/` (or `/admin/`)
- One port, one health check, one TLS termination point

**Do not** run two separate processes. It doubles container port management, health checks, and process supervision for no benefit.

---

### 2.3 HTTP Poller `response_mapping` — Needs Concrete Spec

§5.2 hand-waves the response mapping with "JSONPath or jq-style extraction rules." Before implementation, define:

- **Extraction library**: Recommend `jmespath` (well-maintained, intuitive syntax, no dependencies). Not jsonpath-ng (heavy, inconsistent spec compliance).
- **Mapping Pydantic model**: What fields are extracted from the response? At minimum: `entity_id`, `severity_expr` (JMESPath expression that returns severity), `event_type`, `payload_expr` (optional, extracts subset of response as event payload).
- **Response modes** (pick per-target):
  - `health_check` mode: HTTP 200 = ok, anything else = emit event. No JMESPath needed.
  - `extract` mode: Apply JMESPath expressions to response JSON, build Event from extracted fields.
  - `threshold` mode: Extract a numeric value, emit event when it crosses a configured threshold (e.g., disk usage > 85%).
- **Pydantic model sketch**:

```python
class HttpPollerTarget(BaseModel):
    name: str
    url: str
    auth: Optional[AuthConfig]
    interval: int = 60
    system: str
    mode: Literal["health_check", "extract", "threshold"] = "health_check"
    extract: Optional[ExtractMapping]   # for mode=extract
    threshold: Optional[ThresholdConfig] # for mode=threshold
```

---

### 2.4 `InteractiveNotificationChannel` ABC — Needs Interface Contract

§3.2 mentions `send_approval_request()` and `start_listener()`/`stop_listener()` but doesn't define the contract. Before v0.3.1 (messaging) starts, specify:

- **Relationship to existing ABC**: `InteractiveNotificationChannel` extends `NotificationChannel` (adds interactive methods, doesn't replace).
- **Method signatures**:
  - `send_approval_request(pending: PendingAction) -> str` — returns a message/reference ID
  - `start_listener(callback: Callable[[str, ApprovalDecision], Awaitable[None]]) -> None` — registers approval callback
  - `stop_listener() -> None`
- **Approval response flow**: Interactive channels call the callback with `(action_id, decision)`. The callback is provided by the decision engine (or approval queue manager). Do not route approvals through MQTT unless the non-interactive path already does.
- **Disconnect handling**: If listener disconnects, channel must auto-reconnect (same backoff pattern as other adapters). Pending approvals that expire during disconnect follow existing timeout rules.

---

## 3. Milestone Reordering

### 3.1 Swap v0.3.1 (Messaging) and v0.3.2 (Web UI)

**New order**:

| Version | Content | Weeks |
|---------|---------|-------|
| v0.3.0 | Foundation (webhook receiver, HTTP poller, Proxmox handler, Docker handler, MQTT expansion, **SQLite schema + connector CRUD API**) | 1–3 |
| v0.3.1 | **Web Admin UI** (FastAPI + HTMX + Jinja2, auth, dashboard, connectors page, approval queue, event explorer) | 4–6 |
| v0.3.2 | **Messaging** (Telegram, Slack, Discord) | 7–8 |
| v0.3.3 | Networking integrations (UniFi, Cloudflare, Servarr) | 9–10 |
| v0.3.4 | Preventive scanning | 11–12 |
| v0.3.5 | Learning loop (**only** — move Tier 3 integrations out) | 13–14 |
| v0.3.6 | Plugins + multi-instance + remaining Tier 3 integrations | 15–16 |
| v1.0.0 | Release | — |

**Rationale**:

- The web UI is now the primary config surface (per §2.1). It must exist before operators can configure messaging channels, networking integrations, or scanners.
- The UI provides observability (dashboard, event explorer) needed while debugging the 6+ new integrations in v0.3.0 and v0.3.2+.
- MQTT and email notifications already work from Phase 2. You're not blocked on messaging.
- Building the UI first validates SSE streaming, auth, and the connector CRUD API before downstream milestones depend on them.

### 3.2 Reduce v0.3.5 Scope

The current plan bundles the learning loop + 6 Tier 3 integrations (Stalwart, EMQX, Synology, N8N, Nextcloud, Ollama) into 2 weeks. The learning loop alone (candidate generation, confidence scoring, promotion workflow) is a 2-week effort.

**Change**: v0.3.5 = learning loop only. Move Tier 3 integrations to v0.3.6 alongside plugins and multi-instance.

---

## 4. Non-Blocking Callouts

These don't block execution but should be addressed during implementation.

### 4.1 Learning Loop — Drop Git Integration

§3.4 mentions "Git-based if in repo (commit candidates, `git mv` to promote)." Remove this. The agent should never shell out to git. File-based with `_meta.status` (candidate/promoted/rejected) is the right approach. External git hooks can track the `candidates/` directory if desired.

### 4.2 SQLite Migration Strategy

The plan picks SQLite for user store and now for all integration config. Add a migration story before v1.0:

- Include a `schema_version` integer in the database
- On startup, check version and run sequential migration functions
- Keep it simple — no Alembic. A `migrations/` directory with numbered Python scripts (`001_initial.py`, `002_add_scanner_config.py`) is sufficient.

### 4.3 Multi-Instance Event Dedup Is Load-Bearing

§3.6 (multi-instance) is scheduled last, which is correct. But the existing event dedup in the decision engine becomes critical if two instances ever process the same event. Verify dedup prevents double-action in the failover test matrix.

### 4.4 WhatsApp Drop — Document as ADR

The WhatsApp analysis in §3.2 is excellent. Capture it as an Architecture Decision Record in ARCHITECTURE.md so it doesn't get relitigated later.

### 4.5 Config Export Must Include Schema Version

The `oasisagent config export` command should embed a schema version in the YAML output so that `config import` can detect and handle version mismatches during migration or disaster recovery.

---

## 5. Affirmed Decisions

These choices are strong. Do not revisit:

- **HTMX + Jinja2 over SPA** — Python-only project, no npm build toolchain
- **SSE over WebSocket for dashboard** — server→client only, auto-reconnect, proxy-friendly
- **asyncio.sleep over APScheduler** — one less dependency, simple scheduling needs
- **importlib over Pluggy** — ABC validation is more explicit and debuggable
- **MQTT leader election over Redis** — uses existing infrastructure
- **Telegram first for messaging** — lowest friction, inline keyboards, battle-tested aiogram
- **Proxmox + Docker as Tier 1** — foundation infrastructure, highest blast radius
- **Known fixes as YAML on disk** — version-controllable content, not runtime config
- **"NOT Adding" dependency table** — the discipline to say no

---

## 6. Summary of Actions for CC

1. **Update PHASE3-PLAN.md** with the three-layer config model (§2.1), single-process decision (§2.2), HTTP poller spec (§2.3), and InteractiveNotificationChannel contract (§2.4)
2. **Reorder milestones** per §3.1 (Web UI before Messaging, reduced v0.3.5 scope)
3. **Add SQLite schema + connector CRUD API** to v0.3.0 scope
4. **Add `OASIS_SECRET_KEY` + Fernet encryption** to v0.3.0 scope
5. **Add first-run setup wizard API endpoints** to v0.3.0 scope
6. **Add config import/export CLI commands** to v0.3.0 scope
7. **Remove git integration** from learning loop spec
8. **Add ADR for WhatsApp drop** to ARCHITECTURE.md
9. **Add SQLite migration strategy** (schema_version + numbered scripts)
10. **Spec the HTTP poller Pydantic models** before implementing the adapter
11. **Spec the InteractiveNotificationChannel ABC** before implementing messaging

Once these are integrated, begin v0.3.0 implementation.
