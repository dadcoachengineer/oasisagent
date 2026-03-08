"""T1 triage service — classifies events using the local SLM.

Connects the LLMClient to the decision engine by converting events into
prompt messages, calling the triage endpoint, and parsing the structured
response into a canonical TriageResult.

ARCHITECTURE.md §5 describes T1's role: classify and package, never
decide to take action.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from oasisagent.llm.client import LLMError, LLMRole
from oasisagent.llm.prompts.classify_event import T1ClassifyOutput, build_classify_messages
from oasisagent.models import Disposition, TriageResult

if TYPE_CHECKING:
    from oasisagent.llm.client import LLMClient
    from oasisagent.models import Event

logger = logging.getLogger(__name__)

# Map raw SLM output strings to canonical Disposition values.
# Handles common variations local models may produce.
_DISPOSITION_MAP: dict[str, Disposition] = {
    "drop": Disposition.DROP,
    "known_pattern": Disposition.KNOWN_PATTERN,
    "escalate_t2": Disposition.ESCALATE_T2,
    "escalate_human": Disposition.ESCALATE_HUMAN,
    # Common SLM variations
    "ignore": Disposition.DROP,
    "noise": Disposition.DROP,
    "escalate": Disposition.ESCALATE_HUMAN,
    "notify_human": Disposition.ESCALATE_HUMAN,
    "notify": Disposition.ESCALATE_HUMAN,
}


def _parse_disposition(raw: str) -> Disposition:
    """Convert a raw SLM disposition string to the canonical enum.

    Raises:
        ValueError: If the string doesn't map to any known disposition.
    """
    normalized = raw.strip().lower()
    disposition = _DISPOSITION_MAP.get(normalized)
    if disposition is None:
        raise ValueError(
            f"Unknown disposition '{raw}'. "
            f"Expected one of: {', '.join(sorted(_DISPOSITION_MAP))}"
        )
    return disposition


class TriageService:
    """Classifies events using the T1 local SLM.

    Handles LLM call failures and JSON parse errors gracefully,
    returning a conservative ESCALATE_HUMAN disposition rather than
    dropping events silently.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def classify(self, event: Event) -> TriageResult:
        """Classify an event and return a TriageResult.

        On failure (LLM unavailable, bad JSON, invalid disposition),
        returns a safe ESCALATE_HUMAN result so the event isn't lost.
        """
        messages = build_classify_messages(event)

        try:
            response = await self._llm.complete(
                role=LLMRole.TRIAGE,
                messages=messages,
            )
        except LLMError:
            logger.error(
                "T1 classify failed for event %s: LLM unavailable",
                event.id,
            )
            return self._safe_fallback(event, reason="llm_unavailable")

        try:
            raw_output = T1ClassifyOutput.model_validate_json(response.content)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "T1 classify parse failure for event %s: %s. Raw: %.200s",
                event.id,
                exc,
                response.content,
            )
            return self._safe_fallback(event, reason="parse_failure")

        try:
            disposition = _parse_disposition(raw_output.disposition)
        except ValueError as exc:
            logger.warning(
                "T1 classify invalid disposition for event %s: %s",
                event.id,
                exc,
            )
            return self._safe_fallback(event, reason="parse_failure")

        confidence = max(0.0, min(1.0, raw_output.confidence))

        return TriageResult(
            disposition=disposition,
            confidence=confidence,
            classification=raw_output.classification,
            summary=raw_output.summary,
            suggested_fix=raw_output.suggested_fix,
            reasoning=raw_output.reasoning,
        )

    @staticmethod
    def _safe_fallback(event: Event, *, reason: str) -> TriageResult:
        """Return a conservative ESCALATE_HUMAN result on failure."""
        return TriageResult(
            disposition=Disposition.ESCALATE_HUMAN,
            confidence=0.0,
            classification="triage_failure",
            summary=f"T1 triage failed ({reason}) — escalating to human",
            reasoning=f"Automatic escalation due to {reason}",
        )
