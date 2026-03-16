"""Decision engine — core orchestrator for the event pipeline.

Receives events from the queue, runs them through T0 known fixes lookup,
then T1 triage (if no T0 match), then T2 reasoning (if T1 escalates),
applies guardrails, and produces DecisionResults.

ARCHITECTURE.md §6 describes the decision flow.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from oasisagent.engine.guardrails import GuardrailResult  # noqa: TC001 — Pydantic field type
from oasisagent.models import RecommendedAction  # noqa: TC001 — Pydantic field type

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from oasisagent.engine.guardrails import GuardrailsEngine
    from oasisagent.engine.known_fixes import KnownFix, KnownFixRegistry
    from oasisagent.engine.service_graph import ServiceGraph
    from oasisagent.llm.reasoning import ReasoningService
    from oasisagent.llm.triage import TriageService
    from oasisagent.models import DependencyContext, Event, TriageResult

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
    CORRELATED = "correlated"


class DecisionResult(BaseModel):
    """Outcome of processing a single event through the decision engine."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    tier: DecisionTier
    disposition: DecisionDisposition
    matched_fix_id: str | None = None
    diagnosis: str = ""
    guardrail_result: GuardrailResult | None = None
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DecisionEngine:
    """Orchestrates event processing through T0 lookup, T1 triage, T2 reasoning,
    and guardrails.

    The engine:
    1. Looks up events against the T0 known fixes registry
    2. If no T0 match and triage_service is available, calls T1 SLM
    3. If T1 escalates to T2, calls the cloud reasoning model
    4. Applies guardrails to each recommended action
    5. Returns a DecisionResult for every event

    T2 may return multiple RecommendedActions. Each action is independently
    checked against guardrails. The DecisionResult carries the full list
    so the orchestrator can dispatch each one separately.
    """

    def __init__(
        self,
        registry: KnownFixRegistry,
        guardrails: GuardrailsEngine,
        triage_service: TriageService | None = None,
        reasoning_service: ReasoningService | None = None,
        service_graph: ServiceGraph | None = None,
        dependency_context_depth: int = 2,
    ) -> None:
        self._registry = registry
        self._guardrails = guardrails
        self._triage = triage_service
        self._reasoning = reasoning_service
        self._service_graph = service_graph
        self._dependency_depth = max(1, min(dependency_context_depth, 5))

    def set_service_graph(self, graph: ServiceGraph) -> None:
        """Set or update the service graph (deferred wiring)."""
        self._service_graph = graph

    async def process_event(
        self,
        event: Event,
        entity_context_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        dependency_context: DependencyContext | None = None,
    ) -> DecisionResult:
        """Process a single event through the decision pipeline.

        Args:
            event: The event to process.
            entity_context_fn: Lazy callable that gathers multi-handler
                context. Only invoked when the pipeline reaches T2, so
                handler API calls are skipped for T0 matches and T1 drops.
            dependency_context: Pre-computed dependency subgraph from the
                service topology. Falls back to internal computation if
                None and a service graph is available.

        Returns a DecisionResult regardless of outcome — every event
        that enters the engine produces an auditable result.
        """
        # T0: Known fixes lookup
        fix = self._registry.match(event)

        if fix is not None:
            return self._apply_t0_fix(event, fix)

        # T1: Triage via local SLM (if available)
        if self._triage is not None:
            return await self._apply_t1_triage(
                event, entity_context_fn, dependency_context,
            )

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

        details: dict[str, Any] = {
            "guardrail_rule": guardrail_result.rule_name,
        }

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
                details=details,
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
                details=details,
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
            details=details,
        )

    async def _apply_t1_triage(
        self,
        event: Event,
        entity_context_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        dependency_context: DependencyContext | None = None,
    ) -> DecisionResult:
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
            if self._reasoning is not None:
                return await self._apply_t2_reasoning(
                    event, triage_result, entity_context_fn, dependency_context,
                )
            # No reasoning service configured — escalate to human
            logger.info(
                "T1 escalated to T2 but reasoning service not configured, "
                "escalating to human for event %s",
                event.id,
            )
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

    async def _apply_t2_reasoning(
        self,
        event: Event,
        triage_result: TriageResult,
        entity_context_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        dependency_context: DependencyContext | None = None,
    ) -> DecisionResult:
        """Call T2 reasoning and apply guardrails to each recommended action.

        Each action is independently checked against guardrails. Actions
        that pass are included in the result; blocked actions are filtered
        out. If no actions pass, the event is escalated to a human.
        """
        from oasisagent.engine.service_graph import gather_dependency_context

        assert self._reasoning is not None

        # Lazy: only call handler APIs when we actually reach T2
        entity_context: dict[str, Any] = {}
        if entity_context_fn is not None:
            entity_context = await entity_context_fn()

        # Use passed dependency context; fall back to internal computation
        dependency_ctx = dependency_context
        if dependency_ctx is None and self._service_graph is not None:
            dependency_ctx = gather_dependency_context(
                event.entity_id, self._service_graph, self._dependency_depth,
            )

        diagnosis = await self._reasoning.diagnose(
            event, triage_result, entity_context=entity_context, known_fixes=[],
            dependency_context=dependency_ctx,
        )

        # Track which handler systems provided context (for audit)
        context_systems = sorted(
            {k.split(":")[1] for k in entity_context if k.startswith("dependency:")}
        )
        if "primary" in entity_context:
            context_systems = [event.system, *context_systems]

        def _add_dependency_fields(details: dict[str, Any]) -> None:
            if dependency_ctx is not None:
                details["dependency_upstream"] = [
                    n.entity_id for n in dependency_ctx.upstream
                ]
                details["dependency_downstream"] = [
                    n.entity_id for n in dependency_ctx.downstream
                ]
                details["dependency_same_host"] = [
                    n.entity_id for n in dependency_ctx.same_host
                ]
                details["dependency_depth"] = self._dependency_depth
            if context_systems:
                details["multi_handler_context_systems"] = context_systems

        if not diagnosis.recommended_actions:
            logger.info(
                "T2 returned no actions for event %s, escalating to human",
                event.id,
            )
            details: dict[str, Any] = {
                "escalate_to": "human",
                "t2_root_cause": diagnosis.root_cause,
                "t2_confidence": diagnosis.confidence,
                "risk_assessment": diagnosis.risk_assessment,
                "confidence": diagnosis.confidence,
            }
            _add_dependency_fields(details)
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T2,
                disposition=DecisionDisposition.ESCALATED,
                diagnosis=diagnosis.root_cause,
                details=details,
            )

        # Apply guardrails to each action independently
        approved_actions: list[RecommendedAction] = []
        blocked_count = 0
        guardrail_rules: list[str] = []

        for action in diagnosis.recommended_actions:
            check_entity = action.target_entity_id or event.entity_id
            guardrail_result = self._guardrails.check(
                entity_id=check_entity,
                risk_tier=action.risk_tier,
            )

            if guardrail_result.allowed and not guardrail_result.dry_run:
                approved_actions.append(action)
            else:
                blocked_count += 1
                guardrail_rules.append(guardrail_result.rule_name)
                logger.info(
                    "T2 action blocked by guardrails for event %s: "
                    "handler=%s, op=%s, reason=%s",
                    event.id,
                    action.handler,
                    action.operation,
                    guardrail_result.reason,
                )

        if not approved_actions:
            logger.info(
                "All %d T2 actions blocked by guardrails for event %s, "
                "escalating to human",
                blocked_count,
                event.id,
            )
            details = {
                "t2_root_cause": diagnosis.root_cause,
                "t2_confidence": diagnosis.confidence,
                "confidence": diagnosis.confidence,
                "risk_assessment": diagnosis.risk_assessment,
                "blocked_count": blocked_count,
                "guardrail_rule": ", ".join(guardrail_rules),
            }
            _add_dependency_fields(details)
            return DecisionResult(
                event_id=event.id,
                tier=DecisionTier.T2,
                disposition=DecisionDisposition.BLOCKED,
                diagnosis=diagnosis.root_cause,
                recommended_actions=diagnosis.recommended_actions,
                details=details,
            )

        logger.info(
            "T2 diagnosis for event %s: %d actions approved, %d blocked",
            event.id,
            len(approved_actions),
            blocked_count,
        )

        details = {
            "t2_root_cause": diagnosis.root_cause,
            "t2_confidence": diagnosis.confidence,
            "confidence": diagnosis.confidence,
            "risk_assessment": diagnosis.risk_assessment,
            "total_actions": len(diagnosis.recommended_actions),
            "approved_actions": len(approved_actions),
            "blocked_count": blocked_count,
        }
        _add_dependency_fields(details)

        return DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T2,
            disposition=DecisionDisposition.MATCHED,
            diagnosis=diagnosis.root_cause,
            recommended_actions=approved_actions,
            details=details,
        )
