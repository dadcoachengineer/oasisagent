# OasisAgent — Architecture Specification

> **Version**: 0.2.0
> **Status**: Pre-implementation design spec
> **Last updated**: March 7, 2026

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
- Polls HA's `/api/error/all` endpoint or reads log file directly
- Pattern-matches log entries against known error signatures
- Deduplicates based on error fingerprint + time window
- Config:
  ```yaml
  ingestion:
    ha_log_poller:
      enabled: true
      url: http://192.168.1.120:8123
      token: ${HA_TOKEN}
      poll_interval: 30          # seconds
      patterns:
        - regex: "Error setting up integration '(.+)'"
          event_type: integration_failure
          severity: error
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
- `get_error_log` — Fetch recent error log entries

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

### Phase 1 — Core Framework (target: 2 weeks)
- [ ] Project scaffolding (pyproject.toml, Docker, CI)
- [ ] Config loading and validation
- [ ] Canonical Event model
- [ ] Event queue (asyncio.Queue)
- [ ] Ingestion: MQTT adapter
- [ ] Ingestion: HA WebSocket adapter
- [ ] Ingestion: HA log poller
- [ ] T0: Known fixes registry + matcher
- [ ] Decision engine (orchestrator + guardrails)
- [ ] Circuit breaker
- [ ] LLM client (provider-agnostic wrapper)
- [ ] T1: Triage classification prompts
- [ ] HA handler (notify, restart_integration, reload_automations, get_context)
- [ ] Audit: InfluxDB writer
- [ ] Notifications: MQTT channel
- [ ] Tests for all above
- [ ] Docker image + compose file
- [ ] Documentation: README, config.example.yaml, .env.example

### Phase 2 — Extended Capabilities (target: 2 weeks)
- [ ] T2: Deep reasoning prompts + structured diagnosis
- [ ] Docker handler
- [ ] Notification channels: email, webhook
- [ ] Grafana remediation dashboard (JSON)
- [ ] Event correlation (grouping cascading failures)
- [ ] Verification loop (post-action check)
- [ ] Web UI for approval queue (RECOMMEND tier actions)

### Phase 3 — Advanced (target: 4 weeks)
- [ ] Proxmox handler
- [ ] Preventive scanning (detect issues before they cause failures)
- [ ] Learning loop (successful T2 diagnoses → T0 registry candidates)
- [ ] Multi-instance coordination (multiple agents, leader election)
- [ ] Plugin system for community-contributed handlers and known_fixes
