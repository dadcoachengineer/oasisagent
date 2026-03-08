"""T1 prompt: Summarize context for T2 escalation or human notification.

When T1 classifies an event as escalate_t2, this prompt asks the SLM to
prepare a structured context package that T2 can use for deep diagnosis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from oasisagent.models import Event, TriageResult

SYSTEM_PROMPT = """\
You are preparing a context summary for a senior infrastructure analyst. \
An event has been classified as requiring deeper analysis.

Summarize the event and its context into a structured JSON object with \
these fields:

- "event_summary": A clear, concise description of what happened (2-3 sentences)
- "system_context": What system is affected and its role in the infrastructure
- "failure_indicators": List of specific symptoms or error indicators observed
- "potential_causes": List of possible root causes to investigate
- "urgency": One of "low", "medium", "high", "critical"
- "related_entities": List of entity IDs that may be related to this issue

Respond with ONLY the JSON object, no markdown fences or extra text.\
"""


def build_summarize_messages(
    event: Event, triage: TriageResult
) -> list[dict[str, Any]]:
    """Build chat messages for context summarization.

    Combines the original event with T1's classification to give the
    SLM full context for preparing the T2 escalation package.
    """
    import json

    event_data = event.model_dump(mode="json")
    triage_data = triage.model_dump(mode="json")

    user_content = (
        "Prepare a context summary for deeper analysis.\n\n"
        f"Event:\n{json.dumps(event_data, indent=2, default=str)}\n\n"
        f"T1 Classification:\n{json.dumps(triage_data, indent=2, default=str)}"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
