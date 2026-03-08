"""Tests for LLM prompt builders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from oasisagent.llm.prompts.summarize_context import (
    SYSTEM_PROMPT,
    build_summarize_messages,
)
from oasisagent.models import Disposition, Event, Severity, TriageResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "ha_websocket",
        "system": "homeassistant",
        "event_type": "integration_failure",
        "entity_id": "sensor.temperature",
        "severity": Severity.ERROR,
        "timestamp": datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_triage(**overrides: Any) -> TriageResult:
    defaults: dict[str, Any] = {
        "disposition": Disposition.ESCALATE_T2,
        "confidence": 0.8,
        "classification": "integration_failure",
        "summary": "ZWave integration crashed repeatedly",
        "reasoning": "Multiple restart attempts failed",
    }
    defaults.update(overrides)
    return TriageResult(**defaults)


# ---------------------------------------------------------------------------
# summarize_context
# ---------------------------------------------------------------------------


class TestBuildSummarizeMessages:
    def test_returns_system_and_user_messages(self) -> None:
        messages = build_summarize_messages(_make_event(), _make_triage())

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_prompt_content(self) -> None:
        messages = build_summarize_messages(_make_event(), _make_triage())

        assert messages[0]["content"] == SYSTEM_PROMPT
        assert "event_summary" in messages[0]["content"]
        assert "failure_indicators" in messages[0]["content"]

    def test_user_message_contains_event_data(self) -> None:
        event = _make_event(entity_id="sensor.outdoor_temp")
        messages = build_summarize_messages(event, _make_triage())

        user_content = messages[1]["content"]
        assert "sensor.outdoor_temp" in user_content
        assert "integration_failure" in user_content

    def test_user_message_contains_triage_data(self) -> None:
        triage = _make_triage(
            classification="network_issue",
            summary="Switch port flapping",
        )
        messages = build_summarize_messages(_make_event(), triage)

        user_content = messages[1]["content"]
        assert "network_issue" in user_content
        assert "Switch port flapping" in user_content

    def test_user_message_is_parseable_json_segments(self) -> None:
        """The user message embeds two JSON blocks (event + triage)."""
        messages = build_summarize_messages(_make_event(), _make_triage())
        user_content = messages[1]["content"]

        # Should contain valid JSON for both event and triage
        assert "Event:" in user_content
        assert "T1 Classification:" in user_content

    def test_severity_serialized_as_string(self) -> None:
        messages = build_summarize_messages(
            _make_event(severity=Severity.CRITICAL),
            _make_triage(),
        )
        user_content = messages[1]["content"]
        assert "critical" in user_content
