"""Add max_consecutive_identical column to agent_config.

Supports the repeated-event suppression feature (#168).
Default of 3 matches the AgentConfig Pydantic model default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add max_consecutive_identical column to agent_config."""
    await db.execute(
        "ALTER TABLE agent_config "
        "ADD COLUMN max_consecutive_identical INTEGER NOT NULL DEFAULT 3"
    )
