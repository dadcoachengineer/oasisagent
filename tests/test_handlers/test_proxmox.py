"""Tests for the Proxmox VE handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.config import ProxmoxHandlerConfig
from oasisagent.handlers.proxmox import (
    HandlerNotStartedError,
    ProxmoxHandler,
)
from oasisagent.models import (
    ActionResult,
    ActionStatus,
    Event,
    EventMetadata,
    RecommendedAction,
    RiskTier,
    Severity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> ProxmoxHandlerConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "url": "https://pve.example.com:8006",
        "user": "root@pam",
        "token_name": "oasis",
        "token_value": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "verify_ssl": False,
    }
    defaults.update(overrides)
    return ProxmoxHandlerConfig(**defaults)


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "proxmox",
        "event_type": "vm_stopped",
        "entity_id": "100",
        "severity": Severity.WARNING,
        "timestamp": datetime.now(UTC),
        "payload": {},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_action(**overrides: Any) -> RecommendedAction:
    defaults: dict[str, Any] = {
        "description": "Test action",
        "handler": "proxmox",
        "operation": "notify",
        "params": {},
        "risk_tier": RiskTier.AUTO_FIX,
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


def _pve_response(
    data: dict[str, Any] | list[Any] | str | None = None,
    status: int = 200,
) -> MagicMock:
    """Create a mock aiohttp response wrapping data in PVE's {data: ...} envelope."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value={"data": data})
    if status >= 400:
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=status,
            message=f"HTTP {status}",
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _patch_session(
    handler: ProxmoxHandler, responses: dict[str, MagicMock],
) -> None:
    """Replace the handler's session with a mock returning specified responses."""
    session = MagicMock(spec=aiohttp.ClientSession)

    def _make_cm(resp: MagicMock) -> MagicMock:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    def _get_handler(path: str, **kwargs: Any) -> MagicMock:
        key = f"get:{path}"
        resp = responses.get(key, _pve_response())
        return _make_cm(resp)

    def _post_handler(path: str, **kwargs: Any) -> MagicMock:
        key = f"post:{path}"
        resp = responses.get(key, _pve_response())
        return _make_cm(resp)

    session.get = MagicMock(side_effect=_get_handler)
    session.post = MagicMock(side_effect=_post_handler)
    session.close = AsyncMock()

    handler._session = session


def _started_handler(**config_overrides: Any) -> ProxmoxHandler:
    """Create a handler with a mocked session (skipping real HTTP)."""
    handler = ProxmoxHandler(_make_config(**config_overrides))
    # Set up session mock and seed an empty inventory
    handler._session = MagicMock(spec=aiohttp.ClientSession)
    handler._inventory = {}
    handler._inventory_ts = 0.0
    return handler


def _started_handler_with_inventory(**config_overrides: Any) -> ProxmoxHandler:
    """Create a handler with inventory pre-populated."""
    handler = _started_handler(**config_overrides)
    handler._inventory = {
        "100": {
            "vmid": "100",
            "node": "pve-01",
            "type": "qemu",
            "name": "web-server",
            "status": "running",
        },
        "200": {
            "vmid": "200",
            "node": "pve-01",
            "type": "lxc",
            "name": "dns-server",
            "status": "running",
        },
    }
    import time

    handler._inventory_ts = time.monotonic()
    return handler


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_creates_session(self) -> None:
        handler = ProxmoxHandler(_make_config())
        with (
            patch("oasisagent.handlers.proxmox.aiohttp.TCPConnector") as mock_conn,
            patch("oasisagent.handlers.proxmox.aiohttp.ClientSession") as mock_cls,
        ):
            mock_session = MagicMock()
            mock_session.get = MagicMock(return_value=MagicMock(
                __aenter__=AsyncMock(return_value=_pve_response(data=[])),
                __aexit__=AsyncMock(return_value=False),
            ))
            mock_session.close = AsyncMock()
            mock_cls.return_value = mock_session

            await handler.start()

            mock_conn.assert_called_once_with(ssl=False)
            mock_cls.assert_called_once()
            assert handler._session is not None

    async def test_start_with_verify_ssl(self) -> None:
        handler = ProxmoxHandler(_make_config(verify_ssl=True))
        with (
            patch("oasisagent.handlers.proxmox.aiohttp.TCPConnector") as mock_conn,
            patch("oasisagent.handlers.proxmox.aiohttp.ClientSession") as mock_cls,
        ):
            mock_session = MagicMock()
            mock_session.get = MagicMock(return_value=MagicMock(
                __aenter__=AsyncMock(return_value=_pve_response(data=[])),
                __aexit__=AsyncMock(return_value=False),
            ))
            mock_session.close = AsyncMock()
            mock_cls.return_value = mock_session

            await handler.start()

            # Should pass an SSLContext, not False
            call_kwargs = mock_conn.call_args
            ssl_arg = call_kwargs[1]["ssl"] if "ssl" in call_kwargs[1] else call_kwargs[0][0]
            import ssl

            assert isinstance(ssl_arg, ssl.SSLContext)

    async def test_start_auth_header_format(self) -> None:
        config = _make_config(
            user="root@pam",
            token_name="oasis",
            token_value="test-uuid-value",
        )
        handler = ProxmoxHandler(config)
        with (
            patch("oasisagent.handlers.proxmox.aiohttp.TCPConnector"),
            patch("oasisagent.handlers.proxmox.aiohttp.ClientSession") as mock_cls,
        ):
            mock_session = MagicMock()
            mock_session.get = MagicMock(return_value=MagicMock(
                __aenter__=AsyncMock(return_value=_pve_response(data=[])),
                __aexit__=AsyncMock(return_value=False),
            ))
            mock_session.close = AsyncMock()
            mock_cls.return_value = mock_session

            await handler.start()

            call_kwargs = mock_cls.call_args[1]
            auth_header = call_kwargs["headers"]["Authorization"]
            assert auth_header == "PVEAPIToken=root@pam!oasis=test-uuid-value"

    async def test_start_tolerates_inventory_failure(self) -> None:
        """If initial inventory fetch fails, start() still completes."""
        handler = ProxmoxHandler(_make_config())
        with (
            patch("oasisagent.handlers.proxmox.aiohttp.TCPConnector"),
            patch("oasisagent.handlers.proxmox.aiohttp.ClientSession") as mock_cls,
        ):
            mock_session = MagicMock()
            mock_session.get = MagicMock(side_effect=aiohttp.ClientError("refused"))
            mock_session.close = AsyncMock()
            mock_cls.return_value = mock_session

            await handler.start()  # Should not raise
            assert handler._session is not None

    async def test_stop_closes_session(self) -> None:
        handler = _started_handler()
        mock_session = AsyncMock()
        handler._session = mock_session

        await handler.stop()

        mock_session.close.assert_called_once()
        assert handler._session is None

    async def test_stop_without_start_is_noop(self) -> None:
        handler = ProxmoxHandler(_make_config())
        await handler.stop()  # Should not raise

    async def test_execute_before_start_raises(self) -> None:
        handler = ProxmoxHandler(_make_config())
        with pytest.raises(HandlerNotStartedError):
            await handler.execute(_make_event(), _make_action())

    async def test_get_context_before_start_raises(self) -> None:
        handler = ProxmoxHandler(_make_config())
        with pytest.raises(HandlerNotStartedError):
            await handler.get_context(_make_event())


# ---------------------------------------------------------------------------
# name() and can_handle()
# ---------------------------------------------------------------------------


class TestNameAndCanHandle:
    def test_name_is_proxmox(self) -> None:
        handler = ProxmoxHandler(_make_config())
        assert handler.name() == "proxmox"

    async def test_known_operations_accepted(self) -> None:
        handler = ProxmoxHandler(_make_config())
        event = _make_event()
        for op in [
            "notify", "get_node_status", "list_vms", "get_vm_status",
            "start_vm", "stop_vm", "reboot_vm", "list_tasks", "get_task_log",
        ]:
            action = _make_action(operation=op)
            assert await handler.can_handle(event, action) is True

    async def test_unknown_operation_rejected(self) -> None:
        handler = ProxmoxHandler(_make_config())
        action = _make_action(operation="delete_vm")
        assert await handler.can_handle(_make_event(), action) is False

    async def test_wrong_handler_rejected(self) -> None:
        handler = ProxmoxHandler(_make_config())
        action = _make_action(handler="docker", operation="notify")
        assert await handler.can_handle(_make_event(), action) is False


# ---------------------------------------------------------------------------
# execute: notify
# ---------------------------------------------------------------------------


class TestNotify:
    async def test_notify_returns_message(self) -> None:
        handler = _started_handler()
        action = _make_action(
            operation="notify",
            params={"message": "VM locked, needs manual unlock"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["message"] == "VM locked, needs manual unlock"

    async def test_notify_uses_description_as_fallback(self) -> None:
        handler = _started_handler()
        action = _make_action(operation="notify", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["message"] == "Test action"


# ---------------------------------------------------------------------------
# execute: get_node_status
# ---------------------------------------------------------------------------


class TestGetNodeStatus:
    async def test_success(self) -> None:
        handler = _started_handler()
        node_data = {"cpu": 0.25, "memory": {"total": 64e9, "used": 32e9}}
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-01/status": _pve_response(data=node_data),
        })
        action = _make_action(
            operation="get_node_status",
            params={"node": "pve-01"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["status"]["cpu"] == 0.25

    async def test_uses_entity_id_as_node(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-02/status": _pve_response(data={"cpu": 0.5}),
        })
        action = _make_action(operation="get_node_status", params={})

        result = await handler.execute(_make_event(entity_id="pve-02"), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["node"] == "pve-02"

    async def test_missing_node_fails(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {})
        action = _make_action(operation="get_node_status", params={})

        result = await handler.execute(_make_event(entity_id=""), action)

        assert result.status == ActionStatus.FAILURE
        assert "node" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# execute: list_vms
# ---------------------------------------------------------------------------


class TestListVms:
    async def test_returns_inventory(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {})
        action = _make_action(operation="list_vms", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["count"] == 2

    async def test_empty_inventory(self) -> None:
        handler = _started_handler()
        import time

        handler._inventory_ts = time.monotonic()  # Fresh but empty
        _patch_session(handler, {})
        action = _make_action(operation="list_vms", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["count"] == 0


# ---------------------------------------------------------------------------
# execute: get_vm_status
# ---------------------------------------------------------------------------


class TestGetVmStatus:
    async def test_success_with_params(self) -> None:
        handler = _started_handler()
        vm_data = {"status": "running", "cpu": 0.1, "mem": 2e9}
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-01/qemu/100/status/current": _pve_response(
                data=vm_data,
            ),
        })
        action = _make_action(
            operation="get_vm_status",
            params={"vmid": "100", "node": "pve-01", "type": "qemu"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["status"]["status"] == "running"

    async def test_resolves_from_inventory(self) -> None:
        handler = _started_handler_with_inventory()
        vm_data = {"status": "running"}
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-01/qemu/100/status/current": _pve_response(
                data=vm_data,
            ),
        })
        action = _make_action(
            operation="get_vm_status",
            params={"vmid": "100"},  # No node or type — should resolve from inventory
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["node"] == "pve-01"
        assert result.details["vm_type"] == "qemu"

    async def test_unresolvable_vm_fails(self) -> None:
        handler = _started_handler()
        import time

        handler._inventory_ts = time.monotonic()
        _patch_session(handler, {
            # Inventory refresh returns empty
            "get:/api2/json/cluster/resources": _pve_response(data=[]),
        })
        action = _make_action(
            operation="get_vm_status",
            params={"vmid": "999"},
        )

        result = await handler.execute(_make_event(entity_id="999"), action)

        assert result.status == ActionStatus.FAILURE
        assert "resolve" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# execute: start_vm, stop_vm, reboot_vm
# ---------------------------------------------------------------------------


class TestVmLifecycle:
    async def test_start_vm_success(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "post:/api2/json/nodes/pve-01/qemu/100/status/start": _pve_response(
                data="UPID:pve-01:001234:task-start",
            ),
        })
        action = _make_action(
            operation="start_vm",
            params={"vmid": "100"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["vmid"] == "100"
        assert result.details["node"] == "pve-01"
        assert "UPID" in result.details["upid"]

    async def test_stop_vm_uses_shutdown_endpoint(self) -> None:
        """stop_vm should use /shutdown (ACPI), not /stop (hard power-off)."""
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "post:/api2/json/nodes/pve-01/qemu/100/status/shutdown": _pve_response(
                data="UPID:pve-01:001234:task-shutdown",
            ),
        })
        action = _make_action(
            operation="stop_vm",
            params={"vmid": "100"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        # Verify /shutdown was called (not /stop)
        handler._session.post.assert_called()
        call_path = handler._session.post.call_args[0][0]
        assert "shutdown" in call_path

    async def test_reboot_vm_success(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "post:/api2/json/nodes/pve-01/qemu/100/status/reboot": _pve_response(
                data="UPID:pve-01:001234:task-reboot",
            ),
        })
        action = _make_action(
            operation="reboot_vm",
            params={"vmid": "100"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS

    async def test_start_lxc_container(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "post:/api2/json/nodes/pve-01/lxc/200/status/start": _pve_response(
                data="UPID:pve-01:001234:task-start-ct",
            ),
        })
        action = _make_action(
            operation="start_vm",
            params={"vmid": "200"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["vm_type"] == "lxc"

    async def test_vm_action_unresolvable_node_fails(self) -> None:
        handler = _started_handler()  # Empty inventory
        import time

        handler._inventory_ts = time.monotonic()
        _patch_session(handler, {
            "get:/api2/json/cluster/resources": _pve_response(data=[]),
        })
        action = _make_action(
            operation="start_vm",
            params={"vmid": "999"},
        )

        result = await handler.execute(_make_event(entity_id="999"), action)

        assert result.status == ActionStatus.FAILURE

    async def test_http_error_returns_failure(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "post:/api2/json/nodes/pve-01/qemu/100/status/start": _pve_response(
                status=500,
            ),
        })
        action = _make_action(
            operation="start_vm",
            params={"vmid": "100"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "HTTP error" in (result.error_message or "")


# ---------------------------------------------------------------------------
# execute: list_tasks and get_task_log
# ---------------------------------------------------------------------------


class TestTasks:
    async def test_list_tasks_success(self) -> None:
        handler = _started_handler()
        tasks = [
            {"upid": "UPID:pve-01:1", "type": "vzdump", "status": "OK"},
            {"upid": "UPID:pve-01:2", "type": "qmigrate", "status": "OK"},
        ]
        _patch_session(handler, {
            "get:/api2/json/cluster/tasks": _pve_response(data=tasks),
        })
        action = _make_action(operation="list_tasks", params={})

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert result.details["count"] == 2

    async def test_get_task_log_success(self) -> None:
        handler = _started_handler()
        log_lines = [
            {"n": 1, "t": "INFO: starting backup"},
            {"n": 2, "t": "INFO: backup complete"},
        ]
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-01/tasks/UPID:pve-01:1234/log": _pve_response(
                data=log_lines,
            ),
        })
        action = _make_action(
            operation="get_task_log",
            params={"upid": "UPID:pve-01:1234", "node": "pve-01"},
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.SUCCESS
        assert len(result.details["log"]) == 2

    async def test_get_task_log_missing_params_fails(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {})
        action = _make_action(
            operation="get_task_log",
            params={},  # Missing upid and node
        )

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "upid" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# execute: unknown operation, duration tracking
# ---------------------------------------------------------------------------


class TestExecuteMisc:
    async def test_unknown_operation_returns_failure(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {})
        action = _make_action(operation="delete_vm")

        result = await handler.execute(_make_event(), action)

        assert result.status == ActionStatus.FAILURE
        assert "Unknown operation" in (result.error_message or "")

    async def test_duration_ms_set_on_success(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {})
        action = _make_action(operation="notify", params={"message": "test"})

        result = await handler.execute(_make_event(), action)

        assert result.duration_ms is not None
        assert result.duration_ms >= 0

    async def test_duration_ms_set_on_http_error(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "post:/api2/json/nodes/pve-01/qemu/100/status/start": _pve_response(status=500),
        })
        action = _make_action(operation="start_vm", params={"vmid": "100"})

        result = await handler.execute(_make_event(), action)

        assert result.duration_ms is not None


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    async def test_non_lifecycle_returns_verified(self) -> None:
        handler = ProxmoxHandler(_make_config())
        action = _make_action(operation="list_vms")
        result = ActionResult(status=ActionStatus.SUCCESS)

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True

    async def test_start_vm_verified_when_running(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-01/qemu/100/status/current": _pve_response(
                data={"status": "running"},
            ),
        })
        action = _make_action(operation="start_vm")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": "100", "node": "pve-01", "vm_type": "qemu"},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True
        assert "running" in verify.message

    async def test_stop_vm_verified_when_stopped(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-01/qemu/100/status/current": _pve_response(
                data={"status": "stopped"},
            ),
        })
        action = _make_action(operation="stop_vm")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": "100", "node": "pve-01", "vm_type": "qemu"},
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is True
        assert "stopped" in verify.message

    async def test_verify_times_out(self) -> None:
        handler = _started_handler()
        _patch_session(handler, {
            "get:/api2/json/nodes/pve-01/qemu/100/status/current": _pve_response(
                data={"status": "stopped"},  # Still stopped — start_vm not working
            ),
        })
        action = _make_action(operation="start_vm")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": "100", "node": "pve-01", "vm_type": "qemu"},
        )

        # Monkey-patch timeout to make test fast
        with patch.object(
            ProxmoxHandler, "_verify_vm_status",
            new=AsyncMock(return_value=MagicMock(
                verified=False, message="VM 100 did not reach 'running' within 30s",
            )),
        ):
            verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is False
        assert "did not reach" in verify.message

    async def test_verify_without_node_returns_false(self) -> None:
        handler = _started_handler()
        action = _make_action(operation="start_vm")
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"vmid": "100"},  # No node
        )

        verify = await handler.verify(_make_event(), action, result)

        assert verify.verified is False
        assert "node" in verify.message.lower()


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


class TestGetContext:
    async def test_gathers_cluster_and_tasks(self) -> None:
        handler = _started_handler_with_inventory()
        cluster_status = [{"name": "pve-01", "online": 1}]
        vm_status = {"status": "running", "cpu": 0.15}
        tasks = [{"upid": "UPID:1", "type": "vzdump"}]

        _patch_session(handler, {
            "get:/api2/json/cluster/status": _pve_response(data=cluster_status),
            "get:/api2/json/nodes/pve-01/qemu/100/status/current": _pve_response(
                data=vm_status,
            ),
            "get:/api2/json/cluster/tasks": _pve_response(data=tasks),
        })

        context = await handler.get_context(_make_event(entity_id="100"))

        assert context["cluster_status"] == cluster_status
        assert context["vm_status"]["status"] == "running"
        assert context["resource_info"]["name"] == "web-server"
        assert len(context["recent_tasks"]) == 1

    async def test_unknown_entity_skips_vm_status(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "get:/api2/json/cluster/status": _pve_response(data=[]),
            "get:/api2/json/cluster/tasks": _pve_response(data=[]),
        })

        context = await handler.get_context(_make_event(entity_id="unknown"))

        assert "vm_status" not in context
        assert "resource_info" not in context

    async def test_handles_api_errors_gracefully(self) -> None:
        handler = _started_handler_with_inventory()
        _patch_session(handler, {
            "get:/api2/json/cluster/status": _pve_response(status=500),
            "get:/api2/json/nodes/pve-01/qemu/100/status/current": _pve_response(
                status=500,
            ),
            "get:/api2/json/cluster/tasks": _pve_response(status=500),
        })

        context = await handler.get_context(_make_event(entity_id="100"))

        assert "cluster_status_error" in context
        assert "vm_status_error" in context
        assert "recent_tasks_error" in context


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


class TestInventory:
    async def test_refresh_populates_inventory(self) -> None:
        handler = _started_handler()
        resources = [
            {"vmid": 100, "node": "pve-01", "type": "qemu", "name": "vm1"},
            {"vmid": 200, "node": "pve-02", "type": "lxc", "name": "ct1"},
        ]
        _patch_session(handler, {
            "get:/api2/json/cluster/resources": _pve_response(data=resources),
        })

        await handler._refresh_inventory()

        assert "100" in handler._inventory
        assert "200" in handler._inventory
        assert handler._inventory["100"]["node"] == "pve-01"
        assert handler._inventory["200"]["type"] == "lxc"

    async def test_resolve_vm_from_inventory(self) -> None:
        handler = _started_handler_with_inventory()

        node, vm_type = await handler._resolve_vm("100", {})

        assert node == "pve-01"
        assert vm_type == "qemu"

    async def test_resolve_vm_params_override_inventory(self) -> None:
        handler = _started_handler_with_inventory()

        node, vm_type = await handler._resolve_vm(
            "100", {"node": "pve-03", "type": "lxc"},
        )

        assert node == "pve-03"
        assert vm_type == "lxc"

    async def test_resolve_vm_cache_miss_triggers_refresh(self) -> None:
        handler = _started_handler()
        import time

        handler._inventory_ts = time.monotonic()

        resources = [
            {"vmid": 300, "node": "pve-02", "type": "qemu", "name": "new-vm"},
        ]
        _patch_session(handler, {
            "get:/api2/json/cluster/resources": _pve_response(data=resources),
        })

        node, vm_type = await handler._resolve_vm("300", {})

        assert node == "pve-02"
        assert vm_type == "qemu"
        # Verify inventory was refreshed
        assert "300" in handler._inventory

    async def test_resolve_vm_refresh_failure_returns_defaults(self) -> None:
        handler = _started_handler()
        import time

        handler._inventory_ts = time.monotonic()

        # Mock session that raises on GET
        session = MagicMock(spec=aiohttp.ClientSession)
        session.get = MagicMock(side_effect=aiohttp.ClientError("connection refused"))
        handler._session = session

        node, vm_type = await handler._resolve_vm("999", {})

        assert node == ""
        assert vm_type == "qemu"  # Default


# ---------------------------------------------------------------------------
# Known fixes YAML
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_proxmox_yaml_loads_and_has_expected_fixes(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "proxmox.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fixes = data["fixes"]
        fix_ids = {fix["id"] for fix in fixes}

        expected_ids = {
            "pve-vm-locked",
            "pve-zfs-scrub-errors",
            "pve-backup-failed",
            "pve-storage-thin-provision",
            "pve-node-unreachable",
            "pve-ha-fence",
            "pbs-verification-failed",
            "pbs-datastore-full",
            "pbs-gc-running-long",
        }
        assert expected_ids == fix_ids

    def test_all_fixes_have_required_fields(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "proxmox.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        for fix in data["fixes"]:
            assert "id" in fix, f"Fix missing 'id': {fix}"
            assert "match" in fix, f"Fix {fix['id']} missing 'match'"
            assert "diagnosis" in fix, f"Fix {fix['id']} missing 'diagnosis'"
            assert "action" in fix, f"Fix {fix['id']} missing 'action'"
            assert "risk_tier" in fix, f"Fix {fix['id']} missing 'risk_tier'"
            assert fix["action"]["handler"] == "proxmox"

    def test_all_fixes_use_valid_risk_tiers(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "proxmox.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        valid_tiers = {"auto_fix", "recommend", "escalate"}
        for fix in data["fixes"]:
            assert fix["risk_tier"] in valid_tiers, (
                f"Fix {fix['id']} has invalid risk_tier: {fix['risk_tier']}"
            )
