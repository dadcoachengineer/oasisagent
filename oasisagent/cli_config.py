"""Config import/export CLI — backup, migration, and headless deployment.

Commands:
    oasisagent config export              Dump SQLite config to YAML (stdout)
    oasisagent config export --include-secrets   Include decrypted secrets
    oasisagent config import seed.yaml    Seed database from YAML file

The export format includes ``schema_version`` for compatibility checking.
Secrets are masked as ``<encrypted>`` by default; use ``--include-secrets``
to include them in plaintext (for backup or migration to a new instance).

Import uses upsert semantics: rows with matching names are updated,
new names are created. Agent config is always merged.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING, Any

import yaml

from oasisagent.db.config_store import SECRET_MASK, ConfigStore
from oasisagent.db.crypto import CryptoProvider, resolve_secret_key
from oasisagent.db.registry import CONNECTOR_TYPES, CORE_SERVICE_TYPES, NOTIFICATION_TYPES
from oasisagent.db.schema import _get_schema_version, run_migrations

if TYPE_CHECKING:
    import argparse

    import aiosqlite

# Placeholder shown in exported YAML for masked secrets
_EXPORT_SECRET_PLACEHOLDER = "<encrypted>"


# ---------------------------------------------------------------------------
# Database bootstrap (shared by import and export)
# ---------------------------------------------------------------------------


async def _open_db() -> tuple[aiosqlite.Connection, CryptoProvider]:
    """Open the config database and crypto provider from bootstrap env vars."""
    from pathlib import Path

    data_dir = Path(os.environ.get("OASIS_DATA_DIR", "/data"))
    secret_key = resolve_secret_key(data_dir)
    crypto = CryptoProvider(secret_key)
    db = await run_migrations(data_dir / "oasisagent.db")
    return db, crypto


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _mask_secrets(
    rows: list[dict[str, Any]], table: str, store: ConfigStore,
) -> list[dict[str, Any]]:
    """Replace secret values with placeholders for safe export."""
    return [store.mask_row(table, row) for row in rows]


def _row_to_export(
    row: dict[str, Any], *, include_secrets: bool, table: str, store: ConfigStore,
) -> dict[str, Any]:
    """Convert a DB row dict to the export YAML format."""
    if not include_secrets:
        row = store.mask_row(table, row)
        # Replace the API mask with the export placeholder
        config = dict(row["config"])
        for key, value in config.items():
            if value == SECRET_MASK:
                config[key] = _EXPORT_SECRET_PLACEHOLDER
        row = {**row, "config": config}

    return {
        "type": row["type"],
        "name": row["name"],
        "enabled": row["enabled"],
        "config": row["config"],
    }


async def _cmd_export(include_secrets: bool) -> int:
    """Export current config from SQLite to YAML on stdout."""
    db, crypto = await _open_db()
    try:
        store = ConfigStore(db, crypto)
        schema_version = await _get_schema_version(db)

        # Agent config
        config = await store.load_config()
        agent_dict = config.agent.model_dump(mode="json")

        # Typed rows
        connectors = await store.list_connectors()
        services = await store.list_services()
        notifications = await store.list_notifications()

        kw = {"include_secrets": include_secrets, "store": store}
        export_data: dict[str, Any] = {
            "schema_version": schema_version,
            "agent": agent_dict,
            "connectors": [
                _row_to_export(r, table="connectors", **kw) for r in connectors
            ],
            "services": [
                _row_to_export(r, table="core_services", **kw) for r in services
            ],
            "notifications": [
                _row_to_export(r, table="notification_channels", **kw)
                for r in notifications
            ],
        }

        yaml_out = yaml.dump(export_data, default_flow_style=False, sort_keys=False)
        print(yaml_out, end="")
        return 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _validate_import_data(data: dict[str, Any]) -> None:
    """Validate the structure of import YAML data.

    Raises:
        ValueError: If the data structure is invalid.
    """
    if not isinstance(data, dict):
        msg = "Import file must contain a YAML mapping"
        raise ValueError(msg)

    # Validate connector/service/notification types if present
    for row in data.get("connectors", []):
        if row["type"] not in CONNECTOR_TYPES:
            valid = ", ".join(sorted(CONNECTOR_TYPES.keys()))
            msg = f"Unknown connector type: {row['type']!r}. Valid: {valid}"
            raise ValueError(msg)

    for row in data.get("services", []):
        if row["type"] not in CORE_SERVICE_TYPES:
            valid = ", ".join(sorted(CORE_SERVICE_TYPES.keys()))
            msg = f"Unknown service type: {row['type']!r}. Valid: {valid}"
            raise ValueError(msg)

    for row in data.get("notifications", []):
        if row["type"] not in NOTIFICATION_TYPES:
            valid = ", ".join(sorted(NOTIFICATION_TYPES.keys()))
            msg = f"Unknown notification type: {row['type']!r}. Valid: {valid}"
            raise ValueError(msg)


def _strip_export_placeholders(config: dict[str, Any]) -> dict[str, Any]:
    """Remove fields set to the export placeholder — don't overwrite real secrets."""
    return {k: v for k, v in config.items() if v != _EXPORT_SECRET_PLACEHOLDER}


async def _upsert_rows(
    store: ConfigStore,
    table: str,
    rows: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    create_fn: str,
    update_fn: str,
) -> tuple[int, int]:
    """Upsert rows into a typed table. Returns (created, updated) counts."""
    existing_by_name = {r["name"]: r for r in existing}
    created = 0
    updated = 0

    for row in rows:
        config = _strip_export_placeholders(row.get("config", {}))
        name = row["name"]
        enabled = row.get("enabled", True)

        if name in existing_by_name:
            # Update: merge config (preserves secrets not in import)
            existing_row = existing_by_name[name]
            merged_config = {**existing_row["config"], **config}
            await getattr(store, update_fn)(
                existing_row["id"],
                {"name": name, "enabled": enabled, "config": merged_config},
            )
            updated += 1
        else:
            # Create: use config as-is (secrets must be provided)
            await getattr(store, create_fn)(
                row["type"], name, config, enabled=enabled,
            )
            created += 1

    return created, updated


async def _cmd_import(file_path: str) -> int:
    """Import config from YAML file into SQLite."""
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        return 1

    raw = path.read_text()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"Error: invalid YAML: {exc}", file=sys.stderr)
        return 1

    try:
        _validate_import_data(data)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    db, crypto = await _open_db()
    try:
        store = ConfigStore(db, crypto)

        # Check schema version compatibility
        current_version = await _get_schema_version(db)
        import_version = data.get("schema_version")
        if import_version is not None and import_version > current_version:
            print(
                f"Error: import file schema_version ({import_version}) is newer "
                f"than database ({current_version}). Update OasisAgent first.",
                file=sys.stderr,
            )
            return 1

        total_created = 0
        total_updated = 0

        # Agent config
        if "agent" in data:
            agent_updates = data["agent"]
            if isinstance(agent_updates, dict) and agent_updates:
                await store.update_agent_config(agent_updates)
                print("  Updated agent config")

        # Connectors
        if connectors := data.get("connectors", []):
            existing = await store.list_connectors()
            c, u = await _upsert_rows(
                store, "connectors", connectors, existing,
                "create_connector", "update_connector",
            )
            total_created += c
            total_updated += u
            print(f"  Connectors: {c} created, {u} updated")

        # Services
        if services := data.get("services", []):
            existing = await store.list_services()
            c, u = await _upsert_rows(
                store, "core_services", services, existing,
                "create_service", "update_service",
            )
            total_created += c
            total_updated += u
            print(f"  Services: {c} created, {u} updated")

        # Notifications
        if notifications := data.get("notifications", []):
            existing = await store.list_notifications()
            c, u = await _upsert_rows(
                store, "notification_channels", notifications, existing,
                "create_notification", "update_notification",
            )
            total_created += c
            total_updated += u
            print(f"  Notifications: {c} created, {u} updated")

        print(f"Import complete: {total_created} created, {total_updated} updated")
        return 0

    except Exception as exc:
        print(f"Error during import: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_config_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the 'config' sub-command group to the argument parser."""
    config_parser = subparsers.add_parser(
        "config",
        help="Import/export configuration",
    )

    config_sub = config_parser.add_subparsers(dest="config_command")

    export_parser = config_sub.add_parser(
        "export",
        help="Export config to YAML (stdout)",
    )
    export_parser.add_argument(
        "--include-secrets",
        action="store_true",
        default=False,
        help="Include decrypted secrets in export (default: masked)",
    )

    import_parser = config_sub.add_parser(
        "import",
        help="Import config from YAML file",
    )
    import_parser.add_argument(
        "file",
        help="Path to YAML config file",
    )


def run_config_command(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate config sub-command."""
    try:
        if args.config_command == "export":
            exit_code = asyncio.run(_cmd_export(args.include_secrets))
        elif args.config_command == "import":
            exit_code = asyncio.run(_cmd_import(args.file))
        else:
            print("Usage: oasisagent config {export,import}")
            exit_code = 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        exit_code = 1

    sys.exit(exit_code)
