"""Tests for the SQLite config store."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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
