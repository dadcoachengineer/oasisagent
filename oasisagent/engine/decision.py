"""Decision engine — core orchestrator for the event pipeline.

Receives events from the queue, runs them through T0 known fixes lookup,
applies guardrails, and produces DecisionResults. T1/T2 triage and
handler dispatch are added in later Phase 1 items.

ARCHITECTURE.md §6 describes the decision flow.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from oasisagent.engine.guardrails import GuardrailResult  # noqa: TC001 — Pydantic field type

if TYPE_CHECKING:
    from oasisagent.engine.guardrails import GuardrailsEngine
    from oasisagent.engine.known_fixes import KnownFixRegistry
    from oasisagent.models import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DecisionTier(StrEnum):
    """Which reasoning tier produced the decision."""

    T0 = "t0"
    T1 = "t1"
    T2 = "t2"


class DecisionDisposition(StrEnum):
    """Outcome of the decision engine for a given event."""

    MATCHED = "matched"
    BLOCKED = "blocked"
    DRY_RUN = "dry_run"
    UNMATCHED = "unmatched"


class DecisionResult(BaseModel):
    """Outcome of processing a single event through the decision engine."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    tier: DecisionTier
    disposition: DecisionDisposition
    matched_fix_id: str | None = None
    diagnosis: str = ""
    guardrail_result: GuardrailResult | None = None
    details: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DecisionEngine:
    """Orchestrates event processing through T0 lookup and guardrails.

    For Phase 1, the engine:
    1. Looks up events against the T0 known fixes registry
    2. Applies guardrails to matched fixes
    3. Returns a DecisionResult for every event (matched or unmatched)

    T1/T2 triage, handler dispatch, and circuit breaker integration
    are added in subsequent Phase 1 items.
    """

    def __init__(
        self,
        registry: KnownFixRegistry,
        guardrails: GuardrailsEngine,
    ) -> None:
        self._registry = registry
        self._guardrails = guardrails

    def process_event(self, event: Event) -> DecisionResult:
        """Process a single event through the decision pipeline.

        Returns a DecisionResult regardless of outcome — every event
        that enters the engine produces an auditable result.
        """
        # T0: Known fixes lookup
        fix = self._registry.match(event)

        if fix is None:
            logger.debug(
                "No T0 match for event %s (system=%s, type=%s, entity=%s)",
                event.id,
                event.system,
                event.event_type,
                event.entity_id,
            )
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T0,
                disposition=DecisionDisposition.UNMATCHED,
                diagnosis="No known fix matched — pending T1/T2 processing",
            )

        logger.info(
            "T0 match: event %s → fix '%s' (risk_tier=%s)",
            event.id,
            fix.id,
            fix.risk_tier,
        )

        # Apply guardrails
        guardrail_result = self._guardrails.check(
            entity_id=event.entity_id,
            risk_tier=fix.risk_tier,
        )

        if not guardrail_result.allowed:
            logger.info(
                "Decision BLOCKED for event %s: %s",
                event.id,
                guardrail_result.reason,
            )
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T0,
                disposition=DecisionDisposition.BLOCKED,
                matched_fix_id=fix.id,
                diagnosis=fix.diagnosis,
                guardrail_result=guardrail_result,
            )

        if guardrail_result.dry_run:
            logger.info(
                "Decision DRY_RUN for event %s: fix '%s' would be applied",
                event.id,
                fix.id,
            )
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T0,
                disposition=DecisionDisposition.DRY_RUN,
                matched_fix_id=fix.id,
                diagnosis=fix.diagnosis,
                guardrail_result=guardrail_result,
            )

        logger.info(
            "Decision MATCHED for event %s: fix '%s' approved (handler=%s, op=%s)",
            event.id,
            fix.id,
            fix.action.handler,
            fix.action.operation,
        )
        return DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T0,
            disposition=DecisionDisposition.MATCHED,
            matched_fix_id=fix.id,
            diagnosis=fix.diagnosis,
            guardrail_result=guardrail_result,
        )
