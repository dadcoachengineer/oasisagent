"""Canonical data models for OasisAgent.

These are the core data contracts of the project. All ingestion adapters
produce Event objects. The decision engine, handlers, and audit system all
consume them. LLM tiers return TriageResult and DiagnosisResult. Handlers
return ActionResult and VerifyResult.

All models use Pydantic BaseModel for validation and serialization.
ARCHITECTURE.md §3/§5/§8 have been updated to reflect this choice
(the original spec used @dataclass for readability).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    """Event severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


SEVERITY_MAP: dict[str, Severity] = {
    "info": Severity.INFO,
    "warning": Severity.WARNING,
    "error": Severity.ERROR,
    "critical": Severity.CRITICAL,
}


class Disposition(StrEnum):
    """T1 triage dispositions — what to do with an event after classification."""

    DROP = "drop"
    KNOWN_PATTERN = "known_pattern"
    ESCALATE_T2 = "escalate_t2"
    ESCALATE_HUMAN = "escalate_human"


class RiskTier(StrEnum):
    """Risk tiers for recommended actions — determines guardrail behavior."""

    AUTO_FIX = "auto_fix"
    RECOMMEND = "recommend"
    ESCALATE = "escalate"
    BLOCK = "block"


class ActionStatus(StrEnum):
    """Outcome of a handler action execution."""

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    DRY_RUN = "dry_run"


# ---------------------------------------------------------------------------
# Event pipeline models (§3)
# ---------------------------------------------------------------------------


class EventMetadata(BaseModel):
    """Processing metadata attached to every Event."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str | None = None
    dedup_key: str = ""
    ttl: Annotated[int, Field(ge=0)] = 300
    retry_count: Annotated[int, Field(ge=0)] = 0


class Event(BaseModel):
    """Canonical event model — the core data contract of OasisAgent.

    All ingestion sources produce Events. The decision engine, handlers,
    and audit system all operate on this schema. Each processing stage
    appends to ``context`` so the full history is available for audit.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    system: str
    event_type: str
    entity_id: str
    severity: Severity
    timestamp: datetime
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: EventMetadata = Field(default_factory=EventMetadata)


# ---------------------------------------------------------------------------
# T1 Triage output (§5)
# ---------------------------------------------------------------------------


class TriageResult(BaseModel):
    """Structured output from T1 triage classification.

    T1 classifies and packages — it never decides to take action.
    The decision engine applies risk tiers based on the disposition.
    """

    model_config = ConfigDict(extra="forbid")

    disposition: Disposition
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    classification: str
    summary: str
    suggested_fix: str | None = None
    context_package: dict[str, Any] | None = None
    reasoning: str = ""


# ---------------------------------------------------------------------------
# T2 Diagnosis output (§5)
# ---------------------------------------------------------------------------


class RecommendedAction(BaseModel):
    """A single recommended action from T2 diagnosis."""

    model_config = ConfigDict(extra="forbid")

    description: str
    handler: str
    operation: str
    params: dict[str, Any] = Field(default_factory=dict)
    risk_tier: RiskTier
    reasoning: str = ""
    target_entity_id: str | None = None


class RemediationStep(BaseModel):
    """A single step in a multi-system remediation plan from T2."""

    model_config = ConfigDict(extra="forbid")

    order: Annotated[int, Field(ge=1)]
    action: RecommendedAction
    success_criteria: str
    depends_on: list[int] = Field(default_factory=list)
    conditional: bool = False


class DiagnosisResult(BaseModel):
    """Structured output from T2 deep reasoning diagnosis."""

    model_config = ConfigDict(extra="forbid")

    root_cause: str
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    remediation_plan: list[RemediationStep] | None = None
    risk_assessment: str = ""
    additional_context: str = ""
    suggested_known_fix: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Handler output (§8)
# ---------------------------------------------------------------------------


class ActionResult(BaseModel):
    """Result of executing a handler action."""

    model_config = ConfigDict(extra="forbid")

    status: ActionStatus
    details: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    duration_ms: Annotated[float, Field(ge=0.0)] | None = None


class VerifyResult(BaseModel):
    """Result of verifying a handler action had the desired effect."""

    model_config = ConfigDict(extra="forbid")

    verified: bool
    message: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Remediation plan execution (P2 plan-aware dispatch)
# ---------------------------------------------------------------------------


class PlanStatus(StrEnum):
    """Lifecycle states of a remediation plan."""

    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(StrEnum):
    """Lifecycle states of a single remediation step."""

    PENDING = "pending"
    READY = "ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class StepState(BaseModel):
    """Runtime state of a remediation step within an executing plan."""

    model_config = ConfigDict(extra="forbid")

    order: int
    status: StepStatus = StepStatus.PENDING
    action_result: ActionResult | None = None
    verify_result: VerifyResult | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


class RemediationPlan(BaseModel):
    """A tracked remediation plan with step-level state."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    event_id: str
    status: PlanStatus = PlanStatus.PENDING_APPROVAL
    steps: list[RemediationStep]
    step_states: list[StepState] = Field(default_factory=list)
    diagnosis: str = ""
    effective_risk_tier: RiskTier
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    entity_id: str = ""
    severity: str = ""
    source: str = ""
    system: str = ""


# ---------------------------------------------------------------------------
# Notification (§10)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Decision details contract (§ issue #218)
# ---------------------------------------------------------------------------


class DecisionDetails(TypedDict, total=False):
    """Explicit contract for DecisionResult.details dict.

    All fields optional. Downstream consumers (timeline UI, correlation
    engine, audit reader) can rely on these keys existing when the
    corresponding tier produced the decision.

    T0 fields:
        matched_fix_id: The known fix ID that matched.

    T1 fields:
        classification: Event category from T1 triage.
        confidence: T1 confidence score (0.0-1.0).
        reasoning: T1 model reasoning text.
        suggested_fix: T1 suggested fix (if disposition=KNOWN_PATTERN).

    T2 fields:
        t2_root_cause: Root cause analysis from T2 reasoning.
        t2_confidence: T2 confidence score (0.0-1.0).
        risk_assessment: T2 risk assessment text.
        total_actions: Total actions recommended by T2.
        approved_actions: Actions that passed guardrails.
        blocked_count: Actions blocked by guardrails.

    Guardrail fields:
        guardrail_rule: The specific rule/pattern name that triggered.

    Correlation fields:
        leader_event_id: Leader event ID for correlated events.
        escalate_to: Escalation target ("human", "t2").

    Suppression fields:
        suppressed_count: Number of consecutive events suppressed.
    """

    # T1
    classification: str
    confidence: float
    reasoning: str
    suggested_fix: str | None

    # T2
    t2_root_cause: str
    t2_confidence: float
    risk_assessment: str
    total_actions: int
    approved_actions: int
    blocked_count: int

    # Guardrails
    guardrail_rule: str

    # Correlation
    leader_event_id: str
    escalate_to: str

    # Suppression
    suppressed_count: int

    # Dependency context (T2)
    dependency_upstream: list[str]
    dependency_downstream: list[str]
    dependency_same_host: list[str]
    dependency_depth: int

    # Multi-handler context (T2)
    multi_handler_context_systems: list[str]

    # Remediation plan (T2)
    remediation_plan_steps: int


# ---------------------------------------------------------------------------
# Service topology (§ issue #218 M2a)
# ---------------------------------------------------------------------------


class TopologyNode(BaseModel):
    """A node in the service topology graph.

    Represents a service, host, or network device discovered by an adapter
    or manually added by the operator.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_type: str  # e.g. "service", "host", "network_device", "proxy"
    display_name: str = ""
    host_ip: str | None = None
    source: str = "manual"  # "manual" or "auto:<adapter_name>"
    manually_edited: bool = False
    last_seen: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TopologyEdge(BaseModel):
    """A directed edge between two topology nodes.

    Represents a dependency or relationship (e.g. "proxies_to",
    "runs_on", "depends_on", "connects_via").
    """

    model_config = ConfigDict(extra="forbid")

    from_entity: str
    to_entity: str
    edge_type: str  # e.g. "proxies_to", "runs_on", "depends_on", "connects_via"
    source: str = "manual"
    manually_edited: bool = False
    last_seen: datetime | None = None


class TopologyDiff(BaseModel):
    """A single change detected during topology discovery merge."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["added", "updated", "stale"]
    entity_type: Literal["node", "edge"]
    entity_id: str  # node entity_id or "from->to" for edges
    details: str = ""


class DependencyNode(BaseModel):
    """A node in a dependency subgraph extracted for T2 context."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_type: str
    display_name: str
    host_ip: str | None = None
    edge_type: str  # Edge from BFS parent (traversal edge, not relationship to root)
    depth: int


class DependencyEdgeInfo(BaseModel):
    """An edge in the dependency subgraph."""

    model_config = ConfigDict(extra="forbid")

    from_entity: str
    to_entity: str
    edge_type: str
    manually_edited: bool = False


class DependencyContext(BaseModel):
    """Structured dependency subgraph for T2 prompt injection."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_type: str | None = None
    host_ip: str | None = None
    upstream: list[DependencyNode] = Field(default_factory=list)
    downstream: list[DependencyNode] = Field(default_factory=list)
    same_host: list[DependencyNode] = Field(default_factory=list)
    edges: list[DependencyEdgeInfo] = Field(default_factory=list)


class Notification(BaseModel):
    """A notification to be dispatched via one or more channels.

    Not all notifications relate to events — agent lifecycle events
    (start, stop, internal error) also generate notifications.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    event_id: str | None = None
    channel: str = ""
    severity: Severity = Severity.INFO
    title: str
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)
