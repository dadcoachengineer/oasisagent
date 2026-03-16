"""Create the remediation_plans table for plan-aware dispatch.

Stores multi-step remediation plans produced by T2 reasoning. Plans track
step-level execution state, enabling ordered dependency-aware dispatch
with whole-plan approval for non-AUTO_FIX risk tiers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Create remediation_plans table with status and event indexes."""
    await db.execute("""
        CREATE TABLE remediation_plans (
            id                  TEXT PRIMARY KEY,
            event_id            TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending_approval',
            steps_json          TEXT NOT NULL,
            step_states_json    TEXT NOT NULL,
            diagnosis           TEXT NOT NULL DEFAULT '',
            effective_risk_tier TEXT NOT NULL,
            entity_id           TEXT NOT NULL DEFAULT '',
            severity            TEXT NOT NULL DEFAULT '',
            source              TEXT NOT NULL DEFAULT '',
            system              TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            completed_at        TEXT
        )
    """)
    await db.execute("""
        CREATE INDEX idx_remediation_plans_status
        ON remediation_plans (status)
    """)
    await db.execute("""
        CREATE INDEX idx_remediation_plans_event
        ON remediation_plans (event_id)
    """)
