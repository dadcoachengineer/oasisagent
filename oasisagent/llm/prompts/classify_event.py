"""T1 prompt: Classify an event and determine disposition.

The system prompt instructs the local SLM to classify infrastructure events
and return a structured JSON response. The output schema (T1ClassifyOutput)
is deliberately simpler than the canonical TriageResult — flat fields and
string-typed enums give local models the best chance of producing valid JSON.

TriageService maps T1ClassifyOutput → TriageResult.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from oasisagent.models import Event

SYSTEM_PROMPT = """\
You are an infrastructure triage agent for a home lab. Your job is to classify \
events from Home Assistant, Docker, and Proxmox and determine what action to take.

For each event, respond with a JSON object containing these fields:

- "disposition": One of these exact values:
  - "drop" — The event is noise and should be ignored (e.g., transient sensor \
blips, expected state changes)
  - "known_pattern" — The issue has a recognizable pattern with a clear fix
  - "escalate_t2" — The issue is complex and requires deeper analysis by a \
more capable model
  - "escalate_human" — The issue requires immediate human attention

- "confidence": A float between 0.0 and 1.0 indicating how confident you are \
in your classification

- "classification": A short category label for the event (e.g., \
"sensor_flap", "integration_failure", "automation_error", "network_issue")

- "summary": A one-sentence human-readable summary of what happened and why \
you classified it this way

- "suggested_fix": If disposition is "known_pattern", describe the fix. \
Otherwise set to null.

- "reasoning": Brief explanation of your classification logic (1-2 sentences)

Respond with ONLY the JSON object, no markdown fences or extra text.\
"""


class T1ClassifyOutput(BaseModel):
    """Schema the SLM actually produces.

    Deliberately flat and string-typed for maximum compatibility with
    local models. TriageService maps this to the canonical TriageResult.
    """

    model_config = ConfigDict(extra="forbid")

    disposition: str
    confidence: float
    classification: str
    summary: str
    suggested_fix: str | None = None
    reasoning: str = ""


def build_classify_messages(event: Event) -> list[dict[str, Any]]:
    """Build chat messages for T1 event classification.

    Returns messages in OpenAI chat format with the system prompt
    and a user message containing the serialized event.
    """
    event_data = event.model_dump(mode="json")

    # Remove fields the SLM doesn't need
    event_data.pop("context", None)
    event_data.pop("metadata", None)
    event_data.pop("ingested_at", None)

    import json

    user_content = (
        "Classify this infrastructure event:\n\n"
        f"{json.dumps(event_data, indent=2, default=str)}"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
