"""Deterministic safety guardrails.

Guardrails are enforced in code, never by model judgment. They evaluate
risk tier policy, blocked domains/entities, kill switch, and dry run
mode. Every action must pass through guardrails before execution.

ARCHITECTURE.md §7 describes the guardrail design.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from oasisagent.models import RiskTier

if TYPE_CHECKING:
    from oasisagent.config import GuardrailsConfig

logger = logging.getLogger(__name__)


class GuardrailResult(BaseModel):
    """Outcome of a guardrail check."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    dry_run: bool = False
    risk_tier: RiskTier


# Risk tiers that are allowed through guardrails in Phase 1.
# auto_fix: execute automatically
# recommend: notify human (no auto-execution)
# escalate/block: blocked by guardrails
_ALLOWED_TIERS: frozenset[RiskTier] = frozenset({RiskTier.AUTO_FIX, RiskTier.RECOMMEND})


class GuardrailsEngine:
    """Evaluates safety guardrails against proposed actions.

    Check order (coarse to fine):
    1. Kill switch — blocks everything
    2. Risk tier policy — blocks escalate/block tiers
    3. Blocked domains — glob match against entity_id
    4. Blocked entities — exact match against entity_id
    5. Dry run — allows but flags for logging only

    All checks are deterministic. No LLM involvement.
    """

    def __init__(self, config: GuardrailsConfig) -> None:
        self._config = config

    def check(self, entity_id: str, risk_tier: RiskTier) -> GuardrailResult:
        """Evaluate guardrails for a proposed action.

        Returns a GuardrailResult indicating whether the action is allowed,
        blocked, or allowed in dry-run mode.
        """
        # 1. Kill switch — blocks everything
        if self._config.kill_switch:
            logger.warning("Guardrail BLOCKED: kill switch is active")
            return GuardrailResult(
                allowed=False,
                reason="Kill switch is active — all automated actions disabled",
                risk_tier=risk_tier,
            )

        # 2. Risk tier policy
        if risk_tier not in _ALLOWED_TIERS:
            logger.info(
                "Guardrail BLOCKED: risk_tier=%s is not in allowed tiers",
                risk_tier,
            )
            return GuardrailResult(
                allowed=False,
                reason=f"Risk tier '{risk_tier}' requires human review",
                risk_tier=risk_tier,
            )

        # 3. Blocked domains (glob match)
        for pattern in self._config.blocked_domains:
            if fnmatch(entity_id, pattern):
                logger.info(
                    "Guardrail BLOCKED: entity %s matches blocked domain pattern '%s'",
                    entity_id,
                    pattern,
                )
                return GuardrailResult(
                    allowed=False,
                    reason=f"Entity '{entity_id}' matches blocked domain '{pattern}'",
                    risk_tier=risk_tier,
                )

        # 4. Blocked entities (exact match)
        if entity_id in self._config.blocked_entities:
            logger.info(
                "Guardrail BLOCKED: entity %s is in blocked_entities list",
                entity_id,
            )
            return GuardrailResult(
                allowed=False,
                reason=f"Entity '{entity_id}' is explicitly blocked",
                risk_tier=risk_tier,
            )

        # 5. Dry run — allow but flag
        if self._config.dry_run:
            logger.info(
                "Guardrail DRY_RUN: action on %s would be allowed (risk_tier=%s)",
                entity_id,
                risk_tier,
            )
            return GuardrailResult(
                allowed=True,
                reason="Dry run mode — action logged but not executed",
                dry_run=True,
                risk_tier=risk_tier,
            )

        # All checks passed
        return GuardrailResult(
            allowed=True,
            reason="All guardrail checks passed",
            risk_tier=risk_tier,
        )
