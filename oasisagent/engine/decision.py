"""Decision engine — core orchestrator for the event pipeline.

Receives events from the queue, runs them through T0 known fixes lookup,
then T1 triage (if no T0 match), applies guardrails, and produces
DecisionResults.

ARCHITECTURE.md §6 describes the decision flow.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from oasisagent.engine.guardrails import GuardrailResult  # noqa: TC001 — Pydantic field type

if TYPE_CHECKING:
    from oasisagent.engine.guardrails import GuardrailsEngine
    from oasisagent.engine.known_fixes import KnownFix, KnownFixRegistry
    from oasisagent.llm.triage import TriageService
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
    DROPPED = "dropped"
    ESCALATED = "escalated"


class DecisionResult(BaseModel):
    """Outcome of processing a single event through the decision engine."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    tier: DecisionTier
    disposition: DecisionDisposition
    matched_fix_id: str | None = None
    diagnosis: str = ""
    guardrail_result: GuardrailResult | None = None
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DecisionEngine:
    """Orchestrates event processing through T0 lookup, T1 triage, and guardrails.

    For Phase 1, the engine:
    1. Looks up events against the T0 known fixes registry
    2. If no T0 match and triage_service is available, calls T1 SLM
    3. Applies guardrails to matched/classified fixes
    4. Returns a DecisionResult for every event (matched, dropped, or escalated)

    T2 deep reasoning and handler dispatch are added in later items.
    """

    def __init__(
        self,
        registry: KnownFixRegistry,
        guardrails: GuardrailsEngine,
        triage_service: TriageService | None = None,
    ) -> None:
        self._registry = registry
        self._guardrails = guardrails
        self._triage = triage_service

    async def process_event(self, event: Event) -> DecisionResult:
        """Process a single event through the decision pipeline.

        Returns a DecisionResult regardless of outcome — every event
        that enters the engine produces an auditable result.
        """
        # T0: Known fixes lookup
        fix = self._registry.match(event)

        if fix is not None:
            return self._apply_t0_fix(event, fix)

        # T1: Triage via local SLM (if available)
        if self._triage is not None:
            return await self._apply_t1_triage(event)

        # No T0 match, no T1 available
        logger.debug(
            "No T0 match for event %s and no T1 triage available",
            event.id,
        )
        return DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T0,
            disposition=DecisionDisposition.UNMATCHED,
            diagnosis="No known fix matched and T1 triage is not configured",
        )

    def _apply_t0_fix(self, event: Event, fix: KnownFix) -> DecisionResult:
        """Apply guardrails to a T0-matched fix and return a result."""
        logger.info(
            "T0 match: event %s → fix '%s' (risk_tier=%s)",
            event.id,
            fix.id,
            fix.risk_tier,
        )

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

    async def _apply_t1_triage(self, event: Event) -> DecisionResult:
        """Classify an unmatched event via T1 SLM and return a result."""
        from oasisagent.models import Disposition

        triage_result = await self._triage.classify(event)

        logger.info(
            "T1 triage: event %s → disposition=%s, confidence=%.2f",
            event.id,
            triage_result.disposition,
            triage_result.confidence,
        )

        if triage_result.disposition == Disposition.DROP:
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T1,
                disposition=DecisionDisposition.DROPPED,
                diagnosis=triage_result.summary,
                details={
                    "classification": triage_result.classification,
                    "confidence": triage_result.confidence,
                    "reasoning": triage_result.reasoning,
                },
            )

        if triage_result.disposition == Disposition.ESCALATE_T2:
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T1,
                disposition=DecisionDisposition.ESCALATED,
                diagnosis=triage_result.summary,
                details={
                    "classification": triage_result.classification,
                    "confidence": triage_result.confidence,
                    "reasoning": triage_result.reasoning,
                    "escalate_to": "t2",
                },
            )

        if triage_result.disposition == Disposition.ESCALATE_HUMAN:
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T1,
                disposition=DecisionDisposition.ESCALATED,
                diagnosis=triage_result.summary,
                details={
                    "classification": triage_result.classification,
                    "confidence": triage_result.confidence,
                    "reasoning": triage_result.reasoning,
                    "escalate_to": "human",
                },
            )

        # KNOWN_PATTERN — T1 found a pattern but guardrails still apply
        if triage_result.disposition == Disposition.KNOWN_PATTERN:
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T1,
                disposition=DecisionDisposition.MATCHED,
                diagnosis=triage_result.summary,
                details={
                    "classification": triage_result.classification,
                    "confidence": triage_result.confidence,
                    "reasoning": triage_result.reasoning,
                    "suggested_fix": triage_result.suggested_fix,
                },
            )

        # Fallback — shouldn't happen but be safe
        return DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T1,
            disposition=DecisionDisposition.ESCALATED,
            diagnosis=f"Unexpected T1 disposition: {triage_result.disposition}",
        )
