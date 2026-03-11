"""SQLite database initialization and migration runner.

On startup, opens (or creates) the database at ``{OASIS_DATA_DIR}/oasisagent.db``,
enables WAL mode for concurrent reads during web UI operations, checks the
``schema_version``, and runs any pending numbered migration scripts.

Migrations are idempotent: ``run_migrations`` checks ``_meta.schema_version``
before running and only applies scripts with a higher number.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from pathlib import Path  # noqa: TC003 — used at runtime in run_migrations()

import aiosqlite

logger = logging.getLogger(__name__)

_MIGRATION_PATTERN = re.compile(r"^(\d{3})_\w+$")


async def _ensure_meta_table(db: aiosqlite.Connection) -> None:
    """Create the ``_meta`` table if it does not exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS _meta (
            schema_version  INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Ensure exactly one row exists
    cursor = await db.execute("SELECT COUNT(*) FROM _meta")
    row = await cursor.fetchone()
    if row and row[0] == 0:
        await db.execute("INSERT INTO _meta (schema_version) VALUES (0)")
    await db.commit()


async def _get_schema_version(db: aiosqlite.Connection) -> int:
    """Read the current schema version from ``_meta``."""
    cursor = await db.execute("SELECT schema_version FROM _meta")
    row = await cursor.fetchone()
    return row[0] if row else 0


def _discover_migrations() -> list[tuple[int, str]]:
    """Discover migration modules in ``oasisagent.db.migrations``.

    Returns a sorted list of ``(version_number, module_name)`` tuples.
    """
    import oasisagent.db.migrations as migrations_pkg

    migrations: list[tuple[int, str]] = []
    for info in pkgutil.iter_modules(migrations_pkg.__path__):
        match = _MIGRATION_PATTERN.match(info.name)
        if match:
            version = int(match.group(1))
            migrations.append((version, info.name))

    migrations.sort(key=lambda m: m[0])
    return migrations


async def run_migrations(db_path: Path) -> aiosqlite.Connection:
    """Open the database, enable WAL mode, and run pending migrations.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An open ``aiosqlite.Connection`` ready for use.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))

    # WAL mode: concurrent reads during API CRUD while orchestrator reads config
    await db.execute("PRAGMA journal_mode=WAL")
    # Return rows as sqlite3.Row for column-name access
    db.row_factory = aiosqlite.Row

    await _ensure_meta_table(db)
    current_version = await _get_schema_version(db)
    migrations = _discover_migrations()

    for version, module_name in migrations:
        if version <= current_version:
            continue

        logger.info("Running migration %s (version %d → %d)", module_name, current_version, version)
        module = importlib.import_module(f"oasisagent.db.migrations.{module_name}")
        migrate_fn = module.migrate

        await migrate_fn(db)
        await db.execute(
            "UPDATE _meta SET schema_version = ?, updated_at = datetime('now')",
            (version,),
        )
        await db.commit()
        current_version = version

    logger.info("Database schema at version %d", current_version)
    return db
