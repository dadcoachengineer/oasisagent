"""Tests for health poll trigger configuration (#145)."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "oasisagent" / "ui" / "templates" / "connectors" / "list.html"
)

# Regex anchored to the health-poll div (hx-get contains /health)
# so we don't accidentally match other hx-trigger attributes.
_HEALTH_DIV_RE = re.compile(
    r'hx-get="[^"]*?/health"[^>]*?hx-trigger="([^"]+)"',
    re.DOTALL,
)


def _get_health_trigger() -> str:
    content = TEMPLATE.read_text()
    match = _HEALTH_DIV_RE.search(content)
    assert match is not None, (
        "No hx-trigger found on health-poll element"
    )
    return match.group(1)


class TestHealthPollTrigger:
    def test_hx_trigger_does_not_contain_load(self) -> None:
        assert "load" not in _get_health_trigger()

    def test_hx_trigger_contains_every(self) -> None:
        assert "every" in _get_health_trigger()

    def test_hx_trigger_contains_visibility_guard(self) -> None:
        assert "document.visibilityState" in _get_health_trigger()
