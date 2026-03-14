"""Tests for the SQLite config store."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from oasisagent.config import OasisAgentConfig

if TYPE_CHECKING:
    import aiosqlite

    from oasisagent.db.config_store import ConfigStore


class TestVirginDetection:
    async def test_fresh_db_is_virgin(self, store: ConfigStore) -> None:
        assert await store.is_virgin() is True

    async def test_not_virgin_after_connector_created(self, store: ConfigStore) -> None:
        await store.create_connector(
            "mqtt", "mqtt-primary",
            {"broker": "mqtt://localhost:1883"},
        )
        assert await store.is_virgin() is False

    async def test_not_virgin_after_service_created(self, store: ConfigStore) -> None:
        await store.create_service(
            "llm_options", "llm-opts",
            {"cost_tracking": True},
        )
        assert await store.is_virgin() is False

    async def test_not_virgin_after_notification_created(self, store: ConfigStore) -> None:
        await store.create_notification(
            "webhook", "webhook-primary",
            {"urls": ["https://example.com/hook"]},
        )
        assert await store.is_virgin() is False


class TestLoadConfigDefaults:
    async def test_empty_db_returns_valid_config(self, store: ConfigStore) -> None:
        config = await store.load_config()
        assert isinstance(config, OasisAgentConfig)
        assert config.agent.name == "oasis-agent"

    async def test_empty_db_has_default_ingestion(self, store: ConfigStore) -> None:
        config = await store.load_config()
        assert config.ingestion.mqtt.broker == "mqtt://localhost:1883"


class TestConnectorCRUD:
    async def test_create_and_read(self, store: ConfigStore) -> None:
        row_id = await store.create_connector(
            "mqtt", "mqtt-primary",
            {"broker": "mqtt://test:1883", "password": "secret"},
        )
        row = await store.get_connector(row_id)
        assert row is not None
        assert row["type"] == "mqtt"
        assert row["name"] == "mqtt-primary"
        assert row["config"]["broker"] == "mqtt://test:1883"
        assert row["config"]["password"] == "secret"  # decrypted in-memory

    async def test_list_connectors(self, store: ConfigStore) -> None:
        await store.create_connector("mqtt", "mqtt-1", {"broker": "mqtt://a:1883"})
        await store.create_connector("ha_websocket", "ha-ws", {"url": "ws://b:8123/api/websocket"})
        rows = await store.list_connectors()
        assert len(rows) == 2

    async def test_update_connector(self, store: ConfigStore) -> None:
        row_id = await store.create_connector(
            "mqtt", "mqtt-primary",
            {"broker": "mqtt://old:1883", "password": "old_pass"},
        )
        updated = await store.update_connector(row_id, {
            "config": {"broker": "mqtt://new:1883", "password": "new_pass"},
        })
        assert updated is not None
        assert updated["config"]["broker"] == "mqtt://new:1883"
        assert updated["config"]["password"] == "new_pass"

    async def test_delete_connector(self, store: ConfigStore) -> None:
        row_id = await store.create_connector("mqtt", "mqtt-del", {"broker": "mqtt://x:1883"})
        assert await store.delete_connector(row_id) is True
        assert await store.get_connector(row_id) is None

    async def test_delete_nonexistent_returns_false(self, store: ConfigStore) -> None:
        assert await store.delete_connector(999) is False

    async def test_get_nonexistent_returns_none(self, store: ConfigStore) -> None:
        assert await store.get_connector(999) is None

    async def test_invalid_type_raises_value_error(self, store: ConfigStore) -> None:
        with pytest.raises(ValueError, match="Unknown connectors type"):
            await store.create_connector("nonexistent", "x", {})

    async def test_invalid_config_raises_validation_error(self, store: ConfigStore) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            await store.create_connector("mqtt", "bad", {"qos": 999})


class TestSecretEncryption:
    async def test_secrets_encrypted_in_db(
        self, store: ConfigStore, db: aiosqlite.Connection,
    ) -> None:
        await store.create_connector(
            "mqtt", "mqtt-enc",
            {"broker": "mqtt://localhost:1883", "password": "my_secret_pass"},
        )
        cursor = await db.execute("SELECT secrets_json FROM connectors WHERE name = 'mqtt-enc'")
        row = await cursor.fetchone()
        # secrets_json is encrypted, not plaintext
        assert row[0] != ""
        assert "my_secret_pass" not in row[0]

    async def test_secrets_not_in_config_json(
        self, store: ConfigStore, db: aiosqlite.Connection,
    ) -> None:
        await store.create_connector(
            "mqtt", "mqtt-split",
            {"broker": "mqtt://localhost:1883", "password": "should_not_appear"},
        )
        cursor = await db.execute("SELECT config_json FROM connectors WHERE name = 'mqtt-split'")
        row = await cursor.fetchone()
        config = json.loads(row[0])
        assert "password" not in config

    async def test_secrets_win_on_merge_conflict(self, store: ConfigStore) -> None:
        """If a secret field somehow appears in config_json, secrets_json wins."""
        from oasisagent.db.registry import merge_secrets

        config = {"broker": "mqtt://localhost:1883", "password": "plaintext_bad"}
        secrets = {"password": "encrypted_good"}
        merged = merge_secrets(config, secrets)
        assert merged["password"] == "encrypted_good"


class TestMaskRow:
    async def test_mask_hides_secrets(self, store: ConfigStore) -> None:
        row_id = await store.create_connector(
            "mqtt", "mqtt-mask",
            {"broker": "mqtt://localhost:1883", "password": "hidden"},
        )
        row = await store.get_connector(row_id)
        masked = store.mask_row("connectors", row)
        assert masked["config"]["password"] == "********"
        assert masked["config"]["broker"] == "mqtt://localhost:1883"

    async def test_mask_preserves_empty_secrets(self, store: ConfigStore) -> None:
        row_id = await store.create_connector(
            "mqtt", "mqtt-empty",
            {"broker": "mqtt://localhost:1883", "password": ""},
        )
        row = await store.get_connector(row_id)
        masked = store.mask_row("connectors", row)
        # Empty string is not masked (nothing to hide)
        assert masked["config"]["password"] == ""


class TestLoadConfigWithData:
    async def test_mqtt_connector_appears_in_ingestion(self, store: ConfigStore) -> None:
        await store.create_connector(
            "mqtt", "mqtt-primary",
            {"broker": "mqtt://myhost:1883", "password": "p", "qos": 2},
        )
        config = await store.load_config()
        assert config.ingestion.mqtt.broker == "mqtt://myhost:1883"
        assert config.ingestion.mqtt.password == "p"
        assert config.ingestion.mqtt.qos == 2

    async def test_ha_websocket_connector(self, store: ConfigStore) -> None:
        await store.create_connector(
            "ha_websocket", "ha-ws",
            {"url": "ws://ha.local:8123/api/websocket", "token": "tok123"},
        )
        config = await store.load_config()
        assert config.ingestion.ha_websocket.url == "ws://ha.local:8123/api/websocket"
        assert config.ingestion.ha_websocket.token == "tok123"

    async def test_llm_endpoints(self, store: ConfigStore) -> None:
        await store.create_service(
            "llm_triage", "triage-ollama",
            {"base_url": "http://ollama:11434/v1", "model": "qwen2.5:7b", "api_key": "none"},
        )
        await store.create_service(
            "llm_reasoning", "reasoning-cloud",
            {
                "base_url": "https://openrouter.ai/api/v1",
                "model": "claude-sonnet",
                "api_key": "sk-x",
            },
        )
        config = await store.load_config()
        assert config.llm.triage.base_url == "http://ollama:11434/v1"
        assert config.llm.reasoning.api_key == "sk-x"

    async def test_handler_config(self, store: ConfigStore) -> None:
        await store.create_service(
            "ha_handler", "ha-primary",
            {"url": "http://ha.local:8123", "token": "ha-tok"},
        )
        config = await store.load_config()
        assert config.handlers.homeassistant.url == "http://ha.local:8123"
        assert config.handlers.homeassistant.token == "ha-tok"

    async def test_notification_config(self, store: ConfigStore) -> None:
        await store.create_notification(
            "email", "email-primary",
            {"enabled": True, "smtp_host": "smtp.example.com", "username": "u", "password": "p"},
        )
        config = await store.load_config()
        assert config.notifications.email.smtp_host == "smtp.example.com"
        assert config.notifications.email.password == "p"

    async def test_disabled_rows_use_defaults(self, store: ConfigStore) -> None:
        await store.create_connector(
            "mqtt", "mqtt-disabled",
            {"broker": "mqtt://disabled:1883"},
            enabled=False,
        )
        config = await store.load_config()
        # Disabled connector is ignored; defaults used instead
        assert config.ingestion.mqtt.broker == "mqtt://localhost:1883"

    async def test_influxdb_audit(self, store: ConfigStore) -> None:
        await store.create_service(
            "influxdb", "influx-primary",
            {"url": "http://influx:8086", "token": "influx-tok", "org": "myorg", "bucket": "oasis"},
        )
        config = await store.load_config()
        assert config.audit.influxdb.url == "http://influx:8086"
        assert config.audit.influxdb.token == "influx-tok"


class TestLoadConfigNewTypes:
    """Test that v0.3.3+ types are loaded from SQLite into config."""

    async def test_unifi_connector(self, store: ConfigStore) -> None:
        await store.create_connector(
            "unifi", "oasis-udm",
            {"enabled": True, "url": "https://192.168.1.1", "username": "admin", "password": "pw"},
        )
        config = await store.load_config()
        assert config.ingestion.unifi.url == "https://192.168.1.1"
        assert config.ingestion.unifi.enabled is True

    async def test_cloudflare_connector(self, store: ConfigStore) -> None:
        await store.create_connector(
            "cloudflare", "cf",
            {"enabled": True, "api_token": "tok123"},
        )
        config = await store.load_config()
        assert config.ingestion.cloudflare.api_token == "tok123"
        assert config.ingestion.cloudflare.enabled is True

    async def test_uptime_kuma_connector(self, store: ConfigStore) -> None:
        await store.create_connector(
            "uptime_kuma", "kuma",
            {"enabled": True, "url": "http://kuma:3001", "api_key": "key123"},
        )
        config = await store.load_config()
        assert config.ingestion.uptime_kuma.url == "http://kuma:3001"
        assert config.ingestion.uptime_kuma.enabled is True

    async def test_portainer_handler(self, store: ConfigStore) -> None:
        await store.create_service(
            "portainer_handler", "portainer",
            {"url": "https://portainer:9443", "api_key": "ptk"},
        )
        config = await store.load_config()
        assert config.handlers.portainer.url == "https://portainer:9443"
        assert config.handlers.portainer.api_key == "ptk"

    async def test_unifi_handler(self, store: ConfigStore) -> None:
        await store.create_service(
            "unifi_handler", "unifi",
            {"url": "https://192.168.1.1", "username": "admin", "password": "pw"},
        )
        config = await store.load_config()
        assert config.handlers.unifi.url == "https://192.168.1.1"
        assert config.handlers.unifi.password == "pw"

    async def test_cloudflare_handler(self, store: ConfigStore) -> None:
        await store.create_service(
            "cloudflare_handler", "cf-handler",
            {"api_token": "cf-tok"},
        )
        config = await store.load_config()
        assert config.handlers.cloudflare.api_token == "cf-tok"

    async def test_telegram_notification(self, store: ConfigStore) -> None:
        await store.create_notification(
            "telegram", "tg",
            {"enabled": True, "bot_token": "bot123", "chat_id": "456"},
        )
        config = await store.load_config()
        assert config.notifications.telegram.bot_token == "bot123"
        assert config.notifications.telegram.chat_id == "456"


class TestEnabledFlagInjection:
    """Regression: DB ``enabled`` column must be injected into config dict (#151).

    UI-created rows store ``enabled`` as a DB column, not in ``config_json``.
    Components whose Pydantic models default ``enabled=False`` would never
    start without the injection.
    """

    async def test_connector_without_enabled_in_config_json(
        self, store: ConfigStore,
    ) -> None:
        """UniFi connector created without 'enabled' in config_json loads as enabled."""
        await store.create_connector(
            "unifi", "oasis-udm",
            {"url": "https://192.168.1.1", "username": "admin", "password": "pw"},
        )
        config = await store.load_config()
        assert config.ingestion.unifi.enabled is True
        assert config.ingestion.unifi.url == "https://192.168.1.1"

    async def test_handler_without_enabled_in_config_json(
        self, store: ConfigStore,
    ) -> None:
        """Portainer handler created without 'enabled' in config_json loads as enabled."""
        await store.create_service(
            "portainer_handler", "portainer",
            {"url": "https://portainer:9443", "api_key": "ptk"},
        )
        config = await store.load_config()
        assert config.handlers.portainer.enabled is True

    async def test_cloudflare_connector_without_enabled(
        self, store: ConfigStore,
    ) -> None:
        await store.create_connector(
            "cloudflare", "cf",
            {"api_token": "tok123"},
        )
        config = await store.load_config()
        assert config.ingestion.cloudflare.enabled is True

    async def test_uptime_kuma_without_enabled(
        self, store: ConfigStore,
    ) -> None:
        await store.create_connector(
            "uptime_kuma", "kuma",
            {"url": "http://kuma:3001", "api_key": "key123"},
        )
        config = await store.load_config()
        assert config.ingestion.uptime_kuma.enabled is True

    async def test_unifi_handler_without_enabled_in_config_json(
        self, store: ConfigStore,
    ) -> None:
        """UniFi handler created without 'enabled' in config_json loads as enabled."""
        await store.create_service(
            "unifi_handler", "unifi-handler",
            {
                "url": "https://192.168.1.1",
                "username": "admin",
                "password": "secret",
            },
        )
        config = await store.load_config()
        assert config.handlers.unifi.enabled is True
        assert config.handlers.unifi.url == "https://192.168.1.1"

    async def test_cloudflare_handler_without_enabled_in_config_json(
        self, store: ConfigStore,
    ) -> None:
        """Cloudflare handler created without 'enabled' in config_json loads as enabled."""
        await store.create_service(
            "cloudflare_handler", "cloudflare-handler",
            {"api_token": "cf-tok-123"},
        )
        config = await store.load_config()
        assert config.handlers.cloudflare.enabled is True
        assert config.handlers.cloudflare.api_token == "cf-tok-123"


class TestAgentConfig:
    async def test_default_agent_config(self, store: ConfigStore) -> None:
        config = await store.load_config()
        assert config.agent.name == "oasis-agent"
        assert config.agent.event_queue_size == 1000

    async def test_update_agent_config(self, store: ConfigStore) -> None:
        updated = await store.update_agent_config({"name": "my-agent", "event_queue_size": 500})
        assert updated.name == "my-agent"
        assert updated.event_queue_size == 500

        config = await store.load_config()
        assert config.agent.name == "my-agent"

    async def test_update_agent_config_rejects_unknown_fields(self, store: ConfigStore) -> None:
        with pytest.raises(ValueError, match="Unknown fields"):
            await store.update_agent_config({"bogus_field": "nope"})


class TestImportYaml:
    """Tests for YAML → SQLite import on virgin databases."""

    def _make_config(self, **overrides: Any) -> OasisAgentConfig:
        """Build a minimal config with some enabled components."""
        from oasisagent.config import (
            AuditConfig,
            HandlersConfig,
            InfluxDbConfig,
            IngestionConfig,
            MqttIngestionConfig,
            MqttNotificationConfig,
            NotificationsConfig,
        )

        return OasisAgentConfig(
            ingestion=IngestionConfig(
                mqtt=MqttIngestionConfig(
                    enabled=True,
                    broker="mqtt://test.local:1883",
                    password="mqtt-secret",
                ),
            ),
            audit=AuditConfig(
                influxdb=InfluxDbConfig(
                    enabled=True,
                    url="http://influx.local:8086",
                    token="influx-token",
                ),
            ),
            handlers=HandlersConfig(),
            notifications=NotificationsConfig(
                mqtt=MqttNotificationConfig(
                    enabled=True,
                    broker="mqtt://test.local:1883",
                    password="notif-secret",
                ),
            ),
            **overrides,
        )

    async def test_import_creates_connector_rows(
        self, store: ConfigStore,
    ) -> None:
        config = self._make_config()
        await store.import_yaml(config)

        rows = await store.list_connectors()
        type_names = {r["type"] for r in rows}
        # mqtt, ha_websocket, ha_log_poller all default to enabled=True
        assert "mqtt" in type_names
        assert "ha_websocket" in type_names
        assert "ha_log_poller" in type_names

        mqtt_row = next(r for r in rows if r["type"] == "mqtt")
        assert mqtt_row["config"]["broker"] == "mqtt://test.local:1883"

    async def test_import_creates_notification_rows(
        self, store: ConfigStore,
    ) -> None:
        config = self._make_config()
        await store.import_yaml(config)

        rows = await store.list_notifications()
        assert len(rows) == 1
        assert rows[0]["type"] == "mqtt_notification"
        assert rows[0]["config"]["broker"] == "mqtt://test.local:1883"

    async def test_import_creates_service_rows(
        self, store: ConfigStore,
    ) -> None:
        config = self._make_config()
        await store.import_yaml(config)

        rows = await store.list_services()
        type_names = {r["type"] for r in rows}
        # influxdb + llm_options + guardrails + circuit_breaker (always imported)
        assert "influxdb" in type_names
        assert "llm_options" in type_names
        assert "guardrails" in type_names
        assert "circuit_breaker" in type_names

    async def test_import_secrets_are_encrypted(
        self, store: ConfigStore, db: aiosqlite.Connection,
    ) -> None:
        config = self._make_config()
        await store.import_yaml(config)

        # Raw DB row should NOT contain plaintext password
        cursor = await db.execute(
            "SELECT config_json FROM notification_channels WHERE type = 'mqtt_notification'"
        )
        row = await cursor.fetchone()
        raw = json.loads(row["config_json"])
        assert "password" not in raw

        # But decrypted read should have it
        rows = await store.list_notifications()
        assert rows[0]["config"]["password"] == "notif-secret"

    async def test_import_makes_db_non_virgin(
        self, store: ConfigStore,
    ) -> None:
        config = self._make_config()
        assert await store.is_virgin() is True
        await store.import_yaml(config)
        assert await store.is_virgin() is False

    async def test_roundtrip_yaml_to_sqlite_to_config(
        self, store: ConfigStore,
    ) -> None:
        """Import YAML, then load from SQLite — should produce equivalent config."""
        original = self._make_config()
        await store.import_yaml(original)

        loaded = await store.load_config()
        # MQTT connector
        assert loaded.ingestion.mqtt.enabled is True
        assert loaded.ingestion.mqtt.broker == "mqtt://test.local:1883"
        assert loaded.ingestion.mqtt.password == "mqtt-secret"
        # MQTT notification
        assert loaded.notifications.mqtt.enabled is True
        assert loaded.notifications.mqtt.broker == "mqtt://test.local:1883"
        assert loaded.notifications.mqtt.password == "notif-secret"
        # InfluxDB
        assert loaded.audit.influxdb.enabled is True
        assert loaded.audit.influxdb.token == "influx-token"

    async def test_disabled_components_not_imported(
        self, store: ConfigStore,
    ) -> None:
        """Components with enabled=False should not create DB rows."""
        config = self._make_config()
        await store.import_yaml(config)

        rows = await store.list_services()
        type_names = {r["type"] for r in rows}
        # UniFi handler defaults to enabled=False
        assert "unifi_handler" not in type_names

        conn_rows = await store.list_connectors()
        conn_types = {r["type"] for r in conn_rows}
        # Cloudflare adapter defaults to enabled=False
        assert "cloudflare" not in conn_types
