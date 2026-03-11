"""Tests for the config import/export CLI commands."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from cryptography.fernet import Fernet

from oasisagent.db.config_store import ConfigStore
from oasisagent.db.crypto import CryptoProvider
from oasisagent.db.schema import run_migrations


@pytest.fixture
async def _db_env(tmp_path: Path) -> tuple[ConfigStore, Path]:
    """Set up a temp database with some config data and return store + data_dir."""
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    db_path = tmp_path / "oasisagent.db"
    db = await run_migrations(db_path)
    store = ConfigStore(db, crypto)

    # Seed some data
    await store.create_connector(
        "mqtt", "mqtt-primary",
        {"broker": "mqtt://myhost:1883", "password": "mqtt_secret", "qos": 1},
    )
    await store.create_service(
        "llm_triage", "triage-ollama",
        {"base_url": "http://ollama:11434/v1", "model": "qwen2.5:7b", "api_key": "key123"},
    )
    await store.create_notification(
        "webhook", "discord-hook",
        {"urls": ["https://discord.com/api/webhooks/123"]},
    )
    await store.update_agent_config({"name": "test-agent"})

    # Write key file so resolve_secret_key can find it
    key_path = tmp_path / ".secret_key"
    key_path.write_text(key)
    key_path.chmod(0o600)

    yield store, tmp_path
    await db.close()


@pytest.fixture
def db_env(_db_env: tuple[ConfigStore, Path]) -> tuple[ConfigStore, Path]:
    """Unwrap the async fixture."""
    return _db_env


class TestExport:
    async def test_export_produces_valid_yaml(
        self, db_env: tuple[ConfigStore, Path], capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, data_dir = db_env
        from oasisagent.cli_config import _cmd_export

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(data_dir)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            exit_code = await _cmd_export(include_secrets=False)

        assert exit_code == 0
        output = capsys.readouterr().out
        data = yaml.safe_load(output)
        assert "schema_version" in data
        assert data["agent"]["name"] == "test-agent"
        assert len(data["connectors"]) == 1
        assert len(data["services"]) == 1
        assert len(data["notifications"]) == 1

    async def test_export_masks_secrets_by_default(
        self, db_env: tuple[ConfigStore, Path], capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, data_dir = db_env
        from oasisagent.cli_config import _cmd_export

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(data_dir)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            await _cmd_export(include_secrets=False)

        output = capsys.readouterr().out
        data = yaml.safe_load(output)
        mqtt_config = data["connectors"][0]["config"]
        assert mqtt_config["password"] == "<encrypted>"
        assert mqtt_config["broker"] == "mqtt://myhost:1883"

    async def test_export_includes_secrets_when_requested(
        self, db_env: tuple[ConfigStore, Path], capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, data_dir = db_env
        from oasisagent.cli_config import _cmd_export

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(data_dir)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            await _cmd_export(include_secrets=True)

        output = capsys.readouterr().out
        data = yaml.safe_load(output)
        mqtt_config = data["connectors"][0]["config"]
        assert mqtt_config["password"] == "mqtt_secret"

    async def test_export_empty_db(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Export on virgin DB produces valid YAML with empty lists."""
        key = Fernet.generate_key().decode()
        key_path = tmp_path / ".secret_key"
        key_path.write_text(key)
        key_path.chmod(0o600)

        from oasisagent.cli_config import _cmd_export

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(tmp_path)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            exit_code = await _cmd_export(include_secrets=False)

        assert exit_code == 0
        data = yaml.safe_load(capsys.readouterr().out)
        assert data["connectors"] == []
        assert data["services"] == []
        assert data["notifications"] == []


class TestImport:
    async def test_import_creates_rows(self, tmp_path: Path) -> None:
        """Import into virgin DB creates all rows."""
        key = Fernet.generate_key().decode()
        key_path = tmp_path / ".secret_key"
        key_path.write_text(key)
        key_path.chmod(0o600)

        import_data = {
            "schema_version": 1,
            "agent": {"name": "imported-agent", "event_queue_size": 500},
            "connectors": [
                {
                    "type": "mqtt",
                    "name": "mqtt-imported",
                    "enabled": True,
                    "config": {"broker": "mqtt://imported:1883", "password": "pass"},
                },
            ],
            "services": [
                {
                    "type": "llm_options",
                    "name": "llm-opts",
                    "enabled": True,
                    "config": {"cost_tracking": False},
                },
            ],
            "notifications": [],
        }
        import_file = tmp_path / "seed.yaml"
        import_file.write_text(yaml.dump(import_data))

        from oasisagent.cli_config import _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(tmp_path)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            exit_code = await _cmd_import(str(import_file))

        assert exit_code == 0

        # Verify data was written
        db = await run_migrations(tmp_path / "oasisagent.db")
        try:
            store = ConfigStore(db, CryptoProvider(key))
            config = await store.load_config()
            assert config.agent.name == "imported-agent"
            assert config.agent.event_queue_size == 500
            assert config.ingestion.mqtt.broker == "mqtt://imported:1883"
            assert config.ingestion.mqtt.password == "pass"
        finally:
            await db.close()

    async def test_import_updates_existing(
        self, db_env: tuple[ConfigStore, Path],
    ) -> None:
        """Import with matching names updates existing rows."""
        _, data_dir = db_env

        import_data = {
            "schema_version": 1,
            "connectors": [
                {
                    "type": "mqtt",
                    "name": "mqtt-primary",  # same name as existing
                    "enabled": True,
                    "config": {"broker": "mqtt://updated:1883"},
                },
            ],
        }
        import_file = data_dir / "update.yaml"
        import_file.write_text(yaml.dump(import_data))

        from oasisagent.cli_config import _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(data_dir)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            exit_code = await _cmd_import(str(import_file))

        assert exit_code == 0

        # Re-read from fresh connection
        db = await run_migrations(data_dir / "oasisagent.db")
        try:
            key = (data_dir / ".secret_key").read_text().strip()
            new_store = ConfigStore(db, CryptoProvider(key))
            connectors = await new_store.list_connectors()
            assert len(connectors) == 1
            assert connectors[0]["config"]["broker"] == "mqtt://updated:1883"
            # Password preserved from original (not in import)
            assert connectors[0]["config"]["password"] == "mqtt_secret"
        finally:
            await db.close()

    async def test_import_strips_encrypted_placeholders(
        self, db_env: tuple[ConfigStore, Path],
    ) -> None:
        """Import with <encrypted> placeholders doesn't overwrite real secrets."""
        _, data_dir = db_env

        import_data = {
            "schema_version": 1,
            "connectors": [
                {
                    "type": "mqtt",
                    "name": "mqtt-primary",
                    "enabled": True,
                    "config": {
                        "broker": "mqtt://updated:1883",
                        "password": "<encrypted>",  # should be stripped
                    },
                },
            ],
        }
        import_file = data_dir / "masked.yaml"
        import_file.write_text(yaml.dump(import_data))

        from oasisagent.cli_config import _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(data_dir)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            await _cmd_import(str(import_file))

        db = await run_migrations(data_dir / "oasisagent.db")
        try:
            key = (data_dir / ".secret_key").read_text().strip()
            new_store = ConfigStore(db, CryptoProvider(key))
            connectors = await new_store.list_connectors()
            # Original secret preserved — placeholder was stripped
            assert connectors[0]["config"]["password"] == "mqtt_secret"
        finally:
            await db.close()

    async def test_import_rejects_future_schema(self, tmp_path: Path) -> None:
        """Import fails if file schema_version > database version."""
        key = Fernet.generate_key().decode()
        key_path = tmp_path / ".secret_key"
        key_path.write_text(key)
        key_path.chmod(0o600)

        import_data = {"schema_version": 999}
        import_file = tmp_path / "future.yaml"
        import_file.write_text(yaml.dump(import_data))

        from oasisagent.cli_config import _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(tmp_path)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            exit_code = await _cmd_import(str(import_file))

        assert exit_code == 1

    async def test_import_rejects_invalid_type(self, tmp_path: Path) -> None:
        key = Fernet.generate_key().decode()
        (tmp_path / ".secret_key").write_text(key)

        import_data = {
            "connectors": [{"type": "nonexistent", "name": "x", "config": {}}],
        }
        import_file = tmp_path / "bad.yaml"
        import_file.write_text(yaml.dump(import_data))

        from oasisagent.cli_config import _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(tmp_path)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            exit_code = await _cmd_import(str(import_file))

        assert exit_code == 1

    async def test_import_missing_file(self, tmp_path: Path) -> None:
        from oasisagent.cli_config import _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(tmp_path)}):
            exit_code = await _cmd_import("/nonexistent/path.yaml")

        assert exit_code == 1

    async def test_import_invalid_yaml(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(": : : not yaml [[[")

        from oasisagent.cli_config import _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(tmp_path)}):
            exit_code = await _cmd_import(str(bad_file))

        assert exit_code == 1


class TestRoundTrip:
    async def test_export_then_import_preserves_config(self, tmp_path: Path) -> None:
        """Full round-trip: seed → export → fresh DB → import → verify."""
        key = Fernet.generate_key().decode()
        key_path = tmp_path / ".secret_key"
        key_path.write_text(key)
        key_path.chmod(0o600)

        # Seed original DB
        db1_path = tmp_path / "oasisagent.db"
        db1 = await run_migrations(db1_path)
        store1 = ConfigStore(db1, CryptoProvider(key))
        await store1.create_connector(
            "mqtt", "mqtt-rt",
            {"broker": "mqtt://roundtrip:1883", "password": "rt_pass"},
        )
        await store1.update_agent_config({"name": "roundtrip-agent"})
        await db1.close()

        # Export with secrets
        import io
        import sys

        from oasisagent.cli_config import _cmd_export, _cmd_import

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(tmp_path)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            # Capture stdout
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            await _cmd_export(include_secrets=True)
            yaml_content = sys.stdout.getvalue()
            sys.stdout = old_stdout

        # Write to file for import
        export_file = tmp_path / "exported.yaml"
        export_file.write_text(yaml_content)

        # Fresh DB in a new dir
        new_dir = tmp_path / "new_instance"
        new_dir.mkdir()
        new_key = Fernet.generate_key().decode()
        new_key_path = new_dir / ".secret_key"
        new_key_path.write_text(new_key)
        new_key_path.chmod(0o600)

        with patch.dict(os.environ, {"OASIS_DATA_DIR": str(new_dir)}):
            os.environ.pop("OASIS_SECRET_KEY", None)
            exit_code = await _cmd_import(str(export_file))

        assert exit_code == 0

        # Verify
        db2 = await run_migrations(new_dir / "oasisagent.db")
        try:
            store2 = ConfigStore(db2, CryptoProvider(new_key))
            config = await store2.load_config()
            assert config.agent.name == "roundtrip-agent"
            assert config.ingestion.mqtt.broker == "mqtt://roundtrip:1883"
            assert config.ingestion.mqtt.password == "rt_pass"
        finally:
            await db2.close()


class TestMainIntegration:
    def test_config_command_dispatch(self) -> None:
        """Verify 'oasisagent config' is recognized by the parser."""
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["config", "export"])
        assert args.command == "config"
        assert args.config_command == "export"
        assert args.include_secrets is False

    def test_config_import_parser(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["config", "import", "seed.yaml"])
        assert args.command == "config"
        assert args.config_command == "import"
        assert args.file == "seed.yaml"

    def test_config_export_include_secrets(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["config", "export", "--include-secrets"])
        assert args.include_secrets is True
