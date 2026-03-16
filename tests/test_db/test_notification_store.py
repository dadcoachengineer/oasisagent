"""Tests for NotificationStore CRUD, pruning, and archival."""

from __future__ import annotations

from pathlib import Path

import pytest

from oasisagent.db.notification_store import MAX_ROWS, PRUNE_BUFFER, NotificationStore
from oasisagent.db.schema import run_migrations
from oasisagent.models import Notification, Severity


@pytest.fixture
async def store(tmp_path: Path) -> NotificationStore:
    """NotificationStore backed by a fresh test database."""
    db = await run_migrations(tmp_path / "test.db")
    yield NotificationStore(db)
    await db.close()


def _make_notification(
    *,
    severity: Severity = Severity.INFO,
    title: str = "Test",
    message: str = "test message",
    event_id: str | None = None,
) -> Notification:
    return Notification(
        severity=severity,
        title=title,
        message=message,
        event_id=event_id,
        metadata={"entity_id": "sensor.test"},
    )


class TestAdd:
    async def test_add_returns_id(self, store: NotificationStore) -> None:
        n = _make_notification()
        nid = await store.add(n)
        assert nid == n.id

    async def test_add_with_action(self, store: NotificationStore) -> None:
        n = _make_notification()
        await store.add(n, action_id="act-123", action_status="pending")
        row = await store.get(n.id)
        assert row is not None
        assert row["action_id"] == "act-123"
        assert row["action_status"] == "pending"


class TestListRecent:
    async def test_list_empty(self, store: NotificationStore) -> None:
        result = await store.list_recent()
        assert result == []

    async def test_list_returns_newest_first(self, store: NotificationStore) -> None:
        for i in range(3):
            n = _make_notification(title=f"N{i}")
            await store.add(n)
        result = await store.list_recent()
        assert len(result) == 3
        # newest first
        assert result[0]["title"] == "N2"
        assert result[2]["title"] == "N0"

    async def test_list_with_severity_filter(self, store: NotificationStore) -> None:
        await store.add(_make_notification(severity=Severity.INFO))
        await store.add(_make_notification(severity=Severity.WARNING))
        await store.add(_make_notification(severity=Severity.ERROR))

        result = await store.list_recent(severity="warning")
        assert len(result) == 1
        assert result[0]["severity"] == "warning"

    async def test_list_with_limit_offset(self, store: NotificationStore) -> None:
        for i in range(5):
            await store.add(_make_notification(title=f"N{i}"))
        result = await store.list_recent(limit=2, offset=1)
        assert len(result) == 2
        assert result[0]["title"] == "N3"
        assert result[1]["title"] == "N2"


class TestGet:
    async def test_get_existing(self, store: NotificationStore) -> None:
        n = _make_notification()
        await store.add(n)
        row = await store.get(n.id)
        assert row is not None
        assert row["title"] == "Test"
        assert row["metadata"] == {"entity_id": "sensor.test"}

    async def test_get_nonexistent(self, store: NotificationStore) -> None:
        result = await store.get("nonexistent")
        assert result is None


class TestUpdateActionStatus:
    async def test_update_status(self, store: NotificationStore) -> None:
        n = _make_notification()
        await store.add(n, action_id="act-1", action_status="pending")

        await store.update_action_status("act-1", "approved")

        row = await store.get(n.id)
        assert row is not None
        assert row["action_status"] == "approved"


class TestCount:
    async def test_count_empty(self, store: NotificationStore) -> None:
        assert await store.count() == 0

    async def test_count_unarchived(self, store: NotificationStore) -> None:
        await store.add(_make_notification())
        assert await store.count_unarchived() == 1
        assert await store.count() == 1


class TestPrune:
    async def test_prune_below_threshold_noop(self, store: NotificationStore) -> None:
        for _ in range(10):
            await store.add(_make_notification())
        result = await store.prune()
        assert result == []
        assert await store.count() == 10

    async def test_prune_above_threshold(self, store: NotificationStore) -> None:
        # Insert more than MAX_ROWS + PRUNE_BUFFER
        count = MAX_ROWS + PRUNE_BUFFER + 50
        for i in range(count):
            await store.add(_make_notification(title=f"N{i}"))

        archived = await store.prune()
        # Should prune down to MAX_ROWS
        assert len(archived) == count - MAX_ROWS
        assert await store.count_unarchived() == MAX_ROWS

        # Archived rows should be returned for InfluxDB
        assert all(isinstance(r, dict) for r in archived)
        assert all("title" in r for r in archived)
