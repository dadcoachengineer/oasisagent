"""Tests for the Ollama LLM server ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pydantic import ValidationError

from oasisagent.config import OllamaAdapterConfig
from oasisagent.ingestion.ollama import OllamaAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> OllamaAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:11434",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return OllamaAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[OllamaAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = OllamaAdapter(config, queue)
    return adapter, queue


def _mock_response(status: int = 200, json_data: object = None) -> AsyncMock:
    mock = AsyncMock()
    mock.status = status
    mock.raise_for_status = MagicMock()
    mock.json = AsyncMock(return_value=json_data or {})
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = OllamaAdapterConfig()
        assert config.enabled is False
        assert config.url == "http://localhost:11434"
        assert config.poll_interval == 60
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
        assert adapter.name == "ollama"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.stop()
        assert adapter._stopping is True


# ---------------------------------------------------------------------------
# Health polling — state transitions
# ---------------------------------------------------------------------------


class TestHealthPolling:
    @pytest.mark.asyncio
    async def test_first_poll_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        # / returns health, /api/ps returns models
        responses = [
            _mock_response(200),
            _mock_response(200, {"models": [{"name": "llama3:latest"}]}),
        ]
        mock_session.get = MagicMock(side_effect=responses)

        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        mock_session = AsyncMock()
        responses = [
            _mock_response(200),
            _mock_response(200, {"models": [{"name": "llama3:latest"}]}),
        ]
        mock_session.get = MagicMock(side_effect=responses)

        await adapter._poll_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ollama_recovered"
        assert event.severity == Severity.INFO
        assert event.metadata.dedup_key == "ollama:health"

    @pytest.mark.asyncio
    async def test_url_construction(self) -> None:
        adapter, _ = _make_adapter(url="http://localhost:11434/")

        mock_session = AsyncMock()
        responses = [
            _mock_response(200),
            _mock_response(200, {"models": [{"name": "m"}]}),
        ]
        mock_session.get = MagicMock(side_effect=responses)

        await adapter._poll_health(mock_session)

        first_url = mock_session.get.call_args_list[0][0][0]
        assert first_url == "http://localhost:11434/"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_first_failure_emits_unreachable(self) -> None:
        adapter, queue = _make_adapter()

        adapter._handle_failure("connection refused")

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ollama_unreachable"
        assert event.severity == Severity.ERROR

    def test_already_down_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._service_ok = False

        adapter._handle_failure("still down")

        queue.put_nowait.assert_not_called()

    def test_failure_resets_model_state(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._service_ok = True
        adapter._has_models = True

        adapter._handle_failure("connection lost")

        assert adapter._has_models is None


# ---------------------------------------------------------------------------
# Model monitoring
# ---------------------------------------------------------------------------


class TestModelMonitoring:
    @pytest.mark.asyncio
    async def test_no_models_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(200, {"models": []}),
        )

        await adapter._poll_models(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ollama_no_models"
        assert event.severity == Severity.WARNING
        assert event.metadata.dedup_key == "ollama:models"

    @pytest.mark.asyncio
    async def test_no_models_no_duplicate(self) -> None:
        adapter, queue = _make_adapter()
        adapter._has_models = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(200, {"models": []}),
        )

        await adapter._poll_models(mock_session)

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_models_present_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(
            return_value=_mock_response(
                200, {"models": [{"name": "llama3:latest"}]},
            ),
        )

        await adapter._poll_models(mock_session)

        queue.put_nowait.assert_not_called()
        assert adapter._has_models is True


# ---------------------------------------------------------------------------
# Poll loop backoff
# ---------------------------------------------------------------------------


class TestPollLoopBackoff:
    @pytest.mark.asyncio
    async def test_backoff_increases(self) -> None:
        adapter, _ = _make_adapter(poll_interval=10)

        poll_count = 0
        sleep_totals: list[int] = []
        current_total = 0

        original_sleep = asyncio.sleep

        async def _tracking_sleep(_: float) -> None:
            nonlocal current_total
            current_total += 1
            await original_sleep(0)

        async def _fail_poll(_session: object) -> None:
            nonlocal poll_count, current_total
            if poll_count > 0:
                sleep_totals.append(current_total)
                current_total = 0
            poll_count += 1
            if poll_count >= 4:
                adapter._stopping = True
                return
            raise aiohttp.ClientError("refused")

        adapter._poll_health = _fail_poll  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.ollama.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.ollama.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
            mock_session = AsyncMock()
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter._poll_loop()

        assert len(sleep_totals) >= 2
        for i in range(1, len(sleep_totals)):
            assert sleep_totals[i] >= sleep_totals[i - 1]


# ---------------------------------------------------------------------------
# Registry & form specs
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_ollama_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "ollama" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["ollama"]
        assert meta.model is OllamaAdapterConfig
        assert meta.secret_fields == frozenset()

    def test_form_specs_exist(self) -> None:
        from oasisagent.ui.form_specs import FORM_SPECS

        assert "ollama" in FORM_SPECS
        field_names = {s.name for s in FORM_SPECS["ollama"]}
        assert "url" in field_names
        assert "poll_interval" in field_names

    def test_display_name(self) -> None:
        from oasisagent.ui.form_specs import TYPE_DISPLAY_NAMES

        assert TYPE_DISPLAY_NAMES["ollama"] == "Ollama"


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_exist(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "ollama.yaml"
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fix_ids = {fix["id"] for fix in data["fixes"]}
        assert "ollama-unreachable" in fix_ids
        assert "ollama-no-models" in fix_ids
