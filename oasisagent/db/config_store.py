"""Config store — reads and writes OasisAgentConfig to/from SQLite.

The store owns the boundary between the database (encrypted JSON blobs) and
the application (validated Pydantic models). Encryption/decryption happens
here, so the rest of the app never touches ciphertext.

YAML fallback: triggers only when ``agent_config`` has its default seed row
and zero connectors/services/notifications exist (truly virgin DB). Once any
config is written to SQLite — even partial — YAML is permanently ignored.
This is a one-way door.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.config import (
    AgentConfig,
    AuditConfig,
    GuardrailsConfig,
    HandlersConfig,
    IngestionConfig,
    LlmConfig,
    NotificationsConfig,
    OasisAgentConfig,
)
from oasisagent.db.registry import (
    get_type_meta,
    merge_secrets,
    split_secrets,
)

if TYPE_CHECKING:
    import aiosqlite

    from oasisagent.db.crypto import CryptoProvider

logger = logging.getLogger(__name__)

# Secret mask shown in API responses
SECRET_MASK = "********"


class ConfigStore:
    """CRUD operations on the SQLite config database."""

    def __init__(self, db: aiosqlite.Connection, crypto: CryptoProvider) -> None:
        self._db = db
        self._crypto = crypto

    # ------------------------------------------------------------------
    # Virgin DB detection (YAML fallback gate)
    # ------------------------------------------------------------------

    async def is_virgin(self) -> bool:
        """Return True if the database has never been configured.

        A virgin DB has zero rows in connectors, core_services, and
        notification_channels. Once any row is written, this returns
        False permanently.
        """
        for table in ("connectors", "core_services", "notification_channels"):
            cursor = await self._db.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            if row and row[0] > 0:
                return False
        return True

    # ------------------------------------------------------------------
    # Load full config
    # ------------------------------------------------------------------

    async def load_config(self) -> OasisAgentConfig:
        """Build a complete ``OasisAgentConfig`` from the database.

        Reads all tables, decrypts secrets, validates through Pydantic,
        and returns the config object the Orchestrator expects.
        """
        agent = await self._load_agent_config()
        ingestion = await self._load_ingestion()
        llm = await self._load_llm()
        handlers = await self._load_handlers()
        guardrails = await self._load_guardrails()
        audit = await self._load_audit()
        notifications = await self._load_notifications()

        return OasisAgentConfig(
            agent=agent,
            ingestion=ingestion,
            llm=llm,
            handlers=handlers,
            guardrails=guardrails,
            audit=audit,
            notifications=notifications,
        )

    # ------------------------------------------------------------------
    # Agent config (single-row)
    # ------------------------------------------------------------------

    async def _load_agent_config(self) -> AgentConfig:
        cursor = await self._db.execute("SELECT * FROM agent_config WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return AgentConfig()
        return AgentConfig(
            name=row["name"],
            log_level=row["log_level"],
            event_queue_size=row["event_queue_size"],
            dedup_window_seconds=row["dedup_window_seconds"],
            shutdown_timeout=row["shutdown_timeout"],
            event_ttl=row["event_ttl"],
            known_fixes_dir=row["known_fixes_dir"],
            correlation_window=row["correlation_window"],
            metrics_port=row["metrics_port"],
        )

    async def update_agent_config(self, updates: dict[str, Any]) -> AgentConfig:
        """Update agent config fields. Validates through Pydantic before saving."""
        valid_fields = set(AgentConfig.model_fields)
        if bad := set(updates) - valid_fields:
            msg = f"Unknown fields: {bad}"
            raise ValueError(msg)

        current = await self._load_agent_config()
        merged = {**current.model_dump(), **updates}
        validated = AgentConfig.model_validate(merged)

        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        await self._db.execute(f"UPDATE agent_config SET {sets} WHERE id = 1", values)
        await self._db.commit()
        return validated

    # ------------------------------------------------------------------
    # Generic CRUD for typed tables (connectors, core_services, notifications)
    # ------------------------------------------------------------------

    async def _read_rows(self, table: str) -> list[dict[str, Any]]:
        """Read all rows from a typed table, decrypting secrets."""
        cursor = await self._db.execute(f"SELECT * FROM {table}")
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            config_data = json.loads(row["config_json"])
            secrets = self._crypto.decrypt(row["secrets_json"])
            merged = merge_secrets(config_data, secrets)
            result.append({
                "id": row["id"],
                "type": row["type"],
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "config": merged,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result

    async def _read_row(self, table: str, row_id: int) -> dict[str, Any] | None:
        """Read a single row by ID, decrypting secrets."""
        cursor = await self._db.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        config_data = json.loads(row["config_json"])
        secrets = self._crypto.decrypt(row["secrets_json"])
        merged = merge_secrets(config_data, secrets)
        return {
            "id": row["id"],
            "type": row["type"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "config": merged,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def _create_row(
        self, table: str, type_name: str, name: str, config: dict[str, Any],
        *, enabled: bool = True,
    ) -> int:
        """Create a new row in a typed table.

        Validates the config through the type's Pydantic model, splits
        secrets from non-secret fields, encrypts secrets, and inserts.

        Returns:
            The new row ID.

        Raises:
            ValueError: If the type is unknown or config is invalid.
        """
        type_meta = get_type_meta(table, type_name)

        # Validate through Pydantic (raises ValidationError on bad config)
        type_meta.model.model_validate(config)

        config_dict, secrets_dict = split_secrets(type_meta, config)
        now = datetime.now(tz=UTC).isoformat()

        cursor = await self._db.execute(
            f"INSERT INTO {table} (type, name, enabled, config_json, secrets_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                type_name,
                name,
                int(enabled),
                json.dumps(config_dict, sort_keys=True),
                self._crypto.encrypt(secrets_dict),
                now,
                now,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def _update_row(
        self, table: str, row_id: int, updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update a row in a typed table.

        Merges updates with existing config, re-validates, re-encrypts.

        Returns:
            The updated row dict, or None if not found.
        """
        existing = await self._read_row(table, row_id)
        if not existing:
            return None

        type_meta = get_type_meta(table, existing["type"])

        # Separate top-level fields from config updates
        name = updates.pop("name", existing["name"])
        enabled = updates.pop("enabled", existing["enabled"])
        config_updates = updates.pop("config", None)

        if config_updates is not None:
            merged_config = {**existing["config"], **config_updates}
        else:
            merged_config = existing["config"]

        # Validate through Pydantic
        type_meta.model.model_validate(merged_config)

        config_dict, secrets_dict = split_secrets(type_meta, merged_config)
        now = datetime.now(tz=UTC).isoformat()

        await self._db.execute(
            f"UPDATE {table} SET name = ?, enabled = ?, config_json = ?, "
            "secrets_json = ?, updated_at = ? WHERE id = ?",
            (
                name,
                int(enabled),
                json.dumps(config_dict, sort_keys=True),
                self._crypto.encrypt(secrets_dict),
                now,
                row_id,
            ),
        )
        await self._db.commit()
        return await self._read_row(table, row_id)

    async def _delete_row(self, table: str, row_id: int) -> bool:
        """Delete a row by ID. Returns True if deleted, False if not found."""
        cursor = await self._db.execute(
            f"DELETE FROM {table} WHERE id = ?", (row_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Convenience: typed CRUD wrappers
    # ------------------------------------------------------------------

    async def list_connectors(self) -> list[dict[str, Any]]:
        """List all connectors with decrypted config."""
        return await self._read_rows("connectors")

    async def get_connector(self, row_id: int) -> dict[str, Any] | None:
        """Get a single connector by ID."""
        return await self._read_row("connectors", row_id)

    async def create_connector(
        self, type_name: str, name: str, config: dict[str, Any],
        *, enabled: bool = True,
    ) -> int:
        """Create a connector."""
        return await self._create_row("connectors", type_name, name, config, enabled=enabled)

    async def update_connector(self, row_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update a connector."""
        return await self._update_row("connectors", row_id, updates)

    async def delete_connector(self, row_id: int) -> bool:
        """Delete a connector."""
        return await self._delete_row("connectors", row_id)

    async def list_services(self) -> list[dict[str, Any]]:
        """List all core services."""
        return await self._read_rows("core_services")

    async def get_service(self, row_id: int) -> dict[str, Any] | None:
        """Get a single core service by ID."""
        return await self._read_row("core_services", row_id)

    async def create_service(
        self, type_name: str, name: str, config: dict[str, Any],
        *, enabled: bool = True,
    ) -> int:
        """Create a core service."""
        return await self._create_row("core_services", type_name, name, config, enabled=enabled)

    async def update_service(self, row_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update a core service."""
        return await self._update_row("core_services", row_id, updates)

    async def delete_service(self, row_id: int) -> bool:
        """Delete a core service."""
        return await self._delete_row("core_services", row_id)

    async def list_notifications(self) -> list[dict[str, Any]]:
        """List all notification channels."""
        return await self._read_rows("notification_channels")

    async def get_notification(self, row_id: int) -> dict[str, Any] | None:
        """Get a single notification channel by ID."""
        return await self._read_row("notification_channels", row_id)

    async def create_notification(
        self, type_name: str, name: str, config: dict[str, Any],
        *, enabled: bool = True,
    ) -> int:
        """Create a notification channel."""
        return await self._create_row(
            "notification_channels", type_name, name, config, enabled=enabled,
        )

    async def update_notification(
        self, row_id: int, updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update a notification channel."""
        return await self._update_row("notification_channels", row_id, updates)

    async def delete_notification(self, row_id: int) -> bool:
        """Delete a notification channel."""
        return await self._delete_row("notification_channels", row_id)

    # ------------------------------------------------------------------
    # Mask secrets for API responses
    # ------------------------------------------------------------------

    def mask_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of a row dict with secret fields masked."""
        try:
            type_meta = get_type_meta(table, row["type"])
        except ValueError:
            return row

        masked_config = dict(row["config"])
        for field_name in type_meta.secret_fields:
            if masked_config.get(field_name):
                masked_config[field_name] = SECRET_MASK
        return {**row, "config": masked_config}

    # ------------------------------------------------------------------
    # Build sub-configs from typed rows
    # ------------------------------------------------------------------

    def _rows_by_type(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Index rows by type, keeping only the first enabled row per type."""
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["enabled"] and row["type"] not in result:
                result[row["type"]] = row
        return result

    async def _load_ingestion(self) -> IngestionConfig:
        rows = await self.list_connectors()
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "mqtt" in by_type:
            kwargs["mqtt"] = by_type["mqtt"]["config"]
        if "ha_websocket" in by_type:
            kwargs["ha_websocket"] = by_type["ha_websocket"]["config"]
        if "ha_log_poller" in by_type:
            kwargs["ha_log_poller"] = by_type["ha_log_poller"]["config"]

        return IngestionConfig.model_validate(kwargs) if kwargs else IngestionConfig()

    async def _load_llm(self) -> LlmConfig:
        rows = await self.list_services()
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "llm_triage" in by_type:
            kwargs["triage"] = by_type["llm_triage"]["config"]
        if "llm_reasoning" in by_type:
            kwargs["reasoning"] = by_type["llm_reasoning"]["config"]
        if "llm_options" in by_type:
            kwargs["options"] = by_type["llm_options"]["config"]

        return LlmConfig.model_validate(kwargs) if kwargs else LlmConfig()

    async def _load_handlers(self) -> HandlersConfig:
        rows = await self.list_services()
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "ha_handler" in by_type:
            kwargs["homeassistant"] = by_type["ha_handler"]["config"]
        if "docker_handler" in by_type:
            kwargs["docker"] = by_type["docker_handler"]["config"]
        if "proxmox_handler" in by_type:
            kwargs["proxmox"] = by_type["proxmox_handler"]["config"]

        return HandlersConfig.model_validate(kwargs) if kwargs else HandlersConfig()

    async def _load_guardrails(self) -> GuardrailsConfig:
        rows = await self.list_services()
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "guardrails" in by_type:
            kwargs = dict(by_type["guardrails"]["config"])
        if "circuit_breaker" in by_type:
            kwargs["circuit_breaker"] = by_type["circuit_breaker"]["config"]

        return GuardrailsConfig.model_validate(kwargs) if kwargs else GuardrailsConfig()

    async def _load_audit(self) -> AuditConfig:
        rows = await self.list_services()
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "influxdb" in by_type:
            kwargs["influxdb"] = by_type["influxdb"]["config"]

        return AuditConfig.model_validate(kwargs) if kwargs else AuditConfig()

    async def _load_notifications(self) -> NotificationsConfig:
        rows = await self.list_notifications()
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "mqtt_notification" in by_type:
            kwargs["mqtt"] = by_type["mqtt_notification"]["config"]
        if "email" in by_type:
            kwargs["email"] = by_type["email"]["config"]
        if "webhook" in by_type:
            kwargs["webhook"] = by_type["webhook"]["config"]

        return NotificationsConfig.model_validate(kwargs) if kwargs else NotificationsConfig()
