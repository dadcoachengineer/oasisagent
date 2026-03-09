"""Tests for the approval queue CLI."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from oasisagent.cli import (
    _cmd_approve,
    _cmd_approve_all,
    _cmd_list,
    _cmd_reject,
    _cmd_show,
    _format_table,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BROKER = "mqtt://localhost:1883"


def _pending_action(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "event_id": "evt-001",
        "action": {
            "description": "Restart ZWave integration",
            "handler": "homeassistant",
            "operation": "restart_integration",
            "params": {"integration": "zwave_js"},
            "risk_tier": "recommend",
            "reasoning": "Integration needs restart",
        },
        "diagnosis": "ZWave USB stick needs reset",
        "created_at": "2025-01-15T12:00:00+00:00",
        "expires_at": "2025-01-15T12:30:00+00:00",
        "status": "pending",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# _format_table
# ---------------------------------------------------------------------------


class TestFormatTable:
    def test_empty_list(self) -> None:
        assert _format_table([]) == "No pending actions."

    def test_single_action(self) -> None:
        result = _format_table([_pending_action()])

        assert "550e8400" in result
        assert "ZWave USB stick needs reset" in result
        assert "homeassistant" in result
        assert "restart_integration" in result

    def test_multiple_actions(self) -> None:
        actions = [
            _pending_action(id="aaa", diagnosis="First issue"),
            _pending_action(id="bbb", diagnosis="Second issue"),
        ]
        result = _format_table(actions)

        assert "aaa" in result
        assert "bbb" in result
        assert "First issue" in result
        assert "Second issue" in result

    def test_header_row(self) -> None:
        result = _format_table([_pending_action()])

        lines = result.split("\n")
        header = lines[0]
        assert "ID" in header
        assert "DIAGNOSIS" in header
        assert "HANDLER" in header
        assert "OPERATION" in header
        assert "EXPIRES" in header

    def test_long_diagnosis_truncated(self) -> None:
        long_diag = "A" * 100
        result = _format_table([_pending_action(diagnosis=long_diag)])

        # Diagnosis column truncated to 40 chars
        assert "A" * 40 in result
        assert "A" * 41 not in result


# ---------------------------------------------------------------------------
# queue list
# ---------------------------------------------------------------------------


class TestCmdList:
    async def test_list_with_pending_actions(self, capsys: pytest.CaptureFixture[str]) -> None:
        actions = [_pending_action()]

        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = json.dumps(actions)
            exit_code = await _cmd_list(_BROKER, None, None)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "550e8400" in captured.out
        assert "ZWave USB stick" in captured.out

    async def test_list_empty_queue(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = None
            exit_code = await _cmd_list(_BROKER, None, None)

        assert exit_code == 0
        assert "No pending actions" in capsys.readouterr().out

    async def test_list_empty_json_array(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            exit_code = await _cmd_list(_BROKER, None, None)

        assert exit_code == 0
        assert "No pending actions" in capsys.readouterr().out

    async def test_list_invalid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = "not json"
            exit_code = await _cmd_list(_BROKER, None, None)

        assert exit_code == 1
        assert "invalid JSON" in capsys.readouterr().err

    async def test_list_non_array_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = '{"key": "value"}'
            exit_code = await _cmd_list(_BROKER, None, None)

        assert exit_code == 1
        assert "expected JSON array" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# queue show
# ---------------------------------------------------------------------------


class TestCmdShow:
    async def test_show_existing_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        action = _pending_action()

        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = json.dumps(action)
            exit_code = await _cmd_show(_BROKER, None, None, "abc-123")

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "550e8400" in captured.out
        assert "restart_integration" in captured.out

    async def test_show_not_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = None
            exit_code = await _cmd_show(_BROKER, None, None, "nonexistent")

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "not found or already resolved" in captured.err

    async def test_show_empty_payload(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = ""
            exit_code = await _cmd_show(_BROKER, None, None, "cleared-id")

        assert exit_code == 1
        assert "not found or already resolved" in capsys.readouterr().err

    async def test_show_invalid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = "not json"
            exit_code = await _cmd_show(_BROKER, None, None, "bad-id")

        assert exit_code == 1
        assert "invalid JSON" in capsys.readouterr().err

    async def test_show_subscribes_to_correct_topic(self) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = json.dumps(_pending_action())
            await _cmd_show(_BROKER, None, None, "my-action-id")

        mock.assert_awaited_once_with(
            _BROKER, None, None, "oasis/pending/my-action-id"
        )


# ---------------------------------------------------------------------------
# queue approve
# ---------------------------------------------------------------------------


class TestCmdApprove:
    async def test_approve_publishes_to_correct_topic(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("oasisagent.cli._publish", new_callable=AsyncMock) as mock:
            exit_code = await _cmd_approve(_BROKER, None, None, "abc-123")

        assert exit_code == 0
        mock.assert_awaited_once_with(
            _BROKER, None, None, "oasis/approve/abc-123"
        )

    async def test_approve_prints_honest_confirmation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("oasisagent.cli._publish", new_callable=AsyncMock):
            await _cmd_approve(_BROKER, None, None, "abc-123")

        captured = capsys.readouterr()
        assert "Approval request sent" in captured.out
        assert "abc-123" in captured.out


# ---------------------------------------------------------------------------
# queue reject
# ---------------------------------------------------------------------------


class TestCmdReject:
    async def test_reject_publishes_to_correct_topic(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("oasisagent.cli._publish", new_callable=AsyncMock) as mock:
            exit_code = await _cmd_reject(_BROKER, None, None, "abc-123")

        assert exit_code == 0
        mock.assert_awaited_once_with(
            _BROKER, None, None, "oasis/reject/abc-123"
        )

    async def test_reject_prints_honest_confirmation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("oasisagent.cli._publish", new_callable=AsyncMock):
            await _cmd_reject(_BROKER, None, None, "abc-123")

        captured = capsys.readouterr()
        assert "Rejection request sent" in captured.out
        assert "abc-123" in captured.out


# ---------------------------------------------------------------------------
# queue approve-all
# ---------------------------------------------------------------------------


class TestCmdApproveAll:
    async def test_approve_all_with_confirmation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        actions = [
            _pending_action(id="aaa"),
            _pending_action(id="bbb"),
        ]

        with (
            patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock_read,
            patch("oasisagent.cli._publish", new_callable=AsyncMock) as mock_pub,
            patch("builtins.input", return_value="y"),
        ):
            mock_read.return_value = json.dumps(actions)
            exit_code = await _cmd_approve_all(_BROKER, None, None)

        assert exit_code == 0
        assert mock_pub.await_count == 2
        mock_pub.assert_any_await(_BROKER, None, None, "oasis/approve/aaa")
        mock_pub.assert_any_await(_BROKER, None, None, "oasis/approve/bbb")

    async def test_approve_all_aborted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        actions = [_pending_action(id="aaa")]

        with (
            patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock_read,
            patch("oasisagent.cli._publish", new_callable=AsyncMock) as mock_pub,
            patch("builtins.input", return_value="n"),
        ):
            mock_read.return_value = json.dumps(actions)
            exit_code = await _cmd_approve_all(_BROKER, None, None)

        assert exit_code == 1
        mock_pub.assert_not_awaited()
        assert "Aborted" in capsys.readouterr().out

    async def test_approve_all_empty_queue(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock:
            mock.return_value = None
            exit_code = await _cmd_approve_all(_BROKER, None, None)

        assert exit_code == 0
        assert "No pending actions" in capsys.readouterr().out

    async def test_approve_all_eof_aborts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        actions = [_pending_action(id="aaa")]

        with (
            patch("oasisagent.cli._read_retained", new_callable=AsyncMock) as mock_read,
            patch("oasisagent.cli._publish", new_callable=AsyncMock) as mock_pub,
            patch("builtins.input", side_effect=EOFError),
        ):
            mock_read.return_value = json.dumps(actions)
            exit_code = await _cmd_approve_all(_BROKER, None, None)

        assert exit_code == 1
        mock_pub.assert_not_awaited()


# ---------------------------------------------------------------------------
# Environment variable defaults
# ---------------------------------------------------------------------------


class TestEnvDefaults:
    def test_broker_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OASIS_MQTT_BROKER", "mqtt://custom:1884")

        from oasisagent.cli import _env_default

        assert _env_default("OASIS_MQTT_BROKER", _BROKER) == "mqtt://custom:1884"

    def test_broker_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OASIS_MQTT_BROKER", raising=False)

        from oasisagent.cli import _env_default

        assert _env_default("OASIS_MQTT_BROKER", _BROKER) == _BROKER


# ---------------------------------------------------------------------------
# Sub-command routing
# ---------------------------------------------------------------------------


class TestMainRouting:
    def test_no_args_runs_agent(self) -> None:
        """oasisagent with no args defaults to running the agent."""
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None  # Default: run agent

    def test_run_command(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["run"])
        assert args.command == "run"

    def test_queue_list_command(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["queue", "list"])
        assert args.command == "queue"
        assert args.queue_command == "list"

    def test_queue_approve_command(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["queue", "approve", "abc-123"])
        assert args.command == "queue"
        assert args.queue_command == "approve"
        assert args.action_id == "abc-123"

    def test_queue_reject_command(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["queue", "reject", "abc-123"])
        assert args.command == "queue"
        assert args.queue_command == "reject"
        assert args.action_id == "abc-123"

    def test_queue_show_command(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["queue", "show", "abc-123"])
        assert args.command == "queue"
        assert args.queue_command == "show"
        assert args.action_id == "abc-123"

    def test_queue_approve_all_command(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["queue", "approve-all"])
        assert args.command == "queue"
        assert args.queue_command == "approve-all"

    def test_queue_broker_flag(self) -> None:
        from oasisagent.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["queue", "--broker", "mqtt://custom:1884", "list"])
        assert args.broker == "mqtt://custom:1884"
