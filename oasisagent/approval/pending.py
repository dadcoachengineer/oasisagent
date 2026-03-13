"""Pending action queue — holds RECOMMEND-tier actions awaiting operator approval.

RECOMMEND-tier actions are not auto-executed. Instead they enter the pending
queue where an operator can approve, reject, or let them expire.

When a database connection is provided, the queue persists to SQLite and
uses compare-and-swap (CAS) for status transitions. When ``db=None``,
the queue operates in pure in-memory mode (for tests).

ARCHITECTURE.md §16.2 describes the approval flow.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from oasisagent.models import RecommendedAction

if TYPE_CHECKING:
    from sqlite3 import Row
    from typing import Any

    import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ApprovalDecision(StrEnum):
    """Decision an operator makes on a pending action via an interactive channel."""

    APPROVED = "approved"
    REJECTED = "rejected"


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

    # Event context — denormalized for display without requiring a lookup.
    # Stored as plain strings to keep the approval module decoupled from
    # the Event/Severity models. Defaults to "" for backward compatibility
    # with existing MQTT retained messages.
    entity_id: str = ""
    severity: str = ""
    source: str = ""
    system: str = ""


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class PendingQueue:
    """Queue of RECOMMEND-tier actions awaiting operator decisions.

    Dual-layer: SQLite is the source of truth (when ``db`` is provided),
    and the in-memory dict is the hot cache for fast reads. All mutations
    write to SQLite first, then update the dict.

    When ``db=None``, operates in pure in-memory mode (tests).

    Thread-safety: not required — the orchestrator is single-threaded async.
    """

    def __init__(self, db: aiosqlite.Connection | None = None) -> None:
        self._db = db
        self._actions: dict[str, PendingAction] = {}
        self._pending_keys: set[str] = set()
        if db is None:
            logger.warning(
                "PendingQueue created without database — actions will not "
                "survive restart"
            )

    @classmethod
    async def from_db(cls, db: aiosqlite.Connection) -> PendingQueue:
        """Create a queue and load pending rows from SQLite.

        Rows that expired while the process was down (status='pending'
        but expires_at < now) are transitioned to 'expired' before
        loading into the in-memory cache.
        """
        queue = cls.__new__(cls)
        queue._db = db
        queue._actions = {}

        # Sweep stale-pending rows that expired while we were down (D5)
        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            "UPDATE pending_actions SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at <= ? "
            "RETURNING id",
            (now,),
        )
        expired_rows = await cursor.fetchall()
        if expired_rows:
            await db.commit()
            count = len(expired_rows)
            logger.info(
                "Swept %d stale pending action(s) to expired on startup", count
            )

        # Load remaining pending rows into the in-memory cache
        cursor = await db.execute(
            "SELECT id, event_id, action_json, diagnosis, status, "
            "created_at, expires_at, entity_id, severity, source, system "
            "FROM pending_actions WHERE status = 'pending'"
        )
        rows = await cursor.fetchall()
        for row in rows:
            pending = _row_to_pending_action(row)
            queue._actions[pending.id] = pending

        if rows:
            logger.info("Loaded %d pending action(s) from database", len(rows))

        # Rebuild dedup keys from loaded pending actions
        queue._pending_keys = {
            PendingQueue._make_key(p.action)
            for p in queue._actions.values()
        }

        return queue

    async def add(
        self,
        event_id: str,
        action: RecommendedAction,
        diagnosis: str,
        timeout_minutes: int,
        *,
        entity_id: str = "",
        severity: str = "",
        source: str = "",
        system: str = "",
    ) -> PendingAction | None:
        """Create a pending action and add it to the queue.

        Args:
            event_id: Source event that produced this action.
            action: The RecommendedAction to hold for approval.
            diagnosis: Human-readable summary for the operator.
            timeout_minutes: Minutes until the action expires.
            entity_id: Entity affected (e.g. ``sensor.temperature``).
            severity: Event severity as string (e.g. ``warning``).
            source: Ingestion source (e.g. ``mqtt``).
            system: Target system (e.g. ``homeassistant``).

        Returns:
            The created PendingAction with a unique ID, or None if a
            duplicate pending action already exists.
        """
        key = self._make_key(action)
        if key in self._pending_keys:
            logger.debug(
                "Duplicate pending action suppressed: %s", key
            )
            return None

        now = datetime.now(UTC)
        pending = PendingAction(
            event_id=event_id,
            action=action,
            diagnosis=diagnosis,
            created_at=now,
            expires_at=now + timedelta(minutes=timeout_minutes),
            entity_id=entity_id,
            severity=severity,
            source=source,
            system=system,
        )

        # SQLite first, then in-memory cache (D2)
        if self._db is not None:
            await self._db.execute(
                "INSERT INTO pending_actions "
                "(id, event_id, action_json, diagnosis, status, "
                "created_at, expires_at, entity_id, severity, source, system) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pending.id,
                    pending.event_id,
                    pending.action.model_dump_json(),
                    pending.diagnosis,
                    pending.status.value,
                    pending.created_at.isoformat(),
                    pending.expires_at.isoformat(),
                    pending.entity_id,
                    pending.severity,
                    pending.source,
                    pending.system,
                ),
            )
            await self._db.commit()

        self._actions[pending.id] = pending
        self._pending_keys.add(key)
        logger.info(
            "Pending action %s enqueued: %s (expires %s)",
            pending.id,
            action.description[:80],
            pending.expires_at.isoformat(),
        )
        return pending

    async def approve(self, action_id: str) -> PendingAction | None:
        """Mark a pending action as approved.

        Uses compare-and-swap: only transitions from PENDING → APPROVED.
        Returns the action if successful, None if not found or already
        resolved (CAS failed).
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

        # CAS: SQLite first, then in-memory (D1, D2)
        if self._db is not None:
            cursor = await self._db.execute(
                "UPDATE pending_actions SET status = 'approved' "
                "WHERE id = ? AND status = 'pending'",
                (action_id,),
            )
            if cursor.rowcount == 0:
                logger.warning(
                    "Approve CAS failed: action %s no longer pending in DB",
                    action_id,
                )
                return None
            await self._db.commit()

        pending.status = PendingStatus.APPROVED
        self._pending_keys.discard(self._make_key(pending.action))
        logger.info("Pending action %s approved", action_id)
        return pending

    async def reject(self, action_id: str) -> PendingAction | None:
        """Mark a pending action as rejected.

        Uses compare-and-swap: only transitions from PENDING → REJECTED.
        Returns the action if successful, None if not found or already
        resolved (CAS failed).
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

        # CAS: SQLite first, then in-memory (D1, D2)
        if self._db is not None:
            cursor = await self._db.execute(
                "UPDATE pending_actions SET status = 'rejected' "
                "WHERE id = ? AND status = 'pending'",
                (action_id,),
            )
            if cursor.rowcount == 0:
                logger.warning(
                    "Reject CAS failed: action %s no longer pending in DB",
                    action_id,
                )
                return None
            await self._db.commit()

        pending.status = PendingStatus.REJECTED
        self._pending_keys.discard(self._make_key(pending.action))
        logger.info("Pending action %s rejected", action_id)
        return pending

    async def expire_stale(self) -> list[PendingAction]:
        """Find and mark all expired pending actions.

        Returns the list of newly expired actions for notification.
        Uses CAS in SQLite to eliminate the race window between this
        method and the approval listener.
        """
        now = datetime.now(UTC)

        if self._db is not None:
            # CAS: atomically mark expired in SQLite, then update cache (D2)
            cursor = await self._db.execute(
                "UPDATE pending_actions SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at <= ? "
                "RETURNING id",
                (now.isoformat(),),
            )
            expired_ids = {row[0] for row in await cursor.fetchall()}
            if expired_ids:
                await self._db.commit()

            expired: list[PendingAction] = []
            for eid in expired_ids:
                pending = self._actions.get(eid)
                if pending is not None:
                    pending.status = PendingStatus.EXPIRED
                    expired.append(pending)
                    logger.info("Pending action %s expired", pending.id)

            # Rebuild keys from remaining PENDING actions. Handles the
            # edge case where _actions was out of sync with the DB —
            # prevents stale keys from blocking future additions.
            self._pending_keys = {
                self._make_key(a.action)
                for a in self._actions.values()
                if a.status == PendingStatus.PENDING
            }
            return expired

        # In-memory-only path (tests)
        expired = []
        for pending in self._actions.values():
            if pending.status == PendingStatus.PENDING and now >= pending.expires_at:
                pending.status = PendingStatus.EXPIRED
                self._pending_keys.discard(self._make_key(pending.action))
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


    @staticmethod
    def _make_key(action: RecommendedAction) -> str:
        """Build a dedup key from handler, operation, and target entity."""
        return f"{action.handler}:{action.operation}:{action.target_entity_id or ''}"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_pending_action(row: Row) -> PendingAction:
    """Convert a SQLite row to a PendingAction."""
    action = RecommendedAction.model_validate_json(row["action_json"])
    return PendingAction(
        id=row["id"],
        event_id=row["event_id"],
        action=action,
        diagnosis=row["diagnosis"],
        status=PendingStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        entity_id=row["entity_id"] or "",
        severity=row["severity"] or "",
        source=row["source"] or "",
        system=row["system"] or "",
    )
