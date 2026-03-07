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
from typing import Annotated, Any
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


class DiagnosisResult(BaseModel):
    """Structured output from T2 deep reasoning diagnosis."""

    model_config = ConfigDict(extra="forbid")

    root_cause: str
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
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
# Notification (§10)
# ---------------------------------------------------------------------------


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
