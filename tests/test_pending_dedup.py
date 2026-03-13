"""Tests for pending action deduplication (#143)."""

from __future__ import annotations

import pytest

from oasisagent.approval.pending import PendingQueue, PendingStatus
from oasisagent.models import RecommendedAction, RiskTier


def _make_action(**overrides: object) -> RecommendedAction:
    defaults: dict = {
        "description": "Restart ZWave integration",
        "handler": "homeassistant",
        "operation": "restart_integration",
        "params": {"integration": "zwave_js"},
        "risk_tier": RiskTier.RECOMMEND,
        "reasoning": "Integration needs restart",
        "target_entity_id": "integration.zwave_js",
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


class TestPendingDedup:
    @pytest.mark.asyncio
    async def test_duplicate_returns_none(self) -> None:
        """Same entity + handler + operation twice → second returns None."""
        queue = PendingQueue()
        action = _make_action()

        first = await queue.add("evt-1", action, "crash", 30)
        second = await queue.add("evt-2", action, "crash again", 30)

        assert first is not None
        assert second is None
        assert queue.pending_count == 1

    @pytest.mark.asyncio
    async def test_approve_allows_readd(self) -> None:
        """Approve → same entity can be re-added."""
        queue = PendingQueue()
        action = _make_action()

        first = await queue.add("evt-1", action, "crash", 30)
        assert first is not None
        await queue.approve(first.id)

        second = await queue.add("evt-2", action, "crash again", 30)
        assert second is not None

    @pytest.mark.asyncio
    async def test_reject_allows_readd(self) -> None:
        """Reject → same entity can be re-added."""
        queue = PendingQueue()
        action = _make_action()

        first = await queue.add("evt-1", action, "crash", 30)
        assert first is not None
        await queue.reject(first.id)

        second = await queue.add("evt-2", action, "crash again", 30)
        assert second is not None

    @pytest.mark.asyncio
    async def test_different_entities_both_added(self) -> None:
        """Different target entities → both added."""
        queue = PendingQueue()
        action_a = _make_action(target_entity_id="integration.zwave_js")
        action_b = _make_action(target_entity_id="integration.mqtt")

        first = await queue.add("evt-1", action_a, "crash", 30)
        second = await queue.add("evt-2", action_b, "crash", 30)

        assert first is not None
        assert second is not None
        assert queue.pending_count == 2

    @pytest.mark.asyncio
    async def test_same_entity_different_handler(self) -> None:
        """Same entity, different handler → both added."""
        queue = PendingQueue()
        action_a = _make_action(handler="portainer")
        action_b = _make_action(handler="homeassistant")

        first = await queue.add("evt-1", action_a, "crash", 30)
        second = await queue.add("evt-2", action_b, "crash", 30)

        assert first is not None
        assert second is not None
        assert queue.pending_count == 2

    @pytest.mark.asyncio
    async def test_same_entity_different_operation(self) -> None:
        """Same entity, different operation → both added."""
        queue = PendingQueue()
        action_a = _make_action(operation="restart_integration")
        action_b = _make_action(operation="reload_automations")

        first = await queue.add("evt-1", action_a, "crash", 30)
        second = await queue.add("evt-2", action_b, "crash", 30)

        assert first is not None
        assert second is not None
        assert queue.pending_count == 2

    @pytest.mark.asyncio
    async def test_expire_allows_readd(self) -> None:
        """Expired action frees the key for re-addition."""
        queue = PendingQueue()
        action = _make_action()

        first = await queue.add("evt-1", action, "crash", 0)
        assert first is not None

        expired = await queue.expire_stale()
        assert len(expired) == 1
        assert expired[0].status == PendingStatus.EXPIRED

        second = await queue.add("evt-2", action, "crash again", 30)
        assert second is not None
