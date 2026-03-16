"""Add discovery_interval column to agent_config.

Supports configurable topology discovery interval (#218).
Default of 300 (5 minutes) matches the AgentConfig Pydantic model default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add discovery_interval column to agent_config."""
    await db.execute(
        "ALTER TABLE agent_config "
        "ADD COLUMN discovery_interval INTEGER NOT NULL DEFAULT 300"
    )
