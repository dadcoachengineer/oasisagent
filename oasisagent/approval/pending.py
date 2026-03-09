"""Pending action queue — holds RECOMMEND-tier actions awaiting operator approval.

RECOMMEND-tier actions are not auto-executed. Instead they enter the pending
queue where an operator can approve, reject, or let them expire. The queue
is an in-memory data structure; persistent storage is a Phase 3 concern.

ARCHITECTURE.md §16.2 describes the approval flow.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from oasisagent.models import RecommendedAction  # noqa: TC001 — Pydantic field type

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PendingStatus(StrEnum):
    """Lifecycle status of a pending action."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PendingAction(BaseModel):
    """A RECOMMEND-tier action awaiting operator approval."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    event_id: str
    action: RecommendedAction
    diagnosis: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    status: PendingStatus = PendingStatus.PENDING


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class PendingQueue:
    """In-memory queue of RECOMMEND-tier actions awaiting operator decisions.

    Thread-safety: not required — the orchestrator is single-threaded async.
    The approval listener and main loop share the same event loop, so access
    is serialized by the asyncio scheduler.
    """

    def __init__(self) -> None:
        self._actions: dict[str, PendingAction] = {}

    def add(
        self,
        event_id: str,
        action: RecommendedAction,
        diagnosis: str,
        timeout_minutes: int,
    ) -> PendingAction:
        """Create a pending action and add it to the queue.

        Args:
            event_id: Source event that produced this action.
            action: The RecommendedAction to hold for approval.
            diagnosis: Human-readable summary for the operator.
            timeout_minutes: Minutes until the action expires.

        Returns:
            The created PendingAction with a unique ID.
        """
        now = datetime.now(UTC)
        pending = PendingAction(
            event_id=event_id,
            action=action,
            diagnosis=diagnosis,
            created_at=now,
            expires_at=now + timedelta(minutes=timeout_minutes),
        )
        self._actions[pending.id] = pending
        logger.info(
            "Pending action %s enqueued: %s (expires %s)",
            pending.id,
            action.description[:80],
            pending.expires_at.isoformat(),
        )
        return pending

    def approve(self, action_id: str) -> PendingAction | None:
        """Mark a pending action as approved.

        Returns the action if it was PENDING, None if not found or
        already resolved (idempotent).
        """
        pending = self._actions.get(action_id)
        if pending is None:
            logger.warning("Approve: action %s not found", action_id)
            return None
        if pending.status != PendingStatus.PENDING:
            logger.info(
                "Approve: action %s already %s (idempotent no-op)",
                action_id,
                pending.status,
            )
            return None
        pending.status = PendingStatus.APPROVED
        logger.info("Pending action %s approved", action_id)
        return pending

    def reject(self, action_id: str) -> PendingAction | None:
        """Mark a pending action as rejected.

        Returns the action if it was PENDING, None if not found or
        already resolved (idempotent).
        """
        pending = self._actions.get(action_id)
        if pending is None:
            logger.warning("Reject: action %s not found", action_id)
            return None
        if pending.status != PendingStatus.PENDING:
            logger.info(
                "Reject: action %s already %s (idempotent no-op)",
                action_id,
                pending.status,
            )
            return None
        pending.status = PendingStatus.REJECTED
        logger.info("Pending action %s rejected", action_id)
        return pending

    def expire_stale(self) -> list[PendingAction]:
        """Find and mark all expired pending actions.

        Returns the list of newly expired actions for notification.

        NOTE: There is a small race window between this method and the
        approval listener — an operator's approve message may arrive
        between when we read the status and when we mark it expired.
        With in-memory storage and a single-threaded async event loop,
        this race is practically impossible (both run on the same loop).
        Persistent storage (Phase 3) should use compare-and-swap or a
        lock to eliminate the race entirely.
        """
        now = datetime.now(UTC)
        expired: list[PendingAction] = []

        for pending in self._actions.values():
            if pending.status == PendingStatus.PENDING and now >= pending.expires_at:
                pending.status = PendingStatus.EXPIRED
                expired.append(pending)
                logger.info("Pending action %s expired", pending.id)

        return expired

    def get(self, action_id: str) -> PendingAction | None:
        """Look up a pending action by ID. Returns None if not found."""
        return self._actions.get(action_id)

    @property
    def pending_count(self) -> int:
        """Return the number of actions with PENDING status."""
        return sum(1 for a in self._actions.values() if a.status == PendingStatus.PENDING)

    def list_pending(self) -> list[PendingAction]:
        """Return all actions with PENDING status."""
        return [
            a for a in self._actions.values()
            if a.status == PendingStatus.PENDING
        ]

    def to_list_payload(self) -> list[dict[str, Any]]:
        """Serialize current pending actions for MQTT publishing.

        Used for the oasis/pending/list retained message so new
        subscribers get current state immediately.
        """
        return [
            a.model_dump(mode="json")
            for a in self._actions.values()
            if a.status == PendingStatus.PENDING
        ]
