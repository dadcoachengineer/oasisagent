"""Tests for the learning loop — candidate fix writer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from oasisagent.engine.learning import (
    CandidateFixWriter,
    _candidate_to_yaml,
    _match_hash,
    _validate_candidate,
)

# ---------------------------------------------------------------------------
# Helper: mock DB
# ---------------------------------------------------------------------------


def _mock_db(existing_row: tuple | None = None) -> AsyncMock:
    """Create a mock aiosqlite connection."""
    db = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=existing_row)
    cursor.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor)
    db.commit = AsyncMock()
    return db


def _valid_candidate(
    system: str = "homeassistant",
    event_type: str = "integration_failure",
    **overrides: object,
) -> dict:
    base = {
        "match": {"system": system, "event_type": event_type},
        "diagnosis": "ZWave crash",
        "action": {
            "handler": "homeassistant",
            "operation": "restart_integration",
            "params": {"integration": "zwave_js"},
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Match hash
# ---------------------------------------------------------------------------


class TestMatchHash:
    def test_deterministic(self) -> None:
        h1 = _match_hash("ha", "integration_failure")
        h2 = _match_hash("ha", "integration_failure")
        assert h1 == h2

    def test_different_input_different_hash(self) -> None:
        h1 = _match_hash("ha", "integration_failure")
        h2 = _match_hash("ha", "device_offline")
        assert h1 != h2

    def test_entity_pattern_included(self) -> None:
        h1 = _match_hash("ha", "event", "")
        h2 = _match_hash("ha", "event", "zwave_*")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_candidate(self) -> None:
        assert _validate_candidate(_valid_candidate()) is True

    def test_missing_match(self) -> None:
        assert _validate_candidate({"action": {"handler": "h", "operation": "o"}}) is False

    def test_missing_system(self) -> None:
        c = _valid_candidate()
        c["match"]["system"] = ""
        assert _validate_candidate(c) is False

    def test_missing_action(self) -> None:
        c = {"match": {"system": "ha", "event_type": "fail"}}
        assert _validate_candidate(c) is False

    def test_missing_handler(self) -> None:
        c = _valid_candidate()
        c["action"]["handler"] = ""
        assert _validate_candidate(c) is False


# ---------------------------------------------------------------------------
# YAML generation
# ---------------------------------------------------------------------------


class TestCandidateYaml:
    def test_generates_valid_yaml(self) -> None:
        import yaml

        candidate = _valid_candidate()
        result = _candidate_to_yaml(candidate)
        data = yaml.safe_load(result)

        assert "fixes" in data
        fix = data["fixes"][0]
        assert fix["match"]["system"] == "homeassistant"
        assert fix["match"]["event_type"] == "integration_failure"
        assert fix["risk_tier"] == "recommend"

    def test_risk_tier_always_recommend(self) -> None:
        import yaml

        candidate = _valid_candidate()
        result = _candidate_to_yaml(candidate)
        data = yaml.safe_load(result)
        assert data["fixes"][0]["risk_tier"] == "recommend"


# ---------------------------------------------------------------------------
# CandidateFixWriter
# ---------------------------------------------------------------------------


class TestCandidateFixWriter:
    @pytest.mark.asyncio
    async def test_below_confidence_rejected(self, tmp_path: Path) -> None:
        db = _mock_db()
        writer = CandidateFixWriter(db, tmp_path / "candidates", min_confidence=0.8)

        result = await writer.write_candidate(_valid_candidate(), confidence=0.5)

        assert result is False
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_candidate_rejected(self, tmp_path: Path) -> None:
        db = _mock_db()
        writer = CandidateFixWriter(db, tmp_path / "candidates", min_confidence=0.5)

        result = await writer.write_candidate(
            {"match": {}, "action": {}}, confidence=0.9,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_new_candidate_writes_yaml(self, tmp_path: Path) -> None:
        db = _mock_db(existing_row=None)
        candidates_dir = tmp_path / "candidates"
        writer = CandidateFixWriter(db, candidates_dir, min_confidence=0.7)

        candidate = _valid_candidate()
        result = await writer.write_candidate(candidate, confidence=0.9)

        assert result is True
        # YAML file should exist
        yaml_files = list(candidates_dir.glob("*.yaml"))
        assert len(yaml_files) == 1
        # DB insert should have been called
        assert db.execute.call_count >= 2  # SELECT + INSERT
        db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_existing_higher_confidence_updates(self, tmp_path: Path) -> None:
        # Existing row with confidence 0.7
        db = _mock_db(existing_row=("hash123", 0.7))
        writer = CandidateFixWriter(db, tmp_path / "candidates", min_confidence=0.5)

        result = await writer.write_candidate(_valid_candidate(), confidence=0.9)

        assert result is True
        # Should UPDATE (not INSERT)
        calls = [str(c) for c in db.execute.call_args_list]
        update_calls = [c for c in calls if "UPDATE" in c]
        assert len(update_calls) == 1

    @pytest.mark.asyncio
    async def test_existing_lower_confidence_bumps_count(self, tmp_path: Path) -> None:
        # Existing row with confidence 0.9
        db = _mock_db(existing_row=("hash123", 0.9))
        writer = CandidateFixWriter(db, tmp_path / "candidates", min_confidence=0.5)

        result = await writer.write_candidate(_valid_candidate(), confidence=0.7)

        assert result is True
        # Should UPDATE verified_count only
        calls = [str(c) for c in db.execute.call_args_list]
        update_calls = [c for c in calls if "UPDATE" in c]
        assert len(update_calls) == 1

    @pytest.mark.asyncio
    async def test_get_ready_candidates(self, tmp_path: Path) -> None:
        db = _mock_db()
        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[
            ("h1", "ha", "fail", "yaml...", 0.9, 3, "/path/file.yaml"),
        ])
        db.execute = AsyncMock(return_value=cursor)

        writer = CandidateFixWriter(db, tmp_path / "candidates")

        ready = await writer.get_ready_candidates(min_verified_count=3)

        assert len(ready) == 1
        assert ready[0]["match_hash"] == "h1"
        assert ready[0]["verified_count"] == 3

    @pytest.mark.asyncio
    async def test_mark_notified(self, tmp_path: Path) -> None:
        db = _mock_db()
        writer = CandidateFixWriter(db, tmp_path / "candidates")

        await writer.mark_notified("hash123")

        db.execute.assert_called_once()
        call_args = db.execute.call_args[0]
        assert "UPDATE" in call_args[0]
        assert "notified" in call_args[0]


# ---------------------------------------------------------------------------
# T2 prompt schema
# ---------------------------------------------------------------------------


class TestT2PromptSchema:
    def test_prompt_includes_suggested_known_fix(self) -> None:
        from oasisagent.llm.prompts.diagnose_failure import SYSTEM_PROMPT

        assert "suggested_known_fix" in SYSTEM_PROMPT
        assert '"match"' in SYSTEM_PROMPT
        assert '"diagnosis"' in SYSTEM_PROMPT
        assert '"action"' in SYSTEM_PROMPT

    def test_prompt_includes_confidence_guidance(self) -> None:
        from oasisagent.llm.prompts.diagnose_failure import SYSTEM_PROMPT

        assert "0.7" in SYSTEM_PROMPT
        assert "generalizable" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestLearningConfig:
    def test_defaults(self) -> None:
        from oasisagent.config import LearningConfig

        config = LearningConfig()
        assert config.enabled is False
        assert config.min_confidence == 0.8
        assert config.min_verified_count == 3

    def test_in_top_level_config(self) -> None:
        from oasisagent.config import OasisAgentConfig

        config = OasisAgentConfig()
        assert hasattr(config, "learning")
        assert config.learning.enabled is False


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_migration_file_exists(self) -> None:
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent
            / "oasisagent"
            / "db"
            / "migrations"
            / "005_learning.py"
        )
        assert migration_path.exists()

    @pytest.mark.asyncio
    async def test_migration_runs(self) -> None:
        import importlib

        import aiosqlite

        m005 = importlib.import_module("oasisagent.db.migrations.005_learning")

        async with aiosqlite.connect(":memory:") as db:
            await m005.migrate(db)

            # Verify tables exist
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in await cursor.fetchall()}
            assert "learning_config" in tables
            assert "candidate_fixes" in tables

            # Verify learning_config seeds
            cursor = await db.execute("SELECT key, value FROM learning_config")
            rows = {row[0]: row[1] for row in await cursor.fetchall()}
            assert rows["min_confidence"] == "0.8"
            assert rows["min_verified_count"] == "3"
