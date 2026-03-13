# OasisAgent — Architecture Specification

> **Version**: 0.3.0
> **Status**: Phase 2 complete (v0.2.7). Phase 3 planned.
> **Last updated**: March 11, 2026

This document defines the architecture for OasisAgent, an autonomous infrastructure operations agent for home lab environments. It serves as the implementation contract — all code should conform to these designs.

---

## 1. Overview

OasisAgent is a standalone, containerized Python application that detects infrastructure failures, classifies them, and either auto-remediates or escalates with full context. It sits alongside existing monitoring stacks (not inside them) and communicates with managed systems via their native APIs.

### Design Principles

- **Safety over speed** — Guardrails are deterministic code, never model judgment. Security-critical systems are permanently blocked from auto-remediation.
- **Tiered reasoning** — Known fixes resolve in milliseconds. Local SLM handles classification and context packaging. Cloud reasoning models handle novel diagnosis. Each tier is a cost/latency/capability tradeoff.
- **Provider-agnostic** — Users bring their own LLM endpoints. The agent doesn't care if T1 is Ollama, LM Studio, or a cloud endpoint. Same for T2.
- **Config-driven** — No hardcoded IPs, hostnames, org names, or credentials. Everything is configurable. A new user should be able to deploy by editing `config.yaml` and setting environment variables.
- **Pluggable everything** — Ingestion sources, handlers, notification channels, and LLM providers are all modular. Adding a new managed system means writing a handler, not refactoring the core.
- **Full audit trail** — Every event, classification, decision, and action is logged with complete context. If the agent touched something, there's a record.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     OasisAgent                          │
│                                                         │
│  ┌──────────────┐    ┌──────────────┐                   │
│  │  Ingestion   │───▶│   Event      │                   │
│  │  Adapters    │    │   Queue      │                   │
│  │              │    └──────┬───────┘                   │
│  │ • MQTT       │           │                           │
│  │ • HA WS      │    ┌──────▼───────┐                   │
│  │ • HA Log     │    │  Decision    │                   │
│  │ • (future)   │    │  Engine      │                   │
│  └──────────────┘    │              │                   │
│                      │  T0: Lookup  │                   │
│                      │  T1: Triage  │──▶ LLM Client    │
│                      │  T2: Reason  │   (provider-     │
│                      │              │    agnostic)      │
│                      └──────┬───────┘                   │
│                             │                           │
│                      ┌──────▼───────┐                   │
│                      │  Handlers    │                   │
│                      │              │                   │
│                      │ • HA         │──▶ HA REST API    │
│                      │ • Docker     │──▶ Docker API     │
│                      │ • Proxmox   │──▶ Proxmox API    │
│                      │ • (future)   │                   │
│                      └──────┬───────┘                   │
│                             │                           │
│                      ┌──────▼───────┐                   │
│                      │  Audit &     │──▶ InfluxDB       │
│                      │  Notify      │──▶ MQTT/Email/    │
│                      │              │   Webhook/Push    │
│                      └──────────────┘                   │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Canonical Event Model

All ingestion sources produce the same `Event` model. The decision engine, handlers, and audit system all operate on this schema. This is the core data contract of the project.

```python
class Event(BaseModel):
    id: str                     # UUID, generated at ingestion
    source: str                 # Ingestion adapter that produced this event
                                # e.g., "mqtt", "ha_websocket", "ha_log_poller"
    system: str                 # Managed system this relates to
                                # e.g., "homeassistant", "docker", "proxmox"
    event_type: str             # Classification of what happened
                                # e.g., "automation_error", "state_unavailable",
                                #       "log_error", "service_call_failure",
                                #       "container_unhealthy", "integration_failure"
    entity_id: str              # System-specific identifier for the affected entity
                                # e.g., "automation.kitchen_motion_lights",
                                #       "container/grafana", "vm/102"
    severity: Severity          # enum: info, warning, error, critical
    timestamp: datetime         # When the event occurred (source timestamp)
    ingested_at: datetime       # When the agent received it
    payload: dict[str, Any]     # Raw source data, structure varies by source
    context: dict[str, Any]     # Additional context gathered during processing
                                # Populated progressively by T1 and handlers
    metadata: EventMetadata     # Processing metadata (see below)


class EventMetadata(BaseModel):
    correlation_id: str | None  # Groups related events (e.g., cascading failures)
    dedup_key: str              # For deduplication — source + entity_id + event_type
    ttl: int                    # Seconds before this event expires unprocessed
    retry_count: int            # How many times processing has been attempted


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
```

> **Implementation note:** All models use Pydantic `BaseModel` (not `@dataclass`) for
> validation, serialization (`.model_dump()`), and structured LLM output parsing
> (`.model_validate()`). See `oasisagent/models.py`.

### Event Lifecycle

```
Created (ingestion) → Queued → Classified (T0/T1) → Decided → Acted → Verified → Audited
                                    │                                      │
                                    └─ Dropped (noise)              Failed → Escalated
```

Each stage appends to `event.context` so the full processing history is available for audit.

---

## 4. Ingestion Layer

Ingestion adapters are the input boundary of the system. Each adapter:

1. Connects to an external source (MQTT broker, HA WebSocket, log file)
2. Receives raw data
3. Transforms it into the canonical `Event` model
4. Pushes it to the internal event queue

All adapters implement the same interface:

```python
class IngestAdapter(ABC):
    @abstractmethod
    async def start(self) -> None:
        """Start listening/polling. Runs as a long-lived async task."""

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown."""

    @abstractmethod
    def name(self) -> str:
        """Adapter identifier used in Event.source field."""
```

### Phase 1 Adapters

#### MQTT Subscriber
- Connects to EMQX (or any MQTT broker)
- Subscribes to configurable topic patterns
- Transforms MQTT messages to Events based on topic → event_type mapping
- Config:
  ```yaml
  ingestion:
    mqtt:
      enabled: true
      broker: mqtt://192.168.1.120:1883
      username: ${MQTT_USER}
      password: ${MQTT_PASS}
      topics:
        - pattern: "homeassistant/error/#"
          system: homeassistant
          event_type: automation_error
          severity: error
        - pattern: "oasis/alerts/#"
          system: auto          # Inferred from topic structure
          event_type: auto
          severity: auto
      client_id: oasis-agent
      qos: 1
  ```

#### Home Assistant WebSocket
- Connects to HA's WebSocket API
- Subscribes to `state_changed` events, filters for `unavailable`/`unknown` transitions
- Subscribes to `automation_triggered` events with failure results
- Subscribes to `call_service` result events with errors
- Config:
  ```yaml
  ingestion:
    ha_websocket:
      enabled: true
      url: ws://192.168.1.120:8123/api/websocket
      token: ${HA_TOKEN}
      subscriptions:
        state_changes:
          enabled: true
          # Only emit events for transitions TO these states
          trigger_states: ["unavailable", "unknown"]
          # Ignore entities that are expected to be unavailable sometimes
          ignore_entities:
            - "sensor.outdoor_temp"    # Battery sensor, sleeps
          # Minimum time in bad state before emitting event (debounce)
          min_duration: 60
        automation_failures:
          enabled: true
        service_call_errors:
          enabled: true
  ```

#### Home Assistant Log Poller
- Connects to HA via WebSocket and sends the `system_log/list` command to fetch structured log entries (the REST `/api/error_log` endpoint was removed in newer HA versions)
- Authenticates using the same long-lived access token as the HA WebSocket adapter
- Receives structured JSON entries with `name`, `message`, `level`, `timestamp`, `count`, `first_occurred`, and `source` fields
- Pattern-matches entries against configured regex patterns (matched against `"component: message"` combined text)
- Severity is derived from HA's native log level (`CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`), not the pattern's configured severity
- Deduplicates based on error fingerprint (event_type + component + message) within a configurable time window
- Reconnects with exponential backoff on connection failures; auth failures are fatal (adapter stops)
- Config:
  ```yaml
  ingestion:
    ha_log_poller:
      enabled: true
      url: http://192.168.1.120:8123   # http:// → ws://, https:// → wss:// automatically
      token: ${HA_TOKEN}
      poll_interval: 30          # seconds between system_log/list requests
      patterns:
        - regex: "Error setting up integration '(.+)'"
          event_type: integration_failure
          severity: error         # NOTE: severity from HA log level takes precedence
        - regex: "(.+) is unavailable"
          event_type: state_unavailable
          severity: warning
        - regex: "Deprecated .+ used in (.+)"
          event_type: deprecation_warning
          severity: warning
      dedup_window: 300          # seconds — same error within window = one event
  ```

### Future Adapters (Phase 2+)
- Docker event stream (`docker events`)
- Proxmox log/task polling
- InfluxDB query-based alerting (metric thresholds → events)
- Generic webhook receiver (for external tools to push events)

---

## 5. Three-Tier Reasoning

### T0 — Known Fixes Lookup (deterministic, <1ms)

Pattern matching against a YAML registry of known failure → fix mappings. No LLM involved. This is the fast path.

```yaml
# known_fixes/homeassistant.yaml
fixes:
  - id: ha-deprecated-kelvin
    match:
      system: homeassistant
      event_type: automation_error
      payload_contains: "kelvin"
    diagnosis: "HA deprecated 'kelvin' parameter in favor of 'color_temp_kelvin'"
    action:
      type: recommend       # or auto_fix
      handler: homeassistant
      operation: notify     # Phase 1: notify only. Phase 2+: config_edit
      details:
        message: "Replace 'kelvin' with 'color_temp_kelvin' in automation config"
    risk_tier: recommend

  - id: ha-entity-unavailable-zwave
    match:
      system: homeassistant
      event_type: state_unavailable
      entity_id_pattern: "*.zwave_*"
      min_duration: 300
    diagnosis: "Z-Wave entity unavailable for >5 min, likely controller issue"
    action:
      type: recommend
      handler: homeassistant
      operation: restart_integration
      details:
        integration: zwave_js
    risk_tier: recommend
```

#### Match Engine

The known_fixes matcher supports:
- Exact field match (`system: homeassistant`)
- Glob patterns (`entity_id_pattern: "*.zwave_*"`)
- Payload substring search (`payload_contains: "kelvin"`)
- Compound conditions (all conditions must match)
- Duration thresholds (`min_duration` — entity in bad state for N seconds)

Fixes are evaluated in order; first match wins. This is intentional — it lets you put specific patterns before general ones.

### T1 — Triage & Classification (local SLM, 100-500ms)

The local small language model handles:

1. **Noise filtering** — Is this event actionable or ignorable?
2. **Classification** — What category of failure is this?
3. **Context enrichment** — Summarize relevant logs, state history, related entities
4. **Context packaging** — If escalating to T2, prepare a structured prompt

T1 receives events that didn't match any T0 pattern. It returns a structured classification:

```python
class TriageResult(BaseModel):
    disposition: Disposition     # drop, known_pattern, escalate_t2, escalate_human
    confidence: float           # 0.0-1.0
    classification: str         # Event sub-category
    summary: str                # Human-readable summary of what happened
    suggested_fix: str | None   # If disposition is "known_pattern"
    context_package: dict[str, Any] | None  # Structured context for T2 if escalating
    reasoning: str              # Brief explanation of classification logic
```

**T1 prompt templates** are stored in `llm/prompts/` and include:
- System context (what OasisAgent is, what it does)
- The event data
- Recent related events (correlation window)
- Instructions to output structured JSON

**Important constraint:** T1 never decides to take action. It classifies and packages. The decision engine applies risk tiers and guardrails deterministically based on T1's classification.

### T2 — Deep Reasoning (cloud model, 5-45s)

Invoked only when T1 escalates with `disposition: "escalate_t2"`. Receives:

- T1's context package (structured summary, not raw logs)
- The original event
- Entity state history
- Relevant configuration snippets
- Known fixes registry (so it doesn't re-derive known solutions)

Returns a structured diagnosis:

```python
class DiagnosisResult(BaseModel):
    root_cause: str             # What went wrong and why
    confidence: float           # 0.0-1.0
    recommended_actions: list[RecommendedAction]
    risk_assessment: str        # Why this is or isn't safe to auto-fix
    additional_context: str     # Anything the human should know
    suggested_known_fix: dict[str, Any] | None  # If this should be added to the T0 registry


class RecommendedAction(BaseModel):
    description: str
    handler: str                # Which handler should execute this
    operation: str              # Handler-specific operation name
    params: dict[str, Any]      # Operation parameters
    risk_tier: RiskTier         # auto_fix, recommend, escalate, block
    reasoning: str              # Why this action and this risk tier
```

---

## 6. Decision Engine

The decision engine is the core orchestrator. It is **deterministic** — no LLM calls happen here. It receives events from the queue, routes them through the reasoning tiers, applies guardrails, and dispatches to handlers.

### Flow

```
Event received
  │
  ├─ Dedup check (seen this event recently?) → Drop if duplicate
  │
  ├─ T0: Known fixes lookup
  │   ├─ Match → Apply guardrails → Dispatch to handler
  │   └─ No match → Continue
  │
  ├─ T1: Triage (local SLM)
  │   ├─ Drop → Log and discard
  │   ├─ Known pattern → Apply guardrails → Dispatch to handler
  │   ├─ Escalate to T2 → Continue
  │   └─ Escalate to human → Notify
  │
  ├─ T2: Diagnosis (cloud model)
  │   ├─ Returns recommended actions
  │   └─ Each action → Apply guardrails → Dispatch or notify
  │
  └─ Audit: Log full decision chain to InfluxDB
```

### Guardrails (enforced in code, not model prompts)

#### Risk Tiers

| Tier | Behavior | Example |
|------|----------|---------|
| `AUTO_FIX` | Execute immediately, notify after | Restart a crashed Docker container |
| `RECOMMEND` | Notify with diagnosis + recommended action, wait for approval | Restart an HA integration |
| `ESCALATE` | Notify with full context, do NOT act | Proxmox VM unresponsive |
| `BLOCK` | Never act, never suggest automated action | Security systems |

#### Blocked Domains

These entity patterns are permanently blocked from any automated action, regardless of risk tier:

```yaml
guardrails:
  blocked_domains:
    - "lock.*"
    - "alarm_control_panel.*"
    - "camera.*"
    - "cover.*"
  blocked_entities: []          # User can add specific entities
```

#### Circuit Breaker

Prevents remediation loops:

```yaml
guardrails:
  circuit_breaker:
    max_attempts_per_entity: 3        # Per rolling window
    window_minutes: 60
    cooldown_minutes: 15              # Between retry attempts
    global_failure_rate_threshold: 0.3 # 30% failure rate = global pause
    global_pause_minutes: 30
```

When the circuit breaker trips:
1. All AUTO_FIX actions for that entity become RECOMMEND
2. If global threshold trips, all AUTO_FIX becomes ESCALATE
3. Notification sent to operator
4. Audit log records the trip with full context

#### Manual Override

```yaml
guardrails:
  kill_switch: false            # Set true to disable all automated actions
  dry_run: false                # Set true to log decisions without executing
```

---

## 7. LLM Client

### Provider-Agnostic Design

The LLM client wraps LiteLLM and exposes a role-based interface. The rest of the codebase never references LiteLLM directly or knows about specific providers.

```python
class LLMRole(str, Enum):
    TRIAGE = "triage"           # T1 — local SLM
    REASONING = "reasoning"     # T2 — cloud model


class LLMClient:
    async def complete(
        self,
        role: LLMRole,
        messages: list[dict],
        response_format: type | None = None,  # For structured output
        temperature: float = 0.1,
    ) -> LLMResponse:
        """
        Send a completion request to the configured provider for the given role.
        Handles retries, timeouts, fallback, and cost tracking internally.
        """

    def get_usage_stats(self) -> dict:
        """Return cumulative token usage and estimated cost per role."""
```

### Configuration

```yaml
llm:
  triage:
    base_url: http://192.168.1.50:11434/v1  # Ollama, LM Studio, vLLM, any OpenAI-compat
    model: qwen2.5:7b
    api_key: ${TRIAGE_LLM_API_KEY:-not-needed}
    timeout: 5
    max_tokens: 1024
    temperature: 0.1

  reasoning:
    base_url: https://api.anthropic.com      # Or openai, openrouter, self-hosted
    model: claude-sonnet-4-5-20250929        # Or claude-opus-4-6 for complex diagnosis
    api_key: ${REASONING_LLM_API_KEY}
    timeout: 45
    max_tokens: 4096
    temperature: 0.2

  options:
    cost_tracking: true
    retry_attempts: 2
    fallback_to_triage: true   # If reasoning endpoint is down, T1 attempts diagnosis
    log_prompts: false         # Set true for debugging (WARNING: may log sensitive data)
```

### Behavior

- **Retries**: On transient failures (timeout, 5xx), retry up to `retry_attempts` times with exponential backoff.
- **Fallback**: If the reasoning endpoint is unreachable and `fallback_to_triage` is true, the triage model attempts diagnosis. Result is flagged as `degraded_diagnosis` in the audit log.
- **Cost tracking**: Every completion logs token counts and estimated cost. Aggregated stats available via `get_usage_stats()` and written to InfluxDB audit bucket.
- **Structured output**: When `response_format` is provided, the client instructs the model to return JSON matching the schema and validates the response. Falls back to text parsing if structured output isn't supported by the provider.

---

## 8. Handlers

Handlers are the "hands" — they execute actions against managed systems. Each handler knows how to interact with one system's API.

### Interface

```python
class Handler(ABC):
    @abstractmethod
    def name(self) -> str:
        """Handler identifier, matches Event.system field."""

    @abstractmethod
    async def can_handle(self, event: Event, action: RecommendedAction) -> bool:
        """Check if this handler can execute the given action."""

    @abstractmethod
    async def execute(self, event: Event, action: RecommendedAction) -> ActionResult:
        """Execute the action. Returns success/failure with details."""

    @abstractmethod
    async def verify(self, event: Event, action: RecommendedAction, result: ActionResult) -> VerifyResult:
        """Verify the action had the desired effect."""

    @abstractmethod
    async def get_context(self, event: Event) -> dict:
        """Gather system-specific context for diagnosis (called by T1/T2)."""
```

### Phase 1: Home Assistant Handler

Operations:
- `notify` — Send diagnosis to operator (no system changes)
- `restart_integration` — Call `homeassistant.reload_config_entry` service
- `reload_automations` — Call `automation.reload` service
- `call_service` — Generic HA service call (with guardrail validation)
- `get_entity_state` — Read entity state for context
- `get_automation_config` — Read automation YAML for diagnosis context
- `get_error_log` — Fetch recent error log entries via WebSocket `system_log/list`

Config:
```yaml
handlers:
  homeassistant:
    enabled: true
    url: http://192.168.1.120:8123
    token: ${HA_TOKEN}
    verify_timeout: 30          # Seconds to wait after action to verify effect
```

### Phase 2: Docker Handler

Operations:
- `restart_container` — Restart a container
- `get_container_logs` — Fetch recent logs for context
- `get_container_stats` — CPU/memory/network stats
- `inspect_container` — Full container config

Config:
```yaml
handlers:
  docker:
    enabled: false              # Disabled until Phase 2
    socket: unix:///var/run/docker.sock
    # OR for remote Docker hosts:
    # url: tcp://192.168.1.120:2375
    # tls_verify: true
```

### Phase 3: Proxmox Handler

Operations:
- `restart_vm` / `restart_ct`
- `get_vm_status` / `get_ct_status`
- `get_task_log`
- `get_node_status`

Config:
```yaml
handlers:
  proxmox:
    enabled: false              # Disabled until Phase 3
    url: https://192.168.1.120:8006
    user: ${PROXMOX_USER}
    token_name: ${PROXMOX_TOKEN_NAME}
    token_value: ${PROXMOX_TOKEN_VALUE}
    verify_ssl: false
```

---

## 9. Audit System

Every event, decision, and action is logged to InfluxDB. This is non-negotiable — if the agent touched something, there's a record.

### Measurements

Written to a dedicated InfluxDB bucket (e.g., `oasisagent`):

```
oasis_event
  tags: source, system, event_type, entity_id, severity
  fields: payload (JSON string), correlation_id
  timestamp: event timestamp

oasis_decision
  tags: event_id, tier (t0/t1/t2), disposition, risk_tier
  fields: diagnosis, confidence, reasoning, model_used, tokens_used, cost_estimate
  timestamp: decision timestamp

oasis_action
  tags: event_id, handler, operation, result (success/failure/skipped)
  fields: details (JSON), duration_ms, error_message
  timestamp: action timestamp

oasis_circuit_breaker
  tags: entity_id, trigger_type (entity/global)
  fields: attempts, window_minutes, message
  timestamp: trip timestamp
```

### Config

```yaml
audit:
  influxdb:
    enabled: true
    url: http://192.168.1.120:8086
    token: ${INFLUXDB_TOKEN}
    org: oasis
    bucket: oasisagent
  retention_days: 90
```

---

## 10. Notifications

The notification dispatcher sends messages when:
- An event is classified as RECOMMEND or ESCALATE
- An AUTO_FIX action succeeds or fails
- The circuit breaker trips
- The agent starts/stops or encounters internal errors

### Channels

```yaml
notifications:
  mqtt:
    enabled: true
    broker: mqtt://192.168.1.120:1883
    topic_prefix: oasis/notifications
    username: ${MQTT_USER}
    password: ${MQTT_PASS}

  email:
    enabled: false
    smtp_host: localhost
    smtp_port: 587
    from: oasis-agent@example.com
    to:
      - admin@example.com

  webhook:
    enabled: false
    urls:
      - https://hooks.example.com/oasis

  # Future: ntfy, pushover, slack, discord, etc.
```

Notification channels implement a simple interface:

```python
class NotificationChannel(ABC):
    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Send a notification. Returns True on success."""
```

---

## 11. Configuration Schema

Full config.yaml reference:

```yaml
# OasisAgent Configuration
# All ${VAR} references are resolved from environment variables

agent:
  name: oasis-agent             # Instance name (for multi-agent setups)
  log_level: info               # debug, info, warning, error
  event_queue_size: 1000        # Internal event queue buffer
  shutdown_timeout: 30          # Seconds to wait for graceful shutdown

ingestion:
  mqtt:
    enabled: true
    broker: mqtt://localhost:1883
    username: ${MQTT_USER}
    password: ${MQTT_PASS}
    topics: []
    client_id: oasis-agent
    qos: 1

  ha_websocket:
    enabled: true
    url: ws://localhost:8123/api/websocket
    token: ${HA_TOKEN}
    subscriptions:
      state_changes:
        enabled: true
        trigger_states: ["unavailable", "unknown"]
        ignore_entities: []
        min_duration: 60
      automation_failures:
        enabled: true
      service_call_errors:
        enabled: true

  ha_log_poller:
    enabled: true
    url: http://localhost:8123
    token: ${HA_TOKEN}
    poll_interval: 30
    patterns: []
    dedup_window: 300

llm:
  triage:
    base_url: http://localhost:11434/v1
    model: qwen2.5:7b
    api_key: ${TRIAGE_LLM_API_KEY:-not-needed}
    timeout: 5
    max_tokens: 1024
    temperature: 0.1

  reasoning:
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-5-20250929
    api_key: ${REASONING_LLM_API_KEY}
    timeout: 45
    max_tokens: 4096
    temperature: 0.2

  options:
    cost_tracking: true
    retry_attempts: 2
    fallback_to_triage: true
    log_prompts: false

handlers:
  homeassistant:
    enabled: true
    url: http://localhost:8123
    token: ${HA_TOKEN}
    verify_timeout: 30

  docker:
    enabled: false
    socket: unix:///var/run/docker.sock

  proxmox:
    enabled: false
    url: https://localhost:8006
    user: ${PROXMOX_USER}
    token_name: ${PROXMOX_TOKEN_NAME}
    token_value: ${PROXMOX_TOKEN_VALUE}
    verify_ssl: false

guardrails:
  blocked_domains:
    - "lock.*"
    - "alarm_control_panel.*"
    - "camera.*"
    - "cover.*"
  blocked_entities: []
  kill_switch: false
  dry_run: false
  circuit_breaker:
    max_attempts_per_entity: 3
    window_minutes: 60
    cooldown_minutes: 15
    global_failure_rate_threshold: 0.3
    global_pause_minutes: 30

audit:
  influxdb:
    enabled: true
    url: http://localhost:8086
    token: ${INFLUXDB_TOKEN}
    org: myorg
    bucket: oasisagent
  retention_days: 90

notifications:
  mqtt:
    enabled: true
    broker: mqtt://localhost:1883
    topic_prefix: oasis/notifications
    username: ${MQTT_USER}
    password: ${MQTT_PASS}
  email:
    enabled: false
  webhook:
    enabled: false
```

---

## 12. Project Structure

```
oasisagent/
├── oasisagent/                  # Main package
│   ├── __init__.py
│   ├── __main__.py              # Entry point
│   ├── orchestrator.py          # Main loop, component lifecycle, pipeline
│   ├── config.py                # Config loading and validation (pydantic)
│   ├── models.py                # Event, Severity, ActionResult, etc.
│   │
│   ├── ingestion/               # Ingestion adapters
│   │   ├── __init__.py
│   │   ├── base.py              # IngestAdapter ABC
│   │   ├── mqtt.py
│   │   ├── ha_websocket.py
│   │   └── ha_log_poller.py
│   │
│   ├── engine/                  # Decision engine
│   │   ├── __init__.py
│   │   ├── decision.py          # Core orchestrator
│   │   ├── known_fixes.py       # T0 — YAML registry matcher
│   │   └── circuit_breaker.py
│   │
│   ├── llm/                     # LLM abstraction
│   │   ├── __init__.py
│   │   ├── client.py            # Provider-agnostic LLM client
│   │   ├── roles.py             # LLMRole enum, config mapping
│   │   └── prompts/             # Prompt templates
│   │       ├── classify_event.py
│   │       ├── diagnose_failure.py
│   │       └── summarize_context.py
│   │
│   ├── handlers/                # System handlers
│   │   ├── __init__.py
│   │   ├── base.py              # Handler ABC
│   │   ├── homeassistant.py
│   │   ├── docker.py            # Stub until Phase 2
│   │   └── proxmox.py           # Stub until Phase 3
│   │
│   ├── audit/                   # Audit logging
│   │   ├── __init__.py
│   │   └── influxdb.py
│   │
│   └── notifications/           # Notification dispatch
│       ├── __init__.py
│       ├── base.py              # NotificationChannel ABC
│       ├── mqtt.py
│       ├── email.py
│       └── webhook.py
│
├── known_fixes/                 # YAML fix registries
│   ├── homeassistant.yaml
│   ├── docker.yaml              # Empty until Phase 2
│   └── proxmox.yaml             # Empty until Phase 3
│
├── tests/
│   ├── conftest.py
│   ├── test_models.py
│   ├── test_decision_engine.py
│   ├── test_known_fixes.py
│   ├── test_circuit_breaker.py
│   ├── test_llm_client.py
│   ├── test_ha_handler.py
│   └── test_ingestion/
│       ├── test_mqtt.py
│       ├── test_ha_websocket.py
│       └── test_ha_log_poller.py
│
├── config.yaml                  # Default/example configuration
├── config.example.yaml          # Documented example for new users
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml               # Project metadata, dependencies
├── requirements.txt             # Pinned dependencies
├── LICENSE                      # MIT
├── README.md
├── ARCHITECTURE.md              # This file
├── CLAUDE.md                    # Claude Code project context
└── .env.example                 # Environment variable template
```

---

## 13. Dependencies

Core:
- `asyncio` — Event loop (stdlib)
- `pydantic` — Config validation, data models
- `pyyaml` — Config and known_fixes parsing
- `litellm` — Provider-agnostic LLM client
- `aiomqtt` — Async MQTT client (for ingestion + notifications)
- `aiohttp` — Async HTTP client (HA API, webhooks)
- `influxdb-client[async]` — InfluxDB v2 async client

Dev/Test:
- `pytest` + `pytest-asyncio`
- `pytest-cov`
- `ruff` — Linting and formatting

---

## 14. Phasing

### Phase 1 — Core Framework
- [x] Project scaffolding (pyproject.toml, Docker, CI)
- [x] Config loading and validation
- [x] Canonical Event model
- [x] Event queue (asyncio.Queue)
- [x] Ingestion: MQTT adapter
- [x] Ingestion: HA WebSocket adapter
- [x] Ingestion: HA log poller
- [x] T0: Known fixes registry + matcher
- [x] Decision engine (orchestrator + guardrails)
- [x] Circuit breaker
- [x] LLM client (provider-agnostic wrapper)
- [x] T1: Triage classification prompts
- [x] HA handler (notify, restart_integration, reload_automations, get_context)
- [x] Audit: InfluxDB writer
- [x] Notifications: MQTT channel
- [x] Tests for all above
- [x] Docker image + compose file
- [x] Documentation: README, config.example.yaml, .env.example

### Phase 2 — Extended Capabilities
See §16 for full specification.

### Phase 3 — Production Operations
See §17 for full specification.

---

## 15. Orchestrator

The orchestrator is the application's main loop. It wires all components
together, owns the event processing pipeline, and manages component lifecycles.
This is the bridge between "library of components" and "running system."

### Location

- `oasisagent/orchestrator.py` — `Orchestrator` class
- `oasisagent/__main__.py` — entry point (parse args, load config, create
  Orchestrator, run)

### Responsibilities

1. Build all components from validated config
2. Start components in dependency order
3. Pull events from the queue and run them through the full pipeline
4. Handle graceful shutdown on SIGTERM/SIGINT
5. Isolate failures so one bad event never kills the process

### Component Lifecycle

Startup order reflects dependencies — a component only starts after everything
it depends on is ready.

**Startup:**

```
 1. Load config.yaml → validate with Pydantic
 2. Load known_fixes/ YAML files → KnownFixRegistry
 3. Create CircuitBreaker
 4. Create GuardrailsEngine (needs config.guardrails)
 5. Create LLMClient (stateless — no start() needed)
 6. Create TriageService (needs LLMClient)
 7. Create DecisionEngine (needs KnownFixRegistry, TriageService,
    GuardrailsEngine, CircuitBreaker)
 8. Create Handlers → start() each enabled handler
 9. Create AuditWriter → start()
10. Create NotificationDispatcher → start()
11. Create EventQueue (asyncio.Queue, max_size from config)
12. Create Ingestion Adapters (need queue reference)
13. Start Ingestion Adapters as background asyncio.Tasks
14. Enter main event loop
```

**Shutdown (SIGTERM/SIGINT):**

```
1. Signal ingestion adapters to stop (no new events)
2. Drain queue: process remaining events with a timeout
   (config: agent.shutdown_timeout seconds)
3. Cancel any in-flight LLM calls
4. Stop handlers (close HTTP sessions)
5. Stop notification dispatcher (close MQTT connections)
6. Stop audit writer (flush pending writes, close InfluxDB client)
7. Log final stats (events processed, actions taken, errors)
```

### Main Event Loop

```python
async def run(self) -> None:
    """Main loop. Blocks until shutdown signal received."""
    await self._start_components()
    self._install_signal_handlers()

    try:
        while not self._shutting_down:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue  # Check shutdown flag
            if event is None:  # Poison pill for shutdown
                break
            await self._process_one(event)
    except asyncio.CancelledError:
        pass
    finally:
        await self._shutdown()


async def _process_one(self, event: Event) -> None:
    """Process a single event through the full pipeline.

    Errors are caught per-event — one failure never kills the loop.
    """
    try:
        # 1. TTL check
        if self._is_expired(event):
            logger.info("Event %s expired (TTL), dropping", event.id)
            return

        # 2. Decision (T0 → T1 → T2, guardrails applied)
        result = await self._decision_engine.process_event(event)

        # 3. Audit the decision (best-effort)
        await self._audit_decision(event, result)

        # 4. Handler dispatch (if action required and not dry-run)
        if self._should_execute(result):
            action_result = await self._dispatch_handler(event, result)
            await self._audit_action(event, result, action_result)

        # 5. Notification (if warranted)
        if self._should_notify(result):
            await self._send_notification(event, result)

    except Exception:
        logger.exception("Unhandled error processing event %s", event.id)
```

### Pipeline Dispatch Rules

The orchestrator maps `DecisionResult` fields to downstream actions:

| Disposition | Risk Tier | Handler | Audit | Notify |
|---|---|---|---|---|
| `MATCHED` | `AUTO_FIX` | Execute | Decision + Action | On success or failure |
| `MATCHED` | `RECOMMEND` | Skip (pending queue in §16.3) | Decision | Yes — with recommended action |
| `MATCHED` | `ESCALATE` | Skip | Decision | Yes — with full context |
| `MATCHED` | `BLOCK` | Skip | Decision | No (silent deny) |
| `DROPPED` | — | Skip | Decision only | No |
| `ESCALATE_HUMAN` | — | Skip | Decision | Yes — with T1 context |
| `ESCALATE_T2` | — | Handled inside DecisionEngine | Decision | Depends on T2 result |

### Error Isolation

- **Event processing failure**: Logged, event skipped, loop continues.
- **Handler failure**: Recorded as `ActionResult(success=False)`, audited,
  circuit breaker incremented, notification sent.
- **Audit write failure**: Logged as warning, pipeline continues.
  Audit is best-effort — it must never block processing.
- **Notification failure**: Logged as warning, pipeline continues.
  Notification is best-effort.
- **LLM timeout/error**: DecisionEngine handles internally (retry, fallback).
  If all retries fail, disposition is `ESCALATE_HUMAN` with degraded flag.

### Backpressure

- `asyncio.Queue(maxsize=config.agent.event_queue_size)` — ingestion adapters
  block on `queue.put()` when full, creating natural backpressure.
- Expired events (past TTL) are dropped at dequeue time, not at enqueue.
- Dedup is handled by the decision engine (not the orchestrator).

### Interface

```python
class Orchestrator:
    def __init__(self, config: OasisAgentConfig) -> None: ...

    async def run(self) -> None:
        """Start all components and enter the main event loop.
        Blocks until shutdown signal. This is the application entry point."""

    async def shutdown(self) -> None:
        """Graceful shutdown. Called by signal handler or externally."""
```

`__main__.py` is thin:

```python
async def main() -> None:
    config = load_config()
    orchestrator = Orchestrator(config)
    await orchestrator.run()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 16. Phase 2 — Extended Capabilities

Phase 2 builds on the running system from Phase 1 + the orchestrator. Each
item below is scoped for a single PR or a tightly coupled pair. The system
is deployable and useful after each item ships — there are no cross-item
dependencies except where noted.

### Priority Order

1. Orchestrator (§15) — makes the system runnable → tag v0.1.0
2. T2 deep reasoning prompts
3. Human-in-the-loop approval queue
4. Verification loop
5. Event correlation
6. Docker handler
7. Notification channels: email, webhook
8. Grafana dashboard templates
9. Observability: Prometheus metrics endpoint

### Phase 2 Checklist

- [x] Orchestrator + `__main__.py` entry point (§15)
- [x] T2 deep reasoning prompts (§16.1)
- [x] Human-in-the-loop approval queue (§16.2)
- [x] Approval queue CLI (§16.3)
- [x] Verification loop (§16.4)
- [x] Event correlation (§16.5)
- [x] Docker handler (§16.6)
- [x] Notification channels: email + webhook (§16.7)
- [x] Grafana dashboard templates (§16.8)
- [x] Prometheus metrics endpoint (§16.9)
- [x] Phase 2 test pass + documentation update
- [x] Tag v0.2.0

---

### 16.1 T2 Deep Reasoning Prompts

**What**: Implement `llm/prompts/diagnose_failure.py` — the prompt template
that packages T1's context for the cloud reasoning model and parses the
structured `DiagnosisResult` response.

**Models**: `DiagnosisResult` and `RecommendedAction` already defined in §5.
No new models needed.

**Prompt structure**:

- System message: OasisAgent role, safety constraints, output JSON schema
- User message: original Event, T1 `TriageResult`, entity state history
  (from `handler.get_context()`), relevant known_fixes (so T2 doesn't
  re-derive known solutions)
- Response format: JSON matching `DiagnosisResult` schema

**Integration**: `DecisionEngine` already has the `ESCALATE_T2` code path.
Wire the `diagnose_failure` prompt builder → `LLMClient.complete(role=REASONING)`
→ parse `DiagnosisResult` → re-enter guardrails check for each
`RecommendedAction`.

**Files**: `oasisagent/llm/prompts/diagnose_failure.py`,
`tests/test_prompts_t2.py`

---

### 16.2 Human-in-the-Loop Approval Queue

**What**: RECOMMEND-tier actions publish to a pending queue. An operator
approves or rejects via MQTT, CLI, or Claude Code. Approved actions execute
through the normal handler pipeline.

**Flow**:

```
DecisionResult(risk_tier=RECOMMEND)
  → Orchestrator creates PendingAction(id, event, action, expires_at)
  → Publishes to oasis/pending/{action_id} (JSON payload, QoS 1, retain)
  → Starts expiry timer

Operator approves (via CLI, Claude Code, or raw MQTT publish):
  → Message on oasis/approve/{action_id}
  → Orchestrator matches to PendingAction
  → Dispatches to handler → audit → notify

Operator rejects:
  → Message on oasis/reject/{action_id}
  → Orchestrator marks as rejected → audit

Expiry timer fires:
  → Orchestrator marks as expired → escalate → notify
```

**MQTT topics**:

- `oasis/pending/{action_id}` — agent publishes (QoS 1, retain=true)
- `oasis/approve/{action_id}` — operator publishes to approve
- `oasis/reject/{action_id}` — operator publishes to reject
- `oasis/pending/list` — agent publishes current queue snapshot on change
  (JSON array of PendingAction summaries, retain=true)

The `oasis/pending/list` topic enables any MQTT client (including Home
Assistant MQTT sensors, Node-RED dashboards, or raw `mosquitto_sub`) to
display the current queue without polling.

**New model**:

```python
class PendingAction(BaseModel):
    id: str                          # UUID
    event_id: str                    # Source event
    action: RecommendedAction
    diagnosis: str                   # Human-readable summary
    created_at: datetime
    expires_at: datetime             # Config: approval_timeout_minutes
    status: PendingStatus            # pending, approved, rejected, expired

class PendingStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
```

**Config addition**:

```yaml
guardrails:
  approval_timeout_minutes: 30      # RECOMMEND actions expire after this
```

**Files**: `oasisagent/engine/pending.py`, `tests/test_pending.py`

---

### 16.3 Approval Queue CLI

**What**: A lightweight CLI for operators to view and act on the pending
approval queue. This is the primary operator interface for Phase 2 — no
web UI needed.

**Commands**:

```bash
# List pending actions
oasisagent queue list

# Show details of a specific pending action
oasisagent queue show <action_id>

# Approve a pending action
oasisagent queue approve <action_id>

# Reject a pending action
oasisagent queue reject <action_id>

# Approve all pending actions (with confirmation prompt)
oasisagent queue approve-all
```

**Implementation**: The CLI connects to the same MQTT broker as the agent.
`queue list` subscribes to `oasis/pending/list` (retained message).
`queue approve/reject` publishes to the appropriate topic.

This means the CLI is stateless — it doesn't need direct access to the
agent process. Any machine with MQTT access can operate the queue.

**Claude Code / Cowork integration**: Because the queue is MQTT-based,
Claude Code can operate it via `aiomqtt` or `mosquitto_pub`/`mosquitto_sub`
commands. No special integration needed — the MQTT topics ARE the API.

**Files**: `oasisagent/cli.py`, `tests/test_cli.py`

---

### 16.4 Verification Loop

**What**: After `handler.execute()` succeeds, wait `verify_timeout` seconds,
then call `handler.verify()`. If verification fails, escalate.

**Flow**:

```
handler.execute() → ActionResult(success=True)
  → await asyncio.sleep(handler_config.verify_timeout)
  → handler.verify(event, action, result)
  → VerifyResult(verified=True)  → audit as verified
  → VerifyResult(verified=False) → escalate + notify + audit as verify_failed
```

**Impact**: The orchestrator's `_dispatch_handler()` method grows to include
the verify step. The `Handler` ABC already defines `verify()` (§8).

**Files**: Changes to `oasisagent/orchestrator.py`,
`oasisagent/handlers/homeassistant.py` (implement `verify()`),
`tests/test_verification.py`

---

### 16.5 Event Correlation

**What**: Group related events that arrive within a time window. Cascading
failures (e.g., network switch down → 10 entities unavailable) should be
treated as one incident, not ten.

**Strategy**:

- **Time-window grouping**: Events for the same `system` within
  `correlation_window` seconds share a `correlation_id`.
- **Group leader**: The first event in a correlation group is the leader.
  The decision engine processes the leader normally. Subsequent correlated
  events are marked with disposition `CORRELATED`, audited with a reference
  to the leader, but not independently processed through T0/T1/T2.
- **Entity relationship hints** (optional config): "if entity A fails,
  expect entity B to fail within N seconds" — suppresses downstream noise
  more aggressively than time-window alone.

**Integration point**: The correlator sits between the event queue and the
decision engine. The orchestrator calls `correlator.check(event)` before
`decision_engine.process_event(event)`.

**Config addition**:

```yaml
agent:
  correlation_window: 30            # seconds — 0 to disable
```

**Files**: `oasisagent/engine/correlator.py`, `tests/test_correlator.py`

---

### 16.6 Docker Handler

**What**: Implement the Docker handler stub from Phase 1.

**Operations** (per §8):

- `restart_container` — `POST /containers/{id}/restart`
- `get_container_logs` — `GET /containers/{id}/logs?tail=100`
- `get_container_stats` — `GET /containers/{id}/stats?stream=false`
- `inspect_container` — `GET /containers/{id}/json`

**Connection**: Unix socket (default) or TCP with optional TLS.
Use `aiohttp` with `aiohttp.connector.UnixConnector` for socket access.

**Known fixes**: Populate `known_fixes/docker.yaml` with common patterns:

- OOMKilled → restart with notification
- Health check failure → restart with backoff
- Exit code 137 (SIGKILL) → restart + log context
- Repeated crash loop → escalate (don't restart indefinitely)

**Verify**: After `restart_container`, poll container status until
`running` or timeout.

**Files**: `oasisagent/handlers/docker.py` (replace stub),
`known_fixes/docker.yaml`, `tests/test_docker_handler.py`

---

### 16.7 Notification Channels: Email + Webhook

**What**: Implement the two remaining Phase 2 notification channels.

**Email channel**:

- `aiosmtplib` for async SMTP
- Config already defined in §11
- Message format: plain text with structured sections (event summary,
  diagnosis, recommended action, audit trail link)
- Severity → subject line prefix: `[CRITICAL]`, `[ERROR]`, `[WARNING]`

**Webhook channel**:

- `aiohttp` POST to configured URL(s)
- Payload: `Notification` model serialized as JSON
- Retry on 5xx with exponential backoff (max 3 attempts, 1s/2s/4s)
- Timeout: 10 seconds per attempt
- Config already defined in §11

**Files**: `oasisagent/notifications/email.py`,
`oasisagent/notifications/webhook.py`,
`tests/test_notification_email.py`, `tests/test_notification_webhook.py`

---

### 16.8 Grafana Dashboard Templates

**What**: JSON dashboard templates that query the InfluxDB audit bucket.
Shipped as files in `dashboards/` — users import them into Grafana.

**Panels**:

- Event volume by source and severity (time series)
- Decision distribution: T0 / T1 / T2 tier breakdown (pie + bar)
- Disposition breakdown: matched / dropped / escalated (stacked bar)
- Action success/failure rate (time series)
- Circuit breaker status timeline (state timeline)
- LLM token usage and estimated cost per role (time series)
- Pending approval queue depth (gauge)
- Mean event processing latency (time series)

**Data source**: InfluxDB 2.x with Flux queries against the `oasisagent`
bucket. Dashboard variables for bucket name and time range.

**Files**: `dashboards/oasisagent-overview.json`,
`dashboards/README.md` (import instructions + screenshots)

---

### 16.9 Observability: Prometheus Metrics

**What**: Expose a `/metrics` endpoint for Prometheus scraping. Lightweight
HTTP server (`aiohttp`) on a configurable port.

**Metrics**:

| Metric | Type | Labels |
|---|---|---|
| `oasis_events_total` | counter | source, severity |
| `oasis_decisions_total` | counter | tier, disposition, risk_tier |
| `oasis_actions_total` | counter | handler, operation, result |
| `oasis_circuit_breaker_trips_total` | counter | trigger_type |
| `oasis_event_processing_seconds` | histogram | tier |
| `oasis_llm_requests_total` | counter | role, status |
| `oasis_llm_tokens_total` | counter | role, direction (prompt/completion) |
| `oasis_queue_depth` | gauge | — |
| `oasis_pending_actions` | gauge | — |
| `oasis_uptime_seconds` | gauge | — |

**Library**: `prometheus_client` (standard Python Prometheus client).
Exposes metrics via `aiohttp` handler, not a separate process.

**Config addition**:

```yaml
agent:
  metrics_port: 9090               # 0 to disable
```

**Files**: `oasisagent/metrics.py`, `tests/test_metrics.py`

---

## 17. Phase 3 — Production Operations

Phase 3 transforms OasisAgent from an operator tool into a production home
lab platform with a web admin UI as the primary configuration surface,
40+ service integrations, rich messaging channels, and advanced autonomous
capabilities.

See `docs/research/PHASE3-PLAN.md` for the comprehensive plan with API
research, integration catalog, and implementation details.

### Key Architectural Decisions

- **UI-first config**: Web UI is the primary config surface. Bootstrap with
  4 env vars → runtime config in SQLite (Fernet-encrypted secrets) → known
  fixes as YAML content files. See §17.0.
- **Single process**: FastAPI serves web UI + webhook receiver + REST API on
  one port. No separate webhook process.
- **WhatsApp dropped**: See ADR-001 in §17.4.

### Milestone Plan

| Version | Content | Tracking |
|---------|---------|----------|
| v0.3.0 | Foundation: config backend (SQLite + Fernet), FastAPI scaffold, webhook receiver, HTTP poller, Proxmox handler, Docker handler, MQTT expansion | Issues #47–#55 |
| v0.3.1 | Web Admin UI: HTMX + Jinja2, auth, setup wizard, dashboard, connectors, approval queue | Issue #56 |
| v0.3.2 | Messaging: InteractiveNotificationChannel ABC, Telegram, Slack, Discord | Issues #57–#60 |
| v0.3.3 | Networking: UniFi, Cloudflare, Servarr (Radarr + Sonarr) | Issues #61–#63 |
| v0.3.4 | Preventive scanning: certs, disk, backups, health sweeps | Issue #64 |
| v0.3.5 | Learning loop: T2→T0 candidate generation + promotion | Issue #65 |
| v0.3.6 | Plugins, multi-instance coordination, Tier 3 integrations | Issues #66–#68 |
| v1.0.0 | Full test pass, documentation, migration guide | Epic #70 |

### Phase 3 Checklist

- [ ] Config backend: SQLite schema + Fernet encryption + connector CRUD API (§17.0)
- [ ] Config import/export CLI commands (§17.0)
- [ ] First-run setup wizard API (§17.0)
- [ ] FastAPI application scaffold — single process (§17.1)
- [ ] Webhook receiver ingestion adapter (§17.1a)
- [ ] HTTP polling ingestion adapter with JMESPath (§17.1b)
- [ ] Proxmox VE handler (§17.5)
- [ ] Docker/Portainer handler
- [ ] MQTT topic expansion (Zigbee2MQTT, Frigate, ESPresence, Valetudo)
- [ ] Web admin UI with auth + RBAC (§17.1)
- [ ] Telegram notification + approval channel (§17.2)
- [ ] Slack notification + approval channel (§17.3)
- [ ] Discord webhook notification channel
- [ ] UniFi, Cloudflare, Servarr integrations
- [ ] Preventive scanning (§17.6)
- [ ] Learning loop (§17.7)
- [ ] Multi-instance coordination (§17.8)
- [ ] Plugin system (§17.9)
- [ ] Tier 3 integrations (Stalwart, EMQX, Synology, N8N, Nextcloud, Ollama)
- [ ] Phase 3 test pass + documentation update
- [ ] Tag v1.0.0

---

### 17.0 Configuration Architecture — UI-First

**What**: The web UI is the primary configuration surface for OasisAgent.
Operators never need to edit YAML or manage dozens of env vars to configure
integrations. This follows the pattern established by Home Assistant, Grafana,
Portainer, and Uptime Kuma.

**Three-layer config model**:

| Layer | What | Where | Managed By |
|-------|------|-------|------------|
| Bootstrap | Port, data dir, secret key, log level | 4 env vars | `docker-compose.yml` |
| Runtime config | All integrations, core services (MQTT, InfluxDB), notification channels, scanner settings | SQLite (secrets encrypted with Fernet) | Web UI + REST API |
| Content | Known fixes YAML, prompt templates | Files on disk (mountable volume) | Git / file mount |

**Bootstrap env vars (exhaustive list)**:

- `OASIS_PORT` — Listen port (default: `8080`)
- `OASIS_DATA_DIR` — SQLite + data directory (default: `/data`)
- `OASIS_SECRET_KEY` — Fernet key for encrypting secrets at rest
  (auto-generated on first run if missing)
- `OASIS_LOG_LEVEL` — Logging level (default: `info`)

Everything else — MQTT broker URL, InfluxDB endpoint, HA token, Proxmox
connections, Telegram bot token, polling intervals — is configured through
the web UI and stored in SQLite.

**Secrets handling**: All tokens, passwords, and API keys entered through the
UI are encrypted at rest using Fernet symmetric encryption (from the
`cryptography` package, already a transitive dependency). The
`OASIS_SECRET_KEY` env var is the sole root of trust. Secrets are decrypted
in-memory only when an adapter or handler needs them.

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

**SQLite migration strategy**: A `schema_version` integer is stored in the
database. On startup, the agent checks the version and runs sequential
migration scripts from `migrations/` (`001_initial.py`,
`002_add_scanner_config.py`, etc.). No Alembic — simple numbered scripts.
Config export embeds the schema version for import compatibility checks.

**Files**: `oasisagent/db/` package (schema.py, crypto.py, migrations/)

---

### 17.1 Web Admin UI

**What**: A web-based dashboard and configuration interface for OasisAgent.
This is the primary operator interface (CLI remains available for automation).

**Stack**:

- Backend: FastAPI (async, integrates naturally with existing aiohttp/asyncio)
- Frontend: HTMX + Jinja2 templates (no JavaScript build toolchain)
- Client-side interactivity: Alpine.js (3KB)
- Styling: Tailwind CSS via CDN
- Auth: Local user database (SQLite) with bcrypt password hashing
- 2FA: TOTP (Google Authenticator, Authy, etc.) via `pyotp`
- Sessions: JWT with httpOnly cookies, configurable expiry
- Real-time: Server-Sent Events (SSE) via `sse-starlette`

**Single process**: The web UI, REST API, and webhook receiver run as a
single FastAPI application, single uvicorn process, single port. Route
mount points:

- `/` — Admin UI (HTMX pages)
- `/api/v1/` — REST API (consumed by UI + external automation)
- `/ingest/webhook/{source}` — Webhook receiver for push-based ingestion
- `/healthz` — Health check

**RBAC roles**:

| Role | Permissions |
|---|---|
| `admin` | Full access: approve/reject, config changes, user management |
| `operator` | Approve/reject pending actions, view dashboards, ack alerts |
| `viewer` | Read-only: dashboards, event history, audit trail |

**Pages**:

- **Setup Wizard**: First-run flow — admin account → core services → first
  integration
- **Dashboard**: Real-time event feed (SSE), queue depth, circuit breaker
  status, recent actions
- **Connectors**: Add/configure/test integrations (frontend to connector
  CRUD API in §17.0)
- **Approval Queue**: List pending actions, approve/reject with optional
  comment, bulk operations
- **Event Explorer**: Search and filter historical events, drill into
  decision chain and audit trail
- **Known Fixes Browser**: Read-only view of YAML fix registry
- **Users**: User management (admin only), role assignment, 2FA enrollment

**API**: REST endpoints under `/api/v1/` — the UI consumes these, but they're
also available for external automation. Authenticated with the same JWT tokens.

**Files**: `oasisagent/ui/` package (app.py, auth.py, routes/, templates/,
static/), `tests/test_ui/`

---

### 17.2 Telegram Integration

**What**: Bidirectional Telegram bot for notifications and approval actions.
Operators receive alerts and can approve/reject directly from the chat.

**Capabilities**:

- **Outbound**: Send notifications (formatted with Markdown) to a configured
  chat or group. Severity → emoji prefix for visual scanning.
- **Inbound**: Inline keyboard buttons on RECOMMEND notifications
  (`✅ Approve` / `❌ Reject`). Bot verifies sender against allowed user list.
- **Commands**: `/status` (agent health), `/queue` (pending actions),
  `/approve <id>`, `/reject <id>`, `/mute <duration>` (suppress notifications)

**Library**: `aiogram` (async Telegram bot framework).

**Auth**: Telegram user IDs mapped to OasisAgent roles in config. Only
users in the allowed list can issue commands or approve actions.

**Config addition**:

```yaml
notifications:
  telegram:
    enabled: false
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}       # Default notification target
    allowed_users:                      # Telegram user IDs authorized for commands
      - 123456789
    min_severity: warning               # Don't send info-level to Telegram
```

**Files**: `oasisagent/notifications/telegram.py`, `tests/test_notification_telegram.py`

---

### 17.3 Slack Integration

**What**: Slack app with notifications and interactive approval buttons.

**Capabilities**:

- **Outbound**: Post to a configured channel. Rich message formatting with
  Block Kit (severity color bar, event summary, action buttons).
- **Inbound**: Interactive message buttons for approve/reject. Slack
  user ID mapped to OasisAgent roles.
- **Slash commands**: `/oasis status`, `/oasis queue`, `/oasis approve <id>`

**Library**: `slack_sdk` (async mode) or raw `aiohttp` to Slack API.

**Auth**: Slack workspace + channel permissions. Bot token scopes:
`chat:write`, `commands`, `incoming-webhook`.

**Config addition**:

```yaml
notifications:
  slack:
    enabled: false
    bot_token: ${SLACK_BOT_TOKEN}
    channel: "#oasis-alerts"
    allowed_users: []                  # Slack user IDs for approval actions
    min_severity: warning
```

**Files**: `oasisagent/notifications/slack.py`, `tests/test_notification_slack.py`

---

### 17.4 WhatsApp Integration — DROPPED (ADR-001)

**Status**: Dropped from Phase 3 scope.

**Decision**: WhatsApp Business API integration is not viable for a
self-hosted home lab tool.

**Rationale**:

- Meta has effectively killed the self-hosted (on-premise) WhatsApp Business
  API. Cloud API is the only viable path.
- Message templates must go through Meta's approval process (24–48 hours per
  template). Every notification format change requires re-approval.
- The 24-hour messaging window means you can only send template messages to
  users who haven't messaged the bot recently. For a monitoring system that
  sends unsolicited alerts, this is a significant constraint.
- Per-message cost ($0.005–0.05 depending on region and message type) adds
  ongoing OpEx for a home lab tool.
- No interactive components for approvals — would be notification-only.
- Telegram and Slack fully cover the interactive approval use case with zero
  per-message cost.

**Alternative**: If an operator needs WhatsApp alerts, the existing webhook
notification channel can POST to WhatsApp Cloud API with minimal custom code.
This doesn't require first-class integration.

---

### 17.5 Proxmox Handler

**What**: Implement the Proxmox handler stub from Phase 1.

**Operations** (per §8):

- `restart_vm` / `restart_ct` — `POST /nodes/{node}/qemu/{vmid}/status/reboot`
  (or `lxc/{vmid}`)
- `get_vm_status` / `get_ct_status` — `GET /nodes/{node}/qemu/{vmid}/status/current`
- `get_task_log` — `GET /nodes/{node}/tasks/{upid}/log`
- `get_node_status` — `GET /nodes/{node}/status`

**Connection**: Proxmox REST API with token-based auth. TLS with optional
`verify_ssl: false` for self-signed certs.

**Known fixes**: Populate `known_fixes/proxmox.yaml` with patterns:

- VM/CT stopped unexpectedly → restart with notification
- Node high memory → identify top consumers, notify
- Backup task failed → notify with task log context
- ZFS pool degraded → escalate immediately (never auto-fix storage)

**Files**: `oasisagent/handlers/proxmox.py` (replace stub),
`known_fixes/proxmox.yaml`, `tests/test_proxmox_handler.py`

---

### 17.6 Preventive Scanning

**What**: Scheduled scans that detect issues before they cause failures.
Runs on a configurable interval (e.g., every 15 minutes).

**Scan types**:

- **HA integration health**: Check all integrations for `setup_error` state
- **Docker container health**: Check for unhealthy or restarting containers
- **Certificate expiry**: Check TLS certificates on configured endpoints
- **Disk space**: Check available space on configured paths/hosts
- **Backup freshness**: Verify last backup timestamp is within threshold

**Integration**: Scans produce `Event` objects with `source: "scanner"` and
feed into the normal pipeline. This means T0/T1/T2, guardrails, audit, and
notifications all apply to preventive findings automatically.

**Config addition**:

```yaml
scanner:
  enabled: false
  interval: 900                     # seconds (15 minutes)
  checks:
    ha_integrations: true
    docker_health: true
    certificate_expiry:
      enabled: true
      endpoints:
        - https://ha.local:8123
        - https://grafana.local:3000
      warning_days: 30
    disk_space:
      enabled: false
      paths: []
      warning_threshold_pct: 85
    backup_freshness:
      enabled: false
      max_age_hours: 48
```

**Files**: `oasisagent/scanner/` package, `tests/test_scanner/`

---

### 17.7 Learning Loop

**What**: When T2 produces a successful diagnosis that is verified by the
operator, automatically generate a candidate T0 known fix entry.

**Flow**:

```
T2 DiagnosisResult → handler executes → verify succeeds
  → Generate candidate YAML entry matching the Event pattern
  → Write to known_fixes/candidates/ directory
  → Notify operator: "New known fix candidate: {id}. Review and promote."
```

Candidates are NOT automatically loaded into the registry. The operator
reviews, edits if needed, and moves to `known_fixes/` to activate.

**Versioning**: File-based with `_meta.status` (candidate/promoted/rejected).
The agent never shells out to git (ADR-004). External git hooks can track the
`candidates/` directory if desired.

**Promotion thresholds** (configurable via web UI):

- `min_confidence`: 0.8 — T2 confidence threshold
- `min_verified_count`: 3 — must succeed N times before becoming a candidate
- `auto_promote`: false — human review always required in v1

**Candidate format**: Same YAML schema as known_fixes, with additional
metadata:

```yaml
# known_fixes/candidates/auto-2026-03-15-ha-zwave-timeout.yaml
_meta:
  generated_by: t2_learning_loop
  source_event_id: "abc-123"
  confidence: 0.87
  verified_count: 3
  generated_at: "2026-03-15T14:30:00Z"
  status: candidate                   # candidate → promoted → rejected

fixes:
  - id: ha-zwave-timeout-restart
    match:
      system: homeassistant
      event_type: integration_failure
      payload_contains: "zwave timeout"
    # ... standard known_fix schema
```

**Files**: `oasisagent/engine/learning.py`, `tests/test_learning.py`

---

### 17.8 Multi-Instance Coordination

**What**: Support multiple OasisAgent instances (e.g., one per site, or
primary + backup) with leader election and work distribution.

**Approach**: MQTT-based leader election using retained messages and LWT
(Last Will and Testament). The leader processes events; standbys monitor
and take over if the leader's LWT fires.

**Topics**:

- `oasis/cluster/leader` — retained, contains leader instance ID and heartbeat
- `oasis/cluster/members/{instance_id}` — each instance publishes heartbeat

**Behavior**:

- On startup, check `oasis/cluster/leader`. If empty or stale, claim leadership.
- Leader processes events normally. Standbys subscribe to audit topics for
  state awareness but do not process events.
- If leader LWT fires (disconnect), standbys race to claim leadership.
  First to publish retained message to `oasis/cluster/leader` wins.

**Config addition**:

```yaml
agent:
  instance_id: oasis-primary        # Unique per instance
  cluster:
    enabled: false
    heartbeat_interval: 10          # seconds
    leader_timeout: 30              # seconds before assuming leader is dead
```

**Files**: `oasisagent/cluster.py`, `tests/test_cluster.py`

---

### 17.9 Plugin System

**What**: Allow community-contributed handlers, known_fixes, ingestion
adapters, and notification channels to be installed as plugins.

**Plugin structure**:

```
plugins/
  my-plugin/
    plugin.yaml            # Metadata: name, version, author, type, dependencies
    handler.py             # If type includes "handler"
    known_fixes/           # If type includes "known_fixes"
    adapter.py             # If type includes "ingestion"
    channel.py             # If type includes "notification"
```

**Discovery**: Plugins are loaded from `plugins/` directory on startup.
Each plugin registers its components with the appropriate registry
(handler registry, known_fixes registry, etc.).

**Isolation**: Plugins run in the same process but are loaded via
`importlib`. A plugin crash is caught and logged; it doesn't bring
down the agent.

**Config**:

```yaml
plugins:
  directory: ./plugins
  enabled:
    - my-custom-handler
    - community-unifi-fixes
  disabled: []                      # Explicit blocklist overrides enabled
```

**Files**: `oasisagent/plugins/` package (loader.py, registry.py),
`tests/test_plugins/`
