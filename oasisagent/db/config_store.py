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
from contextlib import asynccontextmanager
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
    from collections.abc import AsyncGenerator

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
        self._has_admin_cache: bool | None = None
        self._in_transaction = False

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        """Execute a block inside a SQLite transaction.

        Commits on success, rolls back on exception. While active,
        individual CRUD methods skip their per-call commits.
        """
        await self._db.execute("BEGIN")
        self._in_transaction = True
        try:
            yield
            await self._db.execute("COMMIT")
        except BaseException:
            await self._db.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    async def _commit(self) -> None:
        """Commit unless inside a managed transaction."""
        if not self._in_transaction:
            await self._db.commit()

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
    # YAML → SQLite import (one-way door for virgin databases)
    # ------------------------------------------------------------------

    async def import_yaml(self, config: OasisAgentConfig) -> None:
        """Seed SQLite from a parsed YAML config.

        Imports all enabled connectors, services, and notification channels
        into the database so the orchestrator and UI share a single source
        of truth.  Only call this on a virgin database — once rows exist
        the YAML is permanently ignored.
        """
        async with self.transaction():
            # --- Agent config ---
            agent = config.agent
            await self._db.execute(
                "UPDATE agent_config SET name=?, log_level=?, "
                "event_queue_size=?, dedup_window_seconds=?, "
                "shutdown_timeout=?, event_ttl=?, known_fixes_dir=?, "
                "correlation_window=?, metrics_port=? WHERE id=1",
                (
                    agent.name, agent.log_level,
                    agent.event_queue_size, agent.dedup_window_seconds,
                    agent.shutdown_timeout, agent.event_ttl,
                    agent.known_fixes_dir, agent.correlation_window,
                    agent.metrics_port,
                ),
            )

            # --- Connectors ---
            connector_map: list[tuple[str, str, Any]] = [
                ("mqtt", "mqtt", config.ingestion.mqtt),
                ("ha_websocket", "ha-websocket", config.ingestion.ha_websocket),
                ("ha_log_poller", "ha-log-poller", config.ingestion.ha_log_poller),
                ("unifi", "unifi", config.ingestion.unifi),
                ("cloudflare", "cloudflare", config.ingestion.cloudflare),
                ("uptime_kuma", "uptime-kuma", config.ingestion.uptime_kuma),
            ]
            for type_name, default_name, sub_cfg in connector_map:
                if sub_cfg.enabled:
                    await self._create_row(
                        "connectors", type_name, default_name,
                        sub_cfg.model_dump(),
                    )
            # http_poller: multi-row
            for i, target in enumerate(config.ingestion.http_poller_targets):
                name = target.system or f"http-poller-{i}"
                await self._create_row(
                    "connectors", "http_poller", name,
                    target.model_dump(),
                )

            # --- Core services ---
            # LLM endpoints have no `enabled` flag — always import
            await self._create_row(
                "core_services", "llm_triage", "llm-triage",
                config.llm.triage.model_dump(),
            )
            await self._create_row(
                "core_services", "llm_reasoning", "llm-reasoning",
                config.llm.reasoning.model_dump(),
            )
            await self._create_row(
                "core_services", "llm_options", "llm-options",
                config.llm.options.model_dump(),
            )

            # Handlers + audit — only import if enabled
            service_map: list[tuple[str, str, Any]] = [
                ("ha_handler", "ha-handler", config.handlers.homeassistant),
                ("docker_handler", "docker-handler", config.handlers.docker),
                ("portainer_handler", "portainer-handler", config.handlers.portainer),
                ("proxmox_handler", "proxmox-handler", config.handlers.proxmox),
                ("unifi_handler", "unifi-handler", config.handlers.unifi),
                ("cloudflare_handler", "cloudflare-handler", config.handlers.cloudflare),
                ("influxdb", "influxdb", config.audit.influxdb),
            ]
            for type_name, default_name, sub_cfg in service_map:
                if sub_cfg.enabled:
                    await self._create_row(
                        "core_services", type_name, default_name,
                        sub_cfg.model_dump(),
                    )

            # Guardrails + circuit breaker
            gr = config.guardrails
            gr_dict = gr.model_dump(exclude={"circuit_breaker"})
            await self._create_row(
                "core_services", "guardrails", "guardrails", gr_dict,
            )
            await self._create_row(
                "core_services", "circuit_breaker", "circuit-breaker",
                gr.circuit_breaker.model_dump(),
            )

            # Scanner (if any check is enabled)
            sc = config.scanner
            if any([
                sc.certificate_expiry.enabled,
                sc.disk_space.enabled,
                sc.ha_health.enabled,
                sc.docker_health.enabled,
                sc.backup_freshness.enabled,
            ]):
                await self._create_row(
                    "core_services", "scanner", "scanner",
                    sc.model_dump(),
                )

            # --- Notifications ---
            notif_map: list[tuple[str, str, Any]] = [
                ("mqtt_notification", "mqtt-notify", config.notifications.mqtt),
                ("email", "email", config.notifications.email),
                ("webhook", "webhook", config.notifications.webhook),
                ("telegram", "telegram", config.notifications.telegram),
                ("discord", "discord", config.notifications.discord),
                ("slack", "slack", config.notifications.slack),
            ]
            for type_name, default_name, sub_cfg in notif_map:
                if sub_cfg.enabled:
                    await self._create_row(
                        "notification_channels", type_name, default_name,
                        sub_cfg.model_dump(),
                    )

        imported = await self._count_imported()
        logger.info("YAML config imported to SQLite: %s", imported)

    async def _count_imported(self) -> str:
        """Return a summary string of imported row counts."""
        counts = []
        for table in ("connectors", "core_services", "notification_channels"):
            cursor = await self._db.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            counts.append(f"{table}={row[0]}" if row else f"{table}=0")
        return ", ".join(counts)

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
        await self._commit()
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
        await self._commit()
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
        await self._commit()
        return await self._read_row(table, row_id)

    async def _delete_row(self, table: str, row_id: int) -> bool:
        """Delete a row by ID. Returns True if deleted, False if not found."""
        cursor = await self._db.execute(
            f"DELETE FROM {table} WHERE id = ?", (row_id,)
        )
        await self._commit()
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

    @staticmethod
    def _cfg(row: dict[str, Any]) -> dict[str, Any]:
        """Return a row's config dict with the DB ``enabled`` column injected.

        The DB ``enabled`` column is the source of truth — ``config_json``
        may not contain it (UI-created rows omit it). This method ensures
        Pydantic models that default ``enabled=False`` see the actual DB
        value rather than the model default.

        Only used for types whose Pydantic model has an ``enabled`` field.
        LLM/guardrails/circuit_breaker configs don't — call
        ``row["config"]`` directly for those.
        """
        cfg = dict(row["config"])
        cfg["enabled"] = bool(row["enabled"])
        return cfg

    async def _load_ingestion(self) -> IngestionConfig:
        rows = await self.list_connectors()
        logger.debug(
            "Connector rows: %s",
            [(r["type"], r["name"], r["enabled"]) for r in rows],
        )
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "mqtt" in by_type:
            kwargs["mqtt"] = self._cfg(by_type["mqtt"])
        if "ha_websocket" in by_type:
            kwargs["ha_websocket"] = self._cfg(by_type["ha_websocket"])
            logger.debug(
                "Loaded ha_websocket config from DB: url=%s enabled=%s",
                by_type["ha_websocket"]["config"].get("url", "<missing>"),
                by_type["ha_websocket"]["enabled"],
            )
        else:
            logger.debug("No ha_websocket row in connectors (types: %s)", list(by_type.keys()))
        if "ha_log_poller" in by_type:
            kwargs["ha_log_poller"] = self._cfg(by_type["ha_log_poller"])
            logger.debug(
                "Loaded ha_log_poller config from DB: url=%s enabled=%s",
                by_type["ha_log_poller"]["config"].get("url", "<missing>"),
                by_type["ha_log_poller"]["enabled"],
            )
        else:
            logger.debug("No ha_log_poller row in connectors (types: %s)", list(by_type.keys()))

        if "unifi" in by_type:
            kwargs["unifi"] = self._cfg(by_type["unifi"])
        if "cloudflare" in by_type:
            kwargs["cloudflare"] = self._cfg(by_type["cloudflare"])
        if "uptime_kuma" in by_type:
            kwargs["uptime_kuma"] = self._cfg(by_type["uptime_kuma"])

        # HTTP poller supports multiple targets (one row per target)
        poller_rows = [r for r in rows if r["type"] == "http_poller" and r["enabled"]]
        if poller_rows:
            kwargs["http_poller_targets"] = [r["config"] for r in poller_rows]

        result = IngestionConfig.model_validate(kwargs) if kwargs else IngestionConfig()
        logger.debug(
            "Final ingestion config: ha_ws_url=%s ha_ws_enabled=%s ha_lp_url=%s ha_lp_enabled=%s",
            result.ha_websocket.url, result.ha_websocket.enabled,
            result.ha_log_poller.url, result.ha_log_poller.enabled,
        )
        return result

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
            kwargs["homeassistant"] = self._cfg(by_type["ha_handler"])
        if "docker_handler" in by_type:
            kwargs["docker"] = self._cfg(by_type["docker_handler"])
        if "portainer_handler" in by_type:
            kwargs["portainer"] = self._cfg(by_type["portainer_handler"])
        if "proxmox_handler" in by_type:
            kwargs["proxmox"] = self._cfg(by_type["proxmox_handler"])
        if "unifi_handler" in by_type:
            kwargs["unifi"] = self._cfg(by_type["unifi_handler"])
        if "cloudflare_handler" in by_type:
            kwargs["cloudflare"] = self._cfg(by_type["cloudflare_handler"])

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
            kwargs["influxdb"] = self._cfg(by_type["influxdb"])

        return AuditConfig.model_validate(kwargs) if kwargs else AuditConfig()

    async def _load_notifications(self) -> NotificationsConfig:
        rows = await self.list_notifications()
        by_type = self._rows_by_type(rows)

        kwargs: dict[str, Any] = {}
        if "mqtt_notification" in by_type:
            kwargs["mqtt"] = self._cfg(by_type["mqtt_notification"])
        if "email" in by_type:
            kwargs["email"] = self._cfg(by_type["email"])
        if "webhook" in by_type:
            kwargs["webhook"] = self._cfg(by_type["webhook"])
        if "telegram" in by_type:
            kwargs["telegram"] = self._cfg(by_type["telegram"])
        if "discord" in by_type:
            kwargs["discord"] = self._cfg(by_type["discord"])
        if "slack" in by_type:
            kwargs["slack"] = self._cfg(by_type["slack"])

        return NotificationsConfig.model_validate(kwargs) if kwargs else NotificationsConfig()

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    async def has_admin(self) -> bool:
        """Return True if at least one admin user exists.

        Uses a one-way cache: once an admin is found, the result is
        cached permanently (admin existence never reverts).
        """
        if self._has_admin_cache:
            return True
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        )
        row = await cursor.fetchone()
        result = bool(row and row[0] > 0)
        if result:
            self._has_admin_cache = True
        return result

    async def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        role: str = "viewer",
        totp_secret: str | None = None,
        totp_confirmed: bool = False,
        backup_codes_hash: list[str] | None = None,
    ) -> int:
        """Create a user row. Returns the new user ID.

        The caller is responsible for hashing the password before calling
        this method — the store does not handle plaintext passwords.

        Raises:
            sqlite3.IntegrityError: If the username already exists.
            ValueError: If the role is invalid.
        """
        from oasisagent.ui.auth import VALID_ROLES

        if role not in VALID_ROLES:
            msg = f"Invalid role: {role}. Must be one of {VALID_ROLES}"
            raise ValueError(msg)

        encrypted_totp = (
            self._crypto.encrypt({"totp_secret": totp_secret})
            if totp_secret
            else None
        )
        backup_json = (
            json.dumps(backup_codes_hash)
            if backup_codes_hash
            else None
        )
        cursor = await self._db.execute(
            "INSERT INTO users (username, password_hash, role, totp_secret, "
            "totp_confirmed, backup_codes_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (
                username,
                password_hash,
                role,
                encrypted_totp,
                int(totp_confirmed),
                backup_json,
            ),
        )
        await self._commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Look up a user by username. Returns None if not found."""
        cursor = await self._db.execute(
            "SELECT id, username, password_hash, role, jwt_generation, "
            "totp_secret, totp_confirmed, backup_codes_hash, "
            "failed_attempts, locked_until, created_at "
            "FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "password_hash": row["password_hash"],
            "role": row["role"],
            "jwt_generation": row["jwt_generation"],
            "totp_secret": row["totp_secret"],
            "totp_confirmed": bool(row["totp_confirmed"]),
            "backup_codes_hash": (
                json.loads(row["backup_codes_hash"])
                if row["backup_codes_hash"]
                else []
            ),
            "failed_attempts": row["failed_attempts"],
            "locked_until": row["locked_until"],
            "created_at": row["created_at"],
        }

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Look up a user by ID. Returns None if not found."""
        cursor = await self._db.execute(
            "SELECT id, username, password_hash, role, jwt_generation, "
            "totp_secret, totp_confirmed, backup_codes_hash, "
            "failed_attempts, locked_until, created_at "
            "FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "password_hash": row["password_hash"],
            "role": row["role"],
            "jwt_generation": row["jwt_generation"],
            "totp_secret": row["totp_secret"],
            "totp_confirmed": bool(row["totp_confirmed"]),
            "backup_codes_hash": (
                json.loads(row["backup_codes_hash"])
                if row["backup_codes_hash"]
                else []
            ),
            "failed_attempts": row["failed_attempts"],
            "locked_until": row["locked_until"],
            "created_at": row["created_at"],
        }

    async def list_users(self) -> list[dict[str, Any]]:
        """List all users (without password hashes or TOTP secrets)."""
        cursor = await self._db.execute(
            "SELECT id, username, role, totp_confirmed, created_at FROM users"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
                "totp_confirmed": bool(row["totp_confirmed"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def update_user(
        self, user_id: int, updates: dict[str, Any],
    ) -> bool:
        """Update user fields. Returns True if the user was found and updated.

        Supported fields: username, password_hash, role, jwt_generation,
        totp_secret, totp_confirmed, backup_codes_hash, failed_attempts,
        locked_until.
        """
        valid_fields = {
            "username", "password_hash", "role", "jwt_generation",
            "totp_secret", "totp_confirmed", "backup_codes_hash",
            "failed_attempts", "locked_until",
        }
        if bad := set(updates) - valid_fields:
            msg = f"Unknown user fields: {bad}"
            raise ValueError(msg)

        if not updates:
            return True

        # Encrypt TOTP secret if being updated
        if "totp_secret" in updates:
            secret = updates["totp_secret"]
            updates["totp_secret"] = (
                self._crypto.encrypt({"totp_secret": secret})
                if secret
                else None
            )

        # Serialize backup codes hash list
        if "backup_codes_hash" in updates:
            codes = updates["backup_codes_hash"]
            updates["backup_codes_hash"] = json.dumps(codes) if codes else None

        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(user_id)

        cursor = await self._db.execute(
            f"UPDATE users SET {sets}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await self._commit()
        return cursor.rowcount > 0

    async def delete_user(self, user_id: int) -> bool:
        """Delete a user by ID. Refuses to delete the last admin.

        Returns True if deleted, False if not found.

        Raises:
            ValueError: If deleting would remove the last admin.
        """
        # Check if this is the last admin
        user = await self.get_user_by_id(user_id)
        if not user:
            return False

        if user["role"] == "admin":
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            )
            row = await cursor.fetchone()
            if row and row[0] <= 1:
                msg = "Cannot delete the last admin user"
                raise ValueError(msg)

        cursor = await self._db.execute(
            "DELETE FROM users WHERE id = ?", (user_id,)
        )
        await self._commit()
        return cursor.rowcount > 0

    async def get_user_totp_secret(self, user_id: int) -> str | None:
        """Decrypt and return the TOTP secret for a user."""
        cursor = await self._db.execute(
            "SELECT totp_secret FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row or not row["totp_secret"]:
            return None
        decrypted = self._crypto.decrypt(row["totp_secret"])
        return decrypted.get("totp_secret")

    async def increment_failed_attempts(self, user_id: int) -> int:
        """Increment failed login attempts and return the new count."""
        await self._db.execute(
            "UPDATE users SET failed_attempts = failed_attempts + 1, "
            "updated_at = datetime('now') WHERE id = ?",
            (user_id,),
        )
        await self._commit()
        cursor = await self._db.execute(
            "SELECT failed_attempts FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["failed_attempts"] if row else 0

    async def reset_failed_attempts(self, user_id: int) -> None:
        """Reset failed login attempts and clear lockout."""
        await self._db.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (user_id,),
        )
        await self._commit()

    async def set_lockout(self, user_id: int, locked_until: str) -> None:
        """Set the account lockout timestamp."""
        await self._db.execute(
            "UPDATE users SET locked_until = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (locked_until, user_id),
        )
        await self._commit()
