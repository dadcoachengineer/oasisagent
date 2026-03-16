"""Tests for WebNotificationChannel — SSE fan-out and interactive approvals."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from oasisagent.approval.pending import PendingAction, PendingStatus
from oasisagent.db.notification_store import NotificationStore
from oasisagent.db.schema import run_migrations
from oasisagent.models import Notification, RecommendedAction, RiskTier, Severity
from oasisagent.notifications.web_channel import WebNotificationChannel


@pytest.fixture
async def store(tmp_path: Path) -> NotificationStore:
    db = await run_migrations(tmp_path / "test.db")
    yield NotificationStore(db)
    await db.close()


@pytest.fixture
def channel(store: NotificationStore) -> WebNotificationChannel:
    return WebNotificationChannel(store)


def _make_notification(**kwargs: object) -> Notification:
    defaults = {
        "severity": Severity.WARNING,
        "title": "Test notification",
        "message": "Something happened",
        "event_id": "evt-123",
    }
    defaults.update(kwargs)
    return Notification(**defaults)


def _make_pending_action() -> PendingAction:
    from datetime import UTC, datetime, timedelta

    return PendingAction(
        event_id="evt-456",
        action=RecommendedAction(
            description="Restart integration",
            handler="homeassistant",
            operation="restart_integration",
            risk_tier=RiskTier.RECOMMEND,
        ),
        diagnosis="Integration not responding",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        entity_id="sensor.temperature",
        severity="warning",
        source="mqtt",
        system="homeassistant",
    )


class TestSend:
    async def test_send_writes_to_store(
        self, channel: WebNotificationChannel, store: NotificationStore,
    ) -> None:
        n = _make_notification()
        result = await channel.send(n)
        assert result is True

        row = await store.get(n.id)
        assert row is not None
        assert row["title"] == "Test notification"

    async def test_send_pushes_to_subscriber(
        self, channel: WebNotificationChannel,
    ) -> None:
        queue = channel.subscribe()
        n = _make_notification()
        await channel.send(n)

        assert not queue.empty()
        msg = queue.get_nowait()
        assert msg["event"] == "notification"
        data = json.loads(msg["data"])
        assert data["title"] == "Test notification"

        channel.unsubscribe(queue)


class TestApprovalRequest:
    async def test_send_approval_creates_interactive_notification(
        self, channel: WebNotificationChannel, store: NotificationStore,
    ) -> None:
        pending = _make_pending_action()
        await channel.send_approval_request(pending)

        # Find the notification in the store
        rows = await store.list_recent()
        assert len(rows) == 1
        row = rows[0]
        assert row["action_id"] == pending.id
        assert row["action_status"] == "pending"
        assert "[APPROVAL]" in row["title"]

    async def test_send_approval_pushes_sse(
        self, channel: WebNotificationChannel,
    ) -> None:
        queue = channel.subscribe()
        pending = _make_pending_action()
        await channel.send_approval_request(pending)

        msg = queue.get_nowait()
        data = json.loads(msg["data"])
        assert data["action_id"] == pending.id
        assert data["action_status"] == "pending"

        channel.unsubscribe(queue)


class TestUpdateApprovalMessage:
    async def test_update_status_in_store(
        self, channel: WebNotificationChannel, store: NotificationStore,
    ) -> None:
        pending = _make_pending_action()
        await channel.send_approval_request(pending)

        await channel.update_approval_message(
            pending.id, PendingStatus.APPROVED,
        )

        rows = await store.list_recent()
        assert rows[0]["action_status"] == "approved"

    async def test_update_pushes_sse(
        self, channel: WebNotificationChannel,
    ) -> None:
        pending = _make_pending_action()
        await channel.send_approval_request(pending)

        queue = channel.subscribe()
        await channel.update_approval_message(
            pending.id, PendingStatus.REJECTED,
        )

        # Skip any leftover messages from before subscribe
        msg = queue.get_nowait()
        assert msg["event"] == "action_update"
        data = json.loads(msg["data"])
        assert data["action_status"] == "rejected"

        channel.unsubscribe(queue)


class TestSSESubscription:
    def test_subscribe_unsubscribe(
        self, channel: WebNotificationChannel,
    ) -> None:
        assert channel.subscriber_count == 0
        q = channel.subscribe()
        assert channel.subscriber_count == 1
        channel.unsubscribe(q)
        assert channel.subscriber_count == 0

    async def test_slow_consumer_message_dropped(
        self, channel: WebNotificationChannel,
    ) -> None:
        """Full queue should not block fan-out."""
        queue = channel.subscribe()
        # Fill the queue
        for i in range(50):
            queue.put_nowait({"data": f"fill-{i}"})

        # This should not raise
        n = _make_notification()
        await channel.send(n)

        channel.unsubscribe(queue)


class TestName:
    def test_name(self, channel: WebNotificationChannel) -> None:
        assert channel.name() == "web"


class TestStartStopListener:
    async def test_start_stop_listener(
        self, channel: WebNotificationChannel,
    ) -> None:
        callback = AsyncMock()
        await channel.start_listener(callback)
        assert channel._approval_callback is callback

        await channel.stop_listener()
        assert channel._approval_callback is None
