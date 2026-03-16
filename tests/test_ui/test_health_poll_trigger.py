"""Tests for health poll trigger configuration (#145)."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "oasisagent" / "ui" / "templates" / "connectors" / "list.html"
)

_TEMPLATE_CONTENT: str | None = None


def _read_template() -> str:
    global _TEMPLATE_CONTENT
    if _TEMPLATE_CONTENT is None:
        _TEMPLATE_CONTENT = TEMPLATE.read_text()
    return _TEMPLATE_CONTENT


# Regex anchored to the health-poll div (hx-get contains /health)
# so we don't accidentally match other hx-trigger attributes.
_HEALTH_DIV_RE = re.compile(
    r'hx-get="[^"]*?/health"[^>]*?hx-trigger="([^"]+)"',
    re.DOTALL,
)

_HEALTH_SYNC_RE = re.compile(
    r'hx-get="[^"]*?/health"[^>]*?hx-sync="([^"]+)"',
    re.DOTALL,
)

_INTERVAL_RE = re.compile(r"every\s+\{\{[^}]*default\((\d+)\)")


def _get_health_trigger() -> str:
    content = _read_template()
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

    def test_default_interval_at_least_10s(self) -> None:
        trigger = _get_health_trigger()
        match = _INTERVAL_RE.search(trigger)
        assert match is not None, "Could not extract default interval"
        assert int(match.group(1)) >= 10

    def test_hx_sync_prevents_request_pileup(self) -> None:
        content = _read_template()
        match = _HEALTH_SYNC_RE.search(content)
        assert match is not None, "hx-sync not set on health-poll element"
        assert "replace" in match.group(1)
