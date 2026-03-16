"""Tests for the Proxmox VE polling ingestion adapter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.config import ProxmoxAdapterConfig
from oasisagent.ingestion.proxmox import ProxmoxAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> ProxmoxAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "https://pve.local:8006",
        "user": "root@pam",
        "token_name": "oasis",
        "token_value": "secret",
        "poll_interval": 30,
        "poll_nodes": True,
        "poll_vms": True,
        "poll_tasks": True,
        "poll_replication": True,
        "cpu_threshold": 90.0,
        "memory_threshold": 90.0,
    }
    defaults.update(overrides)
    return ProxmoxAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(
    **overrides: object,
) -> tuple[ProxmoxAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    with patch("oasisagent.ingestion.proxmox.ProxmoxClient"):
        adapter = ProxmoxAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = ProxmoxAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 30
        assert config.cpu_threshold == 90.0
        assert config.memory_threshold == 90.0
        assert config.url == "https://localhost:8006"

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            ProxmoxAdapterConfig(bogus="nope")  # type: ignore[call-arg]


class TestAdapterBasics:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "proxmox"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert await adapter.healthy() is False


# ---------------------------------------------------------------------------
# Node polling
# ---------------------------------------------------------------------------


class TestPollNodes:
    @pytest.mark.asyncio
    async def test_first_poll_seeds_only(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value=[
            {"type": "node", "name": "pve01", "online": 1},
        ])
        await adapter._poll_nodes()
        assert adapter._node_states == {"pve01": "online"}
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_offline_transition_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._node_states["pve01"] = "online"

        adapter._client.get = AsyncMock(return_value=[
            {"type": "node", "name": "pve01", "online": 0},
        ])
        await adapter._poll_nodes()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_node_unreachable"
        assert event.severity == Severity.ERROR
        assert event.entity_id == "pve01"
        assert event.system == "proxmox"

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._node_states["pve01"] = "offline"

        adapter._client.get = AsyncMock(return_value=[
            {"type": "node", "name": "pve01", "online": 1},
        ])
        await adapter._poll_nodes()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_node_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_stable_state_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._node_states["pve01"] = "online"

        adapter._client.get = AsyncMock(return_value=[
            {"type": "node", "name": "pve01", "online": 1},
        ])
        await adapter._poll_nodes()
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# VM polling
# ---------------------------------------------------------------------------


class TestPollVMs:
    @pytest.mark.asyncio
    async def test_first_poll_seeds_only(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value=[
            {"vmid": 100, "status": "running", "name": "docker-host"},
        ])
        await adapter._poll_vms()
        assert adapter._vm_states["pve:100"] == "running"
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_stopped_transition(self) -> None:
        adapter, queue = _make_adapter()
        adapter._vm_states["pve:100"] = "running"

        adapter._client.get = AsyncMock(return_value=[
            {"vmid": 100, "status": "stopped", "name": "docker-host"},
        ])
        await adapter._poll_vms()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_vm_stopped"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "pve:100"

    @pytest.mark.asyncio
    async def test_running_transition(self) -> None:
        adapter, queue = _make_adapter()
        adapter._vm_states["pve:100"] = "stopped"

        adapter._client.get = AsyncMock(return_value=[
            {"vmid": 100, "status": "running", "name": "docker-host"},
        ])
        await adapter._poll_vms()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_vm_running"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_locked_transition(self) -> None:
        adapter, queue = _make_adapter()
        adapter._vm_states["pve:200"] = "running"

        adapter._client.get = AsyncMock(return_value=[
            {"vmid": 200, "status": "locked", "name": "ha-vm"},
        ])
        await adapter._poll_vms()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_vm_locked"
        assert event.severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_independent_vms(self) -> None:
        adapter, queue = _make_adapter()
        adapter._vm_states["pve:100"] = "running"
        adapter._vm_states["pve:200"] = "running"

        adapter._client.get = AsyncMock(return_value=[
            {"vmid": 100, "status": "stopped", "name": "vm-a"},
            {"vmid": 200, "status": "running", "name": "vm-b"},
        ])
        await adapter._poll_vms()

        # Only vm 100 changed
        assert queue.put_nowait.call_count == 1
        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "pve:100"


# ---------------------------------------------------------------------------
# Node resource polling
# ---------------------------------------------------------------------------


class TestPollNodeResources:
    @pytest.mark.asyncio
    async def test_cpu_threshold_crossing(self) -> None:
        adapter, queue = _make_adapter(cpu_threshold=80.0)
        adapter._node_states["pve01"] = "online"

        adapter._client.get = AsyncMock(return_value={
            "cpu": 0.95,
            "memory": {"used": 1_000_000, "total": 10_000_000},
        })
        await adapter._poll_node_resources()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_node_high_cpu"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "pve01"

    @pytest.mark.asyncio
    async def test_cpu_recovery(self) -> None:
        adapter, queue = _make_adapter(cpu_threshold=80.0)
        adapter._node_states["pve01"] = "online"
        adapter._node_cpu_alert["pve01"] = True

        adapter._client.get = AsyncMock(return_value={
            "cpu": 0.5,
            "memory": {"used": 1_000_000, "total": 10_000_000},
        })
        await adapter._poll_node_resources()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_node_cpu_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_memory_threshold_crossing(self) -> None:
        adapter, queue = _make_adapter(memory_threshold=85.0)
        adapter._node_states["pve01"] = "online"

        adapter._client.get = AsyncMock(return_value={
            "cpu": 0.1,
            "memory": {"used": 9_000_000, "total": 10_000_000},
        })
        await adapter._poll_node_resources()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_node_high_memory"
        assert event.severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_memory_recovery(self) -> None:
        adapter, queue = _make_adapter(memory_threshold=85.0)
        adapter._node_states["pve01"] = "online"
        adapter._node_mem_alert["pve01"] = True

        adapter._client.get = AsyncMock(return_value={
            "cpu": 0.1,
            "memory": {"used": 5_000_000, "total": 10_000_000},
        })
        await adapter._poll_node_resources()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_node_memory_recovered"

    @pytest.mark.asyncio
    async def test_no_nodes_is_noop(self) -> None:
        adapter, queue = _make_adapter()
        # _node_states is empty — nothing to poll
        await adapter._poll_node_resources()
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Replication polling
# ---------------------------------------------------------------------------


class TestPollReplication:
    @pytest.mark.asyncio
    async def test_first_poll_seeds_only(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value=[
            {"guest": 100, "fail_count": 0},
        ])
        await adapter._poll_replication()
        assert adapter._repl_states["pve:100"] is False
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_transition(self) -> None:
        adapter, queue = _make_adapter()
        adapter._repl_states["pve:100"] = False

        adapter._client.get = AsyncMock(return_value=[
            {"guest": 100, "fail_count": 3, "source": "pve01", "target": "pve02"},
        ])
        await adapter._poll_replication()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_replication_failed"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "pve:100"
        assert event.payload["fail_count"] == 3

    @pytest.mark.asyncio
    async def test_recovery_transition(self) -> None:
        adapter, queue = _make_adapter()
        adapter._repl_states["pve:100"] = True

        adapter._client.get = AsyncMock(return_value=[
            {"guest": 100, "fail_count": 0, "source": "pve01", "target": "pve02"},
        ])
        await adapter._poll_replication()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_replication_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_stable_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._repl_states["pve:100"] = False

        adapter._client.get = AsyncMock(return_value=[
            {"guest": 100, "fail_count": 0},
        ])
        await adapter._poll_replication()
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Task polling
# ---------------------------------------------------------------------------


class TestPollTasks:
    @pytest.mark.asyncio
    async def test_failed_backup_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        now_epoch = int(time.time())

        adapter._client.get = AsyncMock(return_value=[
            {
                "upid": "UPID:pve01:001234:ABCDEF:00000001:vzdump:100:root@pam:",
                "type": "vzdump",
                "status": "backup failed",
                "endtime": now_epoch + 10,
                "node": "pve01",
                "user": "root@pam",
            },
        ])
        await adapter._poll_tasks()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_backup_failed"
        assert event.entity_id == "pve:100"
        assert event.severity == Severity.WARNING
        assert event.payload["upid"].startswith("UPID:")

    @pytest.mark.asyncio
    async def test_non_vzdump_failure(self) -> None:
        adapter, queue = _make_adapter()
        now_epoch = int(time.time())

        adapter._client.get = AsyncMock(return_value=[
            {
                "upid": "UPID:pve01:001234:ABCDEF:00000001:qmigrate:200:root@pam:",
                "type": "qmigrate",
                "status": "ERROR: migration failed",
                "endtime": now_epoch + 10,
                "node": "pve01",
            },
        ])
        await adapter._poll_tasks()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "pve_task_failed"
        assert event.entity_id == "pve:200"

    @pytest.mark.asyncio
    async def test_ok_task_ignored(self) -> None:
        adapter, queue = _make_adapter()
        now_epoch = int(time.time())

        adapter._client.get = AsyncMock(return_value=[
            {
                "upid": "UPID:pve01:001234:ABCDEF:00000001:vzdump:100:root@pam:",
                "type": "vzdump",
                "status": "OK",
                "endtime": now_epoch + 10,
            },
        ])
        await adapter._poll_tasks()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_upid_dedup(self) -> None:
        adapter, queue = _make_adapter()
        now_epoch = int(time.time())
        upid = "UPID:pve01:001234:ABCDEF:00000001:vzdump:100:root@pam:"

        # First poll — emits event
        adapter._client.get = AsyncMock(return_value=[
            {"upid": upid, "type": "vzdump", "status": "failed", "endtime": now_epoch + 10},
        ])
        await adapter._poll_tasks()
        assert queue.put_nowait.call_count == 1

        # Second poll — same UPID, no event
        await adapter._poll_tasks()
        assert queue.put_nowait.call_count == 1

    @pytest.mark.asyncio
    async def test_stale_upid_eviction(self) -> None:
        adapter, _queue = _make_adapter(poll_interval=30)

        # Seed a stale UPID entry (well past 2*poll_interval ago)
        stale_upid = "UPID:old:task"
        adapter._seen_tasks[stale_upid] = time.monotonic() - 120

        now_epoch = int(time.time())
        adapter._client.get = AsyncMock(return_value=[
            {
                "upid": "UPID:pve01:001234:ABCDEF:00000001:vzdump:100:root@pam:",
                "type": "vzdump",
                "status": "failed",
                "endtime": now_epoch + 10,
            },
        ])
        await adapter._poll_tasks()

        # Stale entry should be evicted
        assert stale_upid not in adapter._seen_tasks

    @pytest.mark.asyncio
    async def test_node_level_task_uses_upid_as_entity(self) -> None:
        adapter, queue = _make_adapter()
        now_epoch = int(time.time())
        # Node-level task — field 6 is non-numeric
        upid = "UPID:pve01:001234:ABCDEF:00000001:aptupdate:node:root@pam:"

        adapter._client.get = AsyncMock(return_value=[
            {"upid": upid, "type": "aptupdate", "status": "failed", "endtime": now_epoch + 10},
        ])
        await adapter._poll_tasks()

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == upid


# ---------------------------------------------------------------------------
# UPID parsing
# ---------------------------------------------------------------------------


class TestEntityFromUpid:
    def test_vm_upid(self) -> None:
        upid = "UPID:pve01:001234:ABCDEF:00000001:vzdump:100:root@pam:"
        assert ProxmoxAdapter._entity_from_upid(upid) == "pve:100"

    def test_node_level_upid(self) -> None:
        upid = "UPID:pve01:001234:ABCDEF:00000001:aptupdate:node:root@pam:"
        # "node" is not numeric — falls back to full UPID
        assert ProxmoxAdapter._entity_from_upid(upid) == upid

    def test_short_upid_fallback(self) -> None:
        assert ProxmoxAdapter._entity_from_upid("short") == "short"


# ---------------------------------------------------------------------------
# Topology discovery
# ---------------------------------------------------------------------------


class TestTopologyDiscovery:
    @pytest.mark.asyncio
    async def test_produces_nodes_and_edges(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.get = AsyncMock(return_value=[
            {"type": "node", "node": "pve01", "status": "online", "maxcpu": 8},
            {"type": "qemu", "vmid": 100, "name": "docker-host",
             "node": "pve01", "status": "running"},
            {"type": "lxc", "vmid": 200, "name": "pihole", "node": "pve01", "status": "running"},
        ])

        nodes, edges = await adapter.discover_topology()

        # 3 nodes: 1 host + 1 vm + 1 ct
        assert len(nodes) == 3
        node_types = {n.entity_id: n.entity_type for n in nodes}
        assert node_types["proxmox:pve01"] == "host"
        assert node_types["proxmox:100"] == "vm"
        assert node_types["proxmox:200"] == "ct"

        # 2 edges: both VMs run on the host
        assert len(edges) == 2
        for edge in edges:
            assert edge.edge_type == "runs_on"
            assert edge.to_entity == "proxmox:pve01"

    @pytest.mark.asyncio
    async def test_empty_on_api_error(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.get = AsyncMock(side_effect=Exception("connection refused"))

        nodes, edges = await adapter.discover_topology()
        assert nodes == []
        assert edges == []
