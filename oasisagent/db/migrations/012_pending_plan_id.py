"""Add plan_id column to pending_actions table.

Links pending approval entries to RemediationPlan records. When plan_id
is set, approval/rejection routes through the plan executor instead of
single-action dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add plan_id column to pending_actions."""
    await db.execute(
        "ALTER TABLE pending_actions ADD COLUMN plan_id TEXT DEFAULT NULL"
    )
