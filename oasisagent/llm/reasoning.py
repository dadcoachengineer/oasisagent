"""T2 reasoning service — deep diagnosis using the cloud reasoning model.

Connects the LLMClient to the decision engine by converting events and T1
triage results into prompt messages, calling the reasoning endpoint, and
parsing the structured response into a canonical DiagnosisResult.

ARCHITECTURE.md §16.1 describes T2's role.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from oasisagent.llm.client import LLMError, LLMRole
from oasisagent.llm.prompts.diagnose_failure import build_diagnose_messages
from oasisagent.models import DiagnosisResult, RecommendedAction, RiskTier

if TYPE_CHECKING:
    from oasisagent.llm.client import LLMClient
    from oasisagent.models import Event, TriageResult

logger = logging.getLogger(__name__)

# Valid risk_tier values for validation
_VALID_RISK_TIERS = {t.value for t in RiskTier}


def _validate_risk_tier(raw: str) -> RiskTier:
    """Validate and convert a raw risk_tier string to the enum.

    Raises ValueError if the string isn't a valid RiskTier.
    """
    normalized = raw.strip().lower()
    if normalized not in _VALID_RISK_TIERS:
        raise ValueError(
            f"Invalid risk_tier '{raw}'. "
            f"Expected one of: {', '.join(sorted(_VALID_RISK_TIERS))}"
        )
    return RiskTier(normalized)


def _parse_diagnosis(raw_json: str) -> DiagnosisResult:
    """Parse raw JSON from the reasoning model into a DiagnosisResult.

    Validates risk_tier on each action against the enum — the model's
    output is never trusted.

    Raises:
        ValueError: If the JSON is invalid or doesn't match the schema.
        json.JSONDecodeError: If the response isn't valid JSON.
    """
    data = json.loads(raw_json)

    if not isinstance(data, dict):
        msg = f"Expected JSON object, got {type(data).__name__}"
        raise ValueError(msg)

    # Validate and normalize recommended_actions
    raw_actions = data.get("recommended_actions", [])
    validated_actions: list[dict[str, Any]] = []

    for i, action_data in enumerate(raw_actions):
        if not isinstance(action_data, dict):
            logger.warning("T2 action #%d is not a dict, skipping", i)
            continue

        # Validate risk_tier against the enum
        raw_tier = action_data.get("risk_tier", "")
        try:
            validated_tier = _validate_risk_tier(raw_tier)
        except ValueError:
            logger.warning(
                "T2 action #%d has invalid risk_tier '%s', defaulting to escalate",
                i,
                raw_tier,
            )
            validated_tier = RiskTier.ESCALATE

        action_data["risk_tier"] = validated_tier.value
        validated_actions.append(action_data)

    data["recommended_actions"] = validated_actions

    # Clamp confidence
    confidence = data.get("confidence", 0.0)
    if isinstance(confidence, (int, float)):
        data["confidence"] = max(0.0, min(1.0, float(confidence)))

    return DiagnosisResult.model_validate(data)


class ReasoningService:
    """Diagnoses events using the T2 cloud reasoning model.

    Handles LLM call failures and JSON parse errors gracefully,
    returning a safe fallback result rather than crashing.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def diagnose(
        self,
        event: Event,
        triage_result: TriageResult,
        entity_context: dict[str, Any] | None = None,
        known_fixes: list[dict[str, Any]] | None = None,
    ) -> DiagnosisResult:
        """Diagnose an event and return a DiagnosisResult.

        On failure (LLM unavailable, bad JSON, invalid schema),
        returns a safe result recommending human escalation.
        """
        messages = build_diagnose_messages(
            event, triage_result, entity_context, known_fixes
        )

        try:
            response = await self._llm.complete(
                role=LLMRole.REASONING,
                messages=messages,
            )
        except LLMError:
            logger.error(
                "T2 diagnose failed for event %s: LLM unavailable",
                event.id,
            )
            return self._safe_fallback(event, reason="llm_unavailable")

        try:
            result = _parse_diagnosis(response.content)
        except (json.JSONDecodeError, ValueError, Exception) as exc:
            logger.warning(
                "T2 diagnose parse failure for event %s: %s. Raw: %.300s",
                event.id,
                exc,
                response.content,
            )
            return self._safe_fallback(event, reason="parse_failure")

        logger.info(
            "T2 diagnosis: event %s → root_cause='%.80s', confidence=%.2f, "
            "%d actions",
            event.id,
            result.root_cause,
            result.confidence,
            len(result.recommended_actions),
        )

        return result

    @staticmethod
    def _safe_fallback(event: Event, *, reason: str) -> DiagnosisResult:
        """Return a safe result recommending human escalation."""
        return DiagnosisResult(
            root_cause=f"T2 reasoning failed ({reason}) — escalating to human",
            confidence=0.0,
            recommended_actions=[
                RecommendedAction(
                    description="Escalate to human operator for manual diagnosis",
                    handler="homeassistant",
                    operation="notify",
                    risk_tier=RiskTier.ESCALATE,
                    reasoning=f"Automatic escalation due to {reason}",
                ),
            ],
            risk_assessment="Unable to assess — reasoning unavailable",
        )
