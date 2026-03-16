"""Tests for plan notification content and config."""

from __future__ import annotations

from oasisagent.config import AgentConfig


class TestPlanNotificationConfig:
    def test_plan_step_notifications_default_off(self) -> None:
        """plan_step_notifications defaults to False."""
        cfg = AgentConfig()
        assert cfg.plan_step_notifications is False

    def test_plan_step_notifications_opt_in(self) -> None:
        """plan_step_notifications can be enabled."""
        cfg = AgentConfig(plan_step_notifications=True)
        assert cfg.plan_step_notifications is True
