"""Add plan_step_notifications column to agent_config.

Opt-in per-step notifications during plan execution. Defaults to False
(only plan-level notifications fire).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add plan_step_notifications column to agent_config."""
    await db.execute(
        "ALTER TABLE agent_config "
        "ADD COLUMN plan_step_notifications INTEGER NOT NULL DEFAULT 0"
    )
