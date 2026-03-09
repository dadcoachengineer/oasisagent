"""Tests for the pending action queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oasisagent.approval.pending import PendingAction, PendingQueue, PendingStatus
from oasisagent.models import RecommendedAction, RiskTier

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
# PendingQueue.add
# ---------------------------------------------------------------------------


class TestPendingQueueAdd:
    def test_add_creates_pending_action(self) -> None:
        queue = PendingQueue()
        action = _make_action()

        pending = queue.add(
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

    def test_add_sets_expiry(self) -> None:
        queue = PendingQueue()
        before = datetime.now(UTC)

        pending = queue.add(
            event_id="evt-1",
            action=_make_action(),
            diagnosis="test",
            timeout_minutes=30,
        )

        after = datetime.now(UTC)
        expected_min = before + timedelta(minutes=30)
        expected_max = after + timedelta(minutes=30)
        assert expected_min <= pending.expires_at <= expected_max

    def test_add_multiple_unique_ids(self) -> None:
        queue = PendingQueue()
        p1 = queue.add("e1", _make_action(), "d1", 30)
        p2 = queue.add("e2", _make_action(), "d2", 30)
        assert p1.id != p2.id


# ---------------------------------------------------------------------------
# PendingQueue.approve
# ---------------------------------------------------------------------------


class TestPendingQueueApprove:
    def test_approve_pending_action(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)

        result = queue.approve(pending.id)

        assert result is not None
        assert result.status == PendingStatus.APPROVED
        assert result.id == pending.id

    def test_approve_nonexistent_returns_none(self) -> None:
        queue = PendingQueue()
        assert queue.approve("nonexistent") is None

    def test_approve_already_approved_returns_none(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)
        queue.approve(pending.id)

        assert queue.approve(pending.id) is None

    def test_approve_already_rejected_returns_none(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)
        queue.reject(pending.id)

        assert queue.approve(pending.id) is None

    def test_approve_already_expired_returns_none(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)
        # Force expire
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        queue.expire_stale()

        assert queue.approve(pending.id) is None


# ---------------------------------------------------------------------------
# PendingQueue.reject
# ---------------------------------------------------------------------------


class TestPendingQueueReject:
    def test_reject_pending_action(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)

        result = queue.reject(pending.id)

        assert result is not None
        assert result.status == PendingStatus.REJECTED

    def test_reject_nonexistent_returns_none(self) -> None:
        queue = PendingQueue()
        assert queue.reject("nonexistent") is None

    def test_reject_already_approved_returns_none(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)
        queue.approve(pending.id)

        assert queue.reject(pending.id) is None


# ---------------------------------------------------------------------------
# PendingQueue.expire_stale
# ---------------------------------------------------------------------------


class TestPendingQueueExpireStale:
    def test_expire_stale_finds_expired(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)
        # Force expire
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        expired = queue.expire_stale()

        assert len(expired) == 1
        assert expired[0].id == pending.id
        assert expired[0].status == PendingStatus.EXPIRED

    def test_expire_stale_ignores_not_yet_expired(self) -> None:
        queue = PendingQueue()
        queue.add("evt-1", _make_action(), "test", 30)

        expired = queue.expire_stale()

        assert expired == []

    def test_expire_stale_ignores_already_resolved(self) -> None:
        queue = PendingQueue()
        p1 = queue.add("evt-1", _make_action(), "test", 30)
        p2 = queue.add("evt-2", _make_action(), "test", 30)
        queue.approve(p1.id)
        queue.reject(p2.id)

        # Force both to look expired
        p1.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        p2.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        expired = queue.expire_stale()

        assert expired == []

    def test_expire_stale_multiple(self) -> None:
        queue = PendingQueue()
        p1 = queue.add("evt-1", _make_action(), "test", 30)
        p2 = queue.add("evt-2", _make_action(), "test", 30)

        p1.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        p2.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        expired = queue.expire_stale()

        assert len(expired) == 2


# ---------------------------------------------------------------------------
# PendingQueue.get and list_pending
# ---------------------------------------------------------------------------


class TestPendingQueueLookup:
    def test_get_existing(self) -> None:
        queue = PendingQueue()
        pending = queue.add("evt-1", _make_action(), "test", 30)

        result = queue.get(pending.id)

        assert result is not None
        assert result.id == pending.id

    def test_get_nonexistent(self) -> None:
        queue = PendingQueue()
        assert queue.get("nonexistent") is None

    def test_list_pending_returns_only_pending(self) -> None:
        queue = PendingQueue()
        p1 = queue.add("evt-1", _make_action(), "test", 30)
        p2 = queue.add("evt-2", _make_action(), "test", 30)
        p3 = queue.add("evt-3", _make_action(), "test", 30)
        queue.approve(p1.id)
        queue.reject(p2.id)

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
    def test_to_list_payload_only_pending(self) -> None:
        queue = PendingQueue()
        p1 = queue.add("evt-1", _make_action(), "test", 30)
        p2 = queue.add("evt-2", _make_action(), "test", 30)
        queue.approve(p1.id)

        payload = queue.to_list_payload()

        assert len(payload) == 1
        assert payload[0]["id"] == p2.id
        assert payload[0]["status"] == "pending"

    def test_to_list_payload_contains_action_data(self) -> None:
        queue = PendingQueue()
        queue.add("evt-1", _make_action(), "ZWave crash", 30)

        payload = queue.to_list_payload()

        assert payload[0]["diagnosis"] == "ZWave crash"
        assert payload[0]["action"]["handler"] == "homeassistant"
        assert payload[0]["action"]["operation"] == "restart_integration"


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
