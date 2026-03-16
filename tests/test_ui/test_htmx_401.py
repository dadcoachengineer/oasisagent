"""Tests for HTMX 401 redirect handler (#144)."""

from __future__ import annotations

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "oasisagent" / "ui" / "templates" / "base.html"


class TestHtmx401Handler:
    def test_template_contains_response_error_handler(self) -> None:
        content = TEMPLATE.read_text()
        assert "htmx:responseError" in content

    def test_template_checks_401_status(self) -> None:
        content = TEMPLATE.read_text()
        assert "status === 401" in content

    def test_template_redirects_to_login(self) -> None:
        content = TEMPLATE.read_text()
        assert "'/ui/login'" in content

    def test_template_contains_sse_error_handler(self) -> None:
        content = TEMPLATE.read_text()
        assert "htmx:sseError" in content
