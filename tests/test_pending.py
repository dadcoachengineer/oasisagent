"""Tests for the pending action queue."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from oasisagent.approval.pending import PendingAction, PendingQueue, PendingStatus
from oasisagent.db.schema import run_migrations
from oasisagent.models import RecommendedAction, RiskTier

if TYPE_CHECKING:
    import aiosqlite

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(**overrides: dict) -> RecommendedAction:
    defaults = {
        "description": "Restart ZWave integration",
        "handler": "homeassistant",
        "operation": "restart_integration",
        "params": {"integration": "zwave_js"},
        "risk_tier": RiskTier.RECOMMEND,
        "reasoning": "Integration needs restart",
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


# ---------------------------------------------------------------------------
# PendingQueue.add (in-memory, db=None)
# ---------------------------------------------------------------------------


class TestPendingQueueAdd:
    @pytest.mark.asyncio
    async def test_add_creates_pending_action(self) -> None:
        queue = PendingQueue()
        action = _make_action()

        pending = await queue.add(
            event_id="evt-1",
            action=action,
            diagnosis="ZWave crash",
            timeout_minutes=30,
        )

        assert pending.status == PendingStatus.PENDING
        assert pending.event_id == "evt-1"
        assert pending.action == action
        assert pending.diagnosis == "ZWave crash"
        assert pending.id  # UUID assigned

    @pytest.mark.asyncio
    async def test_add_sets_expiry(self) -> None:
        queue = PendingQueue()
        before = datetime.now(UTC)

        pending = await queue.add(
            event_id="evt-1",
            action=_make_action(),
            diagnosis="test",
            timeout_minutes=30,
        )

        after = datetime.now(UTC)
        expected_min = before + timedelta(minutes=30)
        expected_max = after + timedelta(minutes=30)
        assert expected_min <= pending.expires_at <= expected_max

    @pytest.mark.asyncio
    async def test_add_multiple_unique_ids(self) -> None:
        queue = PendingQueue()
        p1 = await queue.add("e1", _make_action(), "d1", 30)
        p2 = await queue.add("e2", _make_action(), "d2", 30)
        assert p1.id != p2.id

    @pytest.mark.asyncio
    async def test_add_with_event_context(self) -> None:
        queue = PendingQueue()
        pending = await queue.add(
            event_id="evt-1",
            action=_make_action(),
            diagnosis="test",
            timeout_minutes=30,
            entity_id="sensor.temperature",
            severity="warning",
            source="mqtt",
            system="homeassistant",
        )

        assert pending.entity_id == "sensor.temperature"
        assert pending.severity == "warning"
        assert pending.source == "mqtt"
        assert pending.system == "homeassistant"

    @pytest.mark.asyncio
    async def test_add_without_event_context_defaults_to_empty(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)

        assert pending.entity_id == ""
        assert pending.severity == ""
        assert pending.source == ""
        assert pending.system == ""


# ---------------------------------------------------------------------------
# PendingQueue.approve
# ---------------------------------------------------------------------------


class TestPendingQueueApprove:
    @pytest.mark.asyncio
    async def test_approve_pending_action(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)

        result = await queue.approve(pending.id)

        assert result is not None
        assert result.status == PendingStatus.APPROVED
        assert result.id == pending.id

    @pytest.mark.asyncio
    async def test_approve_nonexistent_returns_none(self) -> None:
        queue = PendingQueue()
        assert await queue.approve("nonexistent") is None

    @pytest.mark.asyncio
    async def test_approve_already_approved_returns_none(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        await queue.approve(pending.id)

        assert await queue.approve(pending.id) is None

    @pytest.mark.asyncio
    async def test_approve_already_rejected_returns_none(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        await queue.reject(pending.id)

        assert await queue.approve(pending.id) is None

    @pytest.mark.asyncio
    async def test_approve_already_expired_returns_none(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        # Force expire
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await queue.expire_stale()

        assert await queue.approve(pending.id) is None


# ---------------------------------------------------------------------------
# PendingQueue.reject
# ---------------------------------------------------------------------------


class TestPendingQueueReject:
    @pytest.mark.asyncio
    async def test_reject_pending_action(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)

        result = await queue.reject(pending.id)

        assert result is not None
        assert result.status == PendingStatus.REJECTED

    @pytest.mark.asyncio
    async def test_reject_nonexistent_returns_none(self) -> None:
        queue = PendingQueue()
        assert await queue.reject("nonexistent") is None

    @pytest.mark.asyncio
    async def test_reject_already_approved_returns_none(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        await queue.approve(pending.id)

        assert await queue.reject(pending.id) is None


# ---------------------------------------------------------------------------
# PendingQueue.expire_stale
# ---------------------------------------------------------------------------


class TestPendingQueueExpireStale:
    @pytest.mark.asyncio
    async def test_expire_stale_finds_expired(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        # Force expire
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        expired = await queue.expire_stale()

        assert len(expired) == 1
        assert expired[0].id == pending.id
        assert expired[0].status == PendingStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_expire_stale_ignores_not_yet_expired(self) -> None:
        queue = PendingQueue()
        await queue.add("evt-1", _make_action(), "test", 30)

        expired = await queue.expire_stale()

        assert expired == []

    @pytest.mark.asyncio
    async def test_expire_stale_ignores_already_resolved(self) -> None:
        queue = PendingQueue()
        p1 = await queue.add("evt-1", _make_action(), "test", 30)
        p2 = await queue.add("evt-2", _make_action(), "test", 30)
        await queue.approve(p1.id)
        await queue.reject(p2.id)

        # Force both to look expired
        p1.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        p2.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        expired = await queue.expire_stale()

        assert expired == []

    @pytest.mark.asyncio
    async def test_expire_stale_multiple(self) -> None:
        queue = PendingQueue()
        p1 = await queue.add("evt-1", _make_action(), "test", 30)
        p2 = await queue.add("evt-2", _make_action(), "test", 30)

        p1.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        p2.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        expired = await queue.expire_stale()

        assert len(expired) == 2


# ---------------------------------------------------------------------------
# PendingQueue.get and list_pending
# ---------------------------------------------------------------------------


class TestPendingQueueLookup:
    @pytest.mark.asyncio
    async def test_get_existing(self) -> None:
        queue = PendingQueue()
        pending = await queue.add("evt-1", _make_action(), "test", 30)

        result = queue.get(pending.id)

        assert result is not None
        assert result.id == pending.id

    def test_get_nonexistent(self) -> None:
        queue = PendingQueue()
        assert queue.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_pending_returns_only_pending(self) -> None:
        queue = PendingQueue()
        p1 = await queue.add("evt-1", _make_action(), "test", 30)
        p2 = await queue.add("evt-2", _make_action(), "test", 30)
        p3 = await queue.add("evt-3", _make_action(), "test", 30)
        await queue.approve(p1.id)
        await queue.reject(p2.id)

        pending = queue.list_pending()

        assert len(pending) == 1
        assert pending[0].id == p3.id

    def test_list_pending_empty(self) -> None:
        queue = PendingQueue()
        assert queue.list_pending() == []


# ---------------------------------------------------------------------------
# PendingQueue.to_list_payload
# ---------------------------------------------------------------------------


class TestPendingQueuePayload:
    @pytest.mark.asyncio
    async def test_to_list_payload_only_pending(self) -> None:
        queue = PendingQueue()
        p1 = await queue.add("evt-1", _make_action(), "test", 30)
        p2 = await queue.add("evt-2", _make_action(), "test", 30)
        await queue.approve(p1.id)

        payload = queue.to_list_payload()

        assert len(payload) == 1
        assert payload[0]["id"] == p2.id
        assert payload[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_to_list_payload_contains_action_data(self) -> None:
        queue = PendingQueue()
        await queue.add("evt-1", _make_action(), "ZWave crash", 30)

        payload = queue.to_list_payload()

        assert payload[0]["diagnosis"] == "ZWave crash"
        assert payload[0]["action"]["handler"] == "homeassistant"
        assert payload[0]["action"]["operation"] == "restart_integration"

    @pytest.mark.asyncio
    async def test_to_list_payload_includes_event_context(self) -> None:
        queue = PendingQueue()
        await queue.add(
            "evt-1", _make_action(), "ZWave crash", 30,
            entity_id="sensor.temp",
            severity="critical",
            source="mqtt",
            system="homeassistant",
        )

        payload = queue.to_list_payload()

        assert payload[0]["entity_id"] == "sensor.temp"
        assert payload[0]["severity"] == "critical"
        assert payload[0]["source"] == "mqtt"
        assert payload[0]["system"] == "homeassistant"


# ---------------------------------------------------------------------------
# PendingAction model
# ---------------------------------------------------------------------------


class TestPendingActionModel:
    def test_pending_status_values(self) -> None:
        assert PendingStatus.PENDING == "pending"
        assert PendingStatus.APPROVED == "approved"
        assert PendingStatus.REJECTED == "rejected"
        assert PendingStatus.EXPIRED == "expired"

    def test_pending_action_serialization(self) -> None:
        action = _make_action()
        pending = PendingAction(
            event_id="evt-1",
            action=action,
            diagnosis="test",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

        data = pending.model_dump(mode="json")
        assert data["status"] == "pending"
        assert data["event_id"] == "evt-1"
        assert data["action"]["risk_tier"] == "recommend"

    def test_pending_action_serialization_with_context(self) -> None:
        pending = PendingAction(
            event_id="evt-1",
            action=_make_action(),
            diagnosis="test",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            entity_id="sensor.temperature",
            severity="warning",
            source="mqtt",
            system="homeassistant",
        )

        data = pending.model_dump(mode="json")
        assert data["entity_id"] == "sensor.temperature"
        assert data["severity"] == "warning"
        assert data["source"] == "mqtt"
        assert data["system"] == "homeassistant"

    def test_pending_action_deserialization_without_context(self) -> None:
        """Backward compat: deserialize without the new context fields."""
        data = {
            "id": "test-id",
            "event_id": "evt-1",
            "action": _make_action().model_dump(),
            "diagnosis": "test",
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            "status": "pending",
        }

        pending = PendingAction.model_validate(data)
        assert pending.entity_id == ""
        assert pending.severity == ""
        assert pending.source == ""
        assert pending.system == ""


# ---------------------------------------------------------------------------
# Persistence tests (with real SQLite)
# ---------------------------------------------------------------------------


class TestPendingQueuePersistence:
    """Tests using a real in-memory SQLite database."""

    @pytest.fixture
    async def db(self, tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection]:
        """Create a migrated SQLite database."""
        db = await run_migrations(tmp_path / "test.db")
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_add_persists_to_sqlite(self, db: aiosqlite.Connection) -> None:
        queue = await PendingQueue.from_db(db)
        pending = await queue.add("evt-1", _make_action(), "test", 30)

        cursor = await db.execute(
            "SELECT id, status FROM pending_actions WHERE id = ?",
            (pending.id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_approve_updates_sqlite(self, db: aiosqlite.Connection) -> None:
        queue = await PendingQueue.from_db(db)
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        await queue.approve(pending.id)

        cursor = await db.execute(
            "SELECT status FROM pending_actions WHERE id = ?",
            (pending.id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "approved"

    @pytest.mark.asyncio
    async def test_reject_updates_sqlite(self, db: aiosqlite.Connection) -> None:
        queue = await PendingQueue.from_db(db)
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        await queue.reject(pending.id)

        cursor = await db.execute(
            "SELECT status FROM pending_actions WHERE id = ?",
            (pending.id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_expire_updates_sqlite(self, db: aiosqlite.Connection) -> None:
        queue = await PendingQueue.from_db(db)
        pending = await queue.add("evt-1", _make_action(), "test", 30)
        # Force expire
        await db.execute(
            "UPDATE pending_actions SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=10)).isoformat(), pending.id),
        )
        await db.commit()
        # Also update in-memory so the cache matches
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=10)

        expired = await queue.expire_stale()

        assert len(expired) == 1
        cursor = await db.execute(
            "SELECT status FROM pending_actions WHERE id = ?",
            (pending.id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "expired"

    @pytest.mark.asyncio
    async def test_cas_approve_prevents_double_transition(self, db: aiosqlite.Connection) -> None:
        """CAS: if SQLite status was already changed, approve returns None."""
        queue = await PendingQueue.from_db(db)
        pending = await queue.add("evt-1", _make_action(), "test", 30)

        # Simulate external status change directly in SQLite
        await db.execute(
            "UPDATE pending_actions SET status = 'expired' WHERE id = ?",
            (pending.id,),
        )
        await db.commit()

        result = await queue.approve(pending.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_from_db_loads_pending_rows(self, db: aiosqlite.Connection) -> None:
        """from_db loads pending rows on construction."""
        queue1 = await PendingQueue.from_db(db)
        p = await queue1.add(
            "evt-1", _make_action(), "test", 30,
            entity_id="light.kitchen",
            severity="warning",
        )

        # Create a new queue from the same DB — simulates restart
        queue2 = await PendingQueue.from_db(db)

        assert queue2.pending_count == 1
        loaded = queue2.get(p.id)
        assert loaded is not None
        assert loaded.event_id == "evt-1"
        assert loaded.entity_id == "light.kitchen"
        assert loaded.severity == "warning"
        assert loaded.action.handler == "homeassistant"

    @pytest.mark.asyncio
    async def test_from_db_does_not_load_resolved(self, db: aiosqlite.Connection) -> None:
        """Approved/rejected/expired rows are not loaded on restart."""
        queue1 = await PendingQueue.from_db(db)
        p1 = await queue1.add("evt-1", _make_action(), "test", 30)
        p2 = await queue1.add("evt-2", _make_action(), "test", 30)
        p3 = await queue1.add("evt-3", _make_action(), "test", 30)
        await queue1.approve(p1.id)
        await queue1.reject(p2.id)
        # Force p3 to be expired — update both SQLite and in-memory
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await db.execute(
            "UPDATE pending_actions SET expires_at = ? WHERE id = ?",
            (past, p3.id),
        )
        await db.commit()
        p3.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await queue1.expire_stale()

        queue2 = await PendingQueue.from_db(db)
        assert queue2.pending_count == 0

    @pytest.mark.asyncio
    async def test_from_db_sweeps_stale_on_startup(self, db: aiosqlite.Connection) -> None:
        """Actions that expired while process was down get swept to expired (D5)."""
        queue1 = await PendingQueue.from_db(db)
        p = await queue1.add("evt-1", _make_action(), "test", 30)

        # Simulate time passing while process was down
        await db.execute(
            "UPDATE pending_actions SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), p.id),
        )
        await db.commit()

        # New queue on startup should sweep the stale row
        queue2 = await PendingQueue.from_db(db)
        assert queue2.pending_count == 0

        # Verify it was marked expired in SQLite
        cursor = await db.execute(
            "SELECT status FROM pending_actions WHERE id = ?", (p.id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "expired"

    @pytest.mark.asyncio
    async def test_context_fields_persisted(self, db: aiosqlite.Connection) -> None:
        queue1 = await PendingQueue.from_db(db)
        await queue1.add(
            "evt-1", _make_action(), "test", 30,
            entity_id="sensor.temp",
            severity="critical",
            source="mqtt",
            system="homeassistant",
        )

        queue2 = await PendingQueue.from_db(db)
        loaded = queue2.list_pending()[0]
        assert loaded.entity_id == "sensor.temp"
        assert loaded.severity == "critical"
        assert loaded.source == "mqtt"
        assert loaded.system == "homeassistant"
