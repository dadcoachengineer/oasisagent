"""Tests for the N8N polling ingestion adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from oasisagent.config import N8nAdapterConfig
from oasisagent.ingestion.n8n import N8nAdapter
from oasisagent.models import Event, EventMetadata, Severity


def _make_config(**overrides: object) -> N8nAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:5678",
        "api_key": "test-n8n-key",
        "poll_interval": 300,
        "poll_executions": True,
        "timeout": 10,
    }
    defaults.update(overrides)
    return N8nAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[N8nAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = N8nAdapter(config, queue)
    return adapter, queue


def _mock_response(data: object, status: int = 200) -> AsyncMock:
    mock = AsyncMock()
    mock.status = status
    mock.raise_for_status = MagicMock()
    mock.json = AsyncMock(return_value=data)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = N8nAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 300
        assert config.poll_executions is True
        assert config.timeout == 10

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "n8n"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.stop()
        assert adapter._stopping is True

    def test_headers(self) -> None:
        adapter, _ = _make_adapter()
        headers = adapter._headers()
        assert headers["X-N8N-API-KEY"] == "test-n8n-key"


# ---------------------------------------------------------------------------
# Health polling
# ---------------------------------------------------------------------------


class TestHealthPolling:
    @pytest.mark.asyncio
    async def test_recovered_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"data": []}, status=200,
        ))

        await adapter._poll_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "n8n_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_already_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"data": []}, status=200,
        ))

        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"data": []}, status=200,
        ))

        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"data": []}, status=200,
        ))

        await adapter._poll_health(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "n8n:service:status"


# ---------------------------------------------------------------------------
# Execution failure polling
# ---------------------------------------------------------------------------


class TestExecutionPolling:
    @pytest.mark.asyncio
    async def test_failed_execution_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        # Set last poll to 10 minutes ago so execution is "new"
        adapter._last_execution_poll = datetime.now(tz=UTC) - timedelta(minutes=10)

        recent_time = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [
                {
                    "id": "12345",
                    "stoppedAt": recent_time,
                    "workflowData": {"name": "My Workflow"},
                    "data": {
                        "resultData": {
                            "error": {"message": "Connection refused"},
                        },
                    },
                },
            ],
        }))

        await adapter._poll_executions(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "n8n_execution_failed"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "My Workflow"
        assert event.payload["execution_id"] == "12345"
        assert event.payload["workflow_name"] == "My Workflow"
        assert event.payload["error_message"] == "Connection refused"

    @pytest.mark.asyncio
    async def test_old_execution_ignored(self) -> None:
        """Executions that finished before last poll should be skipped."""
        adapter, queue = _make_adapter()
        adapter._last_execution_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        old_time = (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [
                {
                    "id": "99",
                    "stoppedAt": old_time,
                    "workflowData": {"name": "Old Failure"},
                    "data": {},
                },
            ],
        }))

        await adapter._poll_executions(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_lookback(self) -> None:
        """First poll should only report failures within poll_interval."""
        adapter, queue = _make_adapter(poll_interval=300)
        # _last_execution_poll is None — first poll

        recent_time = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()
        old_time = (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [
                {
                    "id": "1",
                    "stoppedAt": recent_time,
                    "workflowData": {"name": "Recent Fail"},
                    "data": {},
                },
                {
                    "id": "2",
                    "stoppedAt": old_time,
                    "workflowData": {"name": "Old Fail"},
                    "data": {},
                },
            ],
        }))

        await adapter._poll_executions(mock_session)

        # Only the recent execution should be reported
        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.payload["execution_id"] == "1"

    @pytest.mark.asyncio
    async def test_empty_executions_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [],
        }))

        await adapter._poll_executions(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_stopped_at_skipped(self) -> None:
        """Executions without a stoppedAt/finishedAt are skipped."""
        adapter, queue = _make_adapter()
        adapter._last_execution_poll = datetime.now(tz=UTC) - timedelta(minutes=10)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [
                {
                    "id": "77",
                    "workflowData": {"name": "No Timestamp"},
                    "data": {},
                },
            ],
        }))

        await adapter._poll_executions(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_key_uses_execution_id(self) -> None:
        adapter, queue = _make_adapter()
        adapter._last_execution_poll = datetime.now(tz=UTC) - timedelta(minutes=10)

        recent_time = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [
                {
                    "id": "42",
                    "stoppedAt": recent_time,
                    "workflowData": {"name": "Test"},
                    "data": {},
                },
            ],
        }))

        await adapter._poll_executions(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "n8n:execution:42"

    @pytest.mark.asyncio
    async def test_no_workflow_name_uses_execution_id(self) -> None:
        """When workflowData is missing, entity_id falls back to execution ID."""
        adapter, queue = _make_adapter()
        adapter._last_execution_poll = datetime.now(tz=UTC) - timedelta(minutes=10)

        recent_time = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [
                {
                    "id": "55",
                    "stoppedAt": recent_time,
                    "data": {},
                },
            ],
        }))

        await adapter._poll_executions(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "execution-55"

    @pytest.mark.asyncio
    async def test_error_message_from_string(self) -> None:
        """Error can be a plain string instead of an object."""
        adapter, queue = _make_adapter()
        adapter._last_execution_poll = datetime.now(tz=UTC) - timedelta(minutes=10)

        recent_time = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "data": [
                {
                    "id": "66",
                    "stoppedAt": recent_time,
                    "workflowData": {"name": "String Error"},
                    "data": {
                        "resultData": {
                            "error": "Something went wrong",
                        },
                    },
                },
            ],
        }))

        await adapter._poll_executions(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.payload["error_message"] == "Something went wrong"


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_file_exists(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "n8n.yaml"
        )
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        assert "fixes" in data
        fixes = data["fixes"]
        assert len(fixes) >= 2

        for fix in fixes:
            assert "id" in fix
            assert "match" in fix
            assert "diagnosis" in fix
            assert "action" in fix
            assert "risk_tier" in fix

    def test_known_fix_ids_unique(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "n8n.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        ids = [fix["id"] for fix in data["fixes"]]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_n8n_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "n8n" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["n8n"]
        assert meta.model is N8nAdapterConfig
        assert "api_key" in meta.secret_fields


# ---------------------------------------------------------------------------
# Form specs
# ---------------------------------------------------------------------------


class TestFormSpecs:
    def test_n8n_form_spec_exists(self) -> None:
        from oasisagent.ui.form_specs import FORM_SPECS, TYPE_DESCRIPTIONS, TYPE_DISPLAY_NAMES

        assert "n8n" in FORM_SPECS
        assert "n8n" in TYPE_DISPLAY_NAMES
        assert "n8n" in TYPE_DESCRIPTIONS

        specs = FORM_SPECS["n8n"]
        field_names = {s.name for s in specs}
        assert "url" in field_names
        assert "api_key" in field_names
        assert "poll_interval" in field_names
        assert "poll_executions" in field_names
        assert "timeout" in field_names


# ---------------------------------------------------------------------------
# Enqueue error handling
# ---------------------------------------------------------------------------


class TestEnqueueErrorHandling:
    def test_enqueue_failure_logged(self) -> None:
        adapter, queue = _make_adapter()
        queue.put_nowait.side_effect = RuntimeError("queue full")

        event = Event(
            source="n8n",
            system="n8n",
            event_type="test",
            entity_id="test",
            severity=Severity.INFO,
            timestamp=datetime.now(tz=UTC),
            payload={},
            metadata=EventMetadata(dedup_key="test"),
        )

        adapter._enqueue(event)  # should not raise
