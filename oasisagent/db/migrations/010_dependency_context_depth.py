"""Add dependency_context_depth column to agent_config.

Supports configurable BFS depth for T2 dependency context extraction.
Default of 2 matches the AgentConfig Pydantic model default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add dependency_context_depth column to agent_config."""
    await db.execute(
        "ALTER TABLE agent_config "
        "ADD COLUMN dependency_context_depth INTEGER NOT NULL DEFAULT 2"
    )
