"""Tests for health poll trigger configuration (#145)."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "oasisagent" / "ui" / "templates" / "connectors" / "list.html"
)


class TestHealthPollTrigger:
    def test_hx_trigger_does_not_contain_load(self) -> None:
        content = TEMPLATE.read_text()
        match = re.search(r'hx-trigger="([^"]+)"', content)
        assert match is not None, "No hx-trigger attribute found"
        trigger_value = match.group(1)
        assert "load" not in trigger_value

    def test_hx_trigger_contains_every(self) -> None:
        content = TEMPLATE.read_text()
        match = re.search(r'hx-trigger="([^"]+)"', content)
        assert match is not None
        trigger_value = match.group(1)
        assert "every" in trigger_value

    def test_hx_trigger_contains_visibility_guard(self) -> None:
        content = TEMPLATE.read_text()
        match = re.search(r'hx-trigger="([^"]+)"', content)
        assert match is not None
        trigger_value = match.group(1)
        assert "document.visibilityState" in trigger_value
