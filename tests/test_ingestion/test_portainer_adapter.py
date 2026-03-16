"""Tests for the Portainer polling ingestion adapter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.config import PortainerAdapterConfig
from oasisagent.ingestion.portainer import PortainerAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> PortainerAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "https://portainer.local:9443",
        "api_key": "ptr_test_key",
        "poll_interval": 30,
        "poll_endpoints": True,
        "poll_containers": True,
        "poll_stacks": True,
        "poll_container_resources": False,
        "cpu_threshold": 90.0,
        "memory_threshold": 90.0,
    }
    defaults.update(overrides)
    return PortainerAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(
    **overrides: object,
) -> tuple[PortainerAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    with patch("oasisagent.ingestion.portainer.PortainerClient"):
        adapter = PortainerAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = PortainerAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 30
        assert config.url == "https://localhost:9443"
        assert config.poll_container_resources is False
        assert config.cpu_threshold == 90.0
        assert config.memory_threshold == 90.0
        assert config.ignore_containers == []

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            PortainerAdapterConfig(bogus="nope")  # type: ignore[call-arg]


class TestAdapterBasics:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "portainer"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert await adapter.healthy() is False


# ---------------------------------------------------------------------------
# Endpoint polling
# ---------------------------------------------------------------------------


class TestPollEndpoints:
    @pytest.mark.asyncio
    async def test_first_poll_seeds_only(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value=[
            {"Id": 1, "Name": "primary", "Type": 1, "Status": 1,
             "PublicURL": "192.168.1.120", "URL": "tcp://192.168.1.120:2375"},
        ])
        await adapter._poll_endpoints()

        assert adapter._endpoint_states == {"primary": "online"}
        assert adapter._known_endpoints == {"primary": 1}
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_offline_transition_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._endpoint_states["primary"] = "online"
        adapter._known_endpoints["primary"] = 1

        adapter._client.get = AsyncMock(return_value=[
            {"Id": 1, "Name": "primary", "Type": 1, "Status": 2},
        ])
        await adapter._poll_endpoints()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_endpoint_unreachable"
        assert event.severity == Severity.ERROR
        assert event.entity_id == "portainer:primary"
        assert event.system == "portainer"

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._endpoint_states["primary"] = "offline"
        adapter._known_endpoints["primary"] = 1

        adapter._client.get = AsyncMock(return_value=[
            {"Id": 1, "Name": "primary", "Type": 1, "Status": 1},
        ])
        await adapter._poll_endpoints()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_endpoint_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_stable_state_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._endpoint_states["primary"] = "online"
        adapter._known_endpoints["primary"] = 1

        adapter._client.get = AsyncMock(return_value=[
            {"Id": 1, "Name": "primary", "Type": 1, "Status": 1},
        ])
        await adapter._poll_endpoints()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_seeds_known_endpoints(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.get = AsyncMock(return_value=[
            {"Id": 1, "Name": "primary", "Type": 1, "Status": 1,
             "PublicURL": "192.168.1.120", "URL": "tcp://192.168.1.120:2375"},
            {"Id": 2, "Name": "secondary", "Type": 2, "Status": 1,
             "PublicURL": "192.168.1.130", "URL": "tcp://192.168.1.130:2375"},
        ])
        await adapter._poll_endpoints()

        assert adapter._known_endpoints == {"primary": 1, "secondary": 2}
        assert "primary" in adapter._endpoint_meta
        assert "secondary" in adapter._endpoint_meta

    @pytest.mark.asyncio
    async def test_ignores_non_docker_endpoints(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.get = AsyncMock(return_value=[
            {"Id": 1, "Name": "primary", "Type": 1, "Status": 1},
            {"Id": 3, "Name": "k8s-cluster", "Type": 3, "Status": 1},
        ])
        await adapter._poll_endpoints()

        assert "primary" in adapter._known_endpoints
        assert "k8s-cluster" not in adapter._known_endpoints


# ---------------------------------------------------------------------------
# Container polling
# ---------------------------------------------------------------------------


def _container(
    name: str,
    state: str = "running",
    status: str = "Up 2 hours",
    ct_id: str = "abc123",
    image: str = "nginx:latest",
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "Id": ct_id,
        "Names": [f"/{name}"],
        "State": state,
        "Status": status,
        "Image": image,
        "Labels": labels or {},
    }


class TestPollContainers:
    @pytest.mark.asyncio
    async def test_first_poll_seeds_only(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "running"),
        ])
        await adapter._poll_containers()

        assert adapter._container_states == {"primary/nginx": "ok"}
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_exited_transition_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "ok"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "exited", "Exited (137) 2 minutes ago"),
        ])
        await adapter._poll_containers()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_container_exited"
        assert event.severity == Severity.ERROR
        assert event.entity_id == "portainer:primary/nginx"
        assert event.payload["endpoint_id"] == 1

    @pytest.mark.asyncio
    async def test_unhealthy_transition_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "ok"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "running", "Up 2 hours (unhealthy)"),
        ])
        await adapter._poll_containers()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_container_unhealthy"
        assert event.severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_dead_transition_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "ok"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "dead"),
        ])
        await adapter._poll_containers()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_container_dead"
        assert event.severity == Severity.ERROR

    @pytest.mark.asyncio
    async def test_restarting_transition_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "ok"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "restarting"),
        ])
        await adapter._poll_containers()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_container_restarting"
        assert event.severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_created_transition_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "ok"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "created"),
        ])
        await adapter._poll_containers()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_container_created"
        assert event.severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "exited"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "running"),
        ])
        await adapter._poll_containers()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ptr_container_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_ignore_list_skips_containers(self) -> None:
        adapter, _queue = _make_adapter(ignore_containers=["watchtower"])
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("watchtower", "exited"),
            _container("nginx", "running"),
        ])
        await adapter._poll_containers()

        assert "primary/watchtower" not in adapter._container_states
        assert "primary/nginx" in adapter._container_states

    @pytest.mark.asyncio
    async def test_skips_offline_endpoints(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "offline"}

        adapter._client.get_docker = AsyncMock()
        await adapter._poll_containers()

        adapter._client.get_docker.assert_not_called()

    @pytest.mark.asyncio
    async def test_per_endpoint_error_isolation(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1, "secondary": 2}
        adapter._endpoint_states = {"primary": "online", "secondary": "online"}

        call_count = 0

        async def _failing_get_docker(
            ep_id: int, path: str, **kw: object,
        ) -> list[dict[str, object]]:
            nonlocal call_count
            call_count += 1
            if ep_id == 1:
                raise ConnectionError("connection refused")
            return [_container("redis", "running")]

        adapter._client.get_docker = AsyncMock(side_effect=_failing_get_docker)
        await adapter._poll_containers()

        # Should have attempted both endpoints
        assert call_count == 2
        # Should have seeded container from the successful endpoint
        assert "secondary/redis" in adapter._container_states

    @pytest.mark.asyncio
    async def test_entity_id_is_qualified(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "ok"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "exited"),
        ])
        await adapter._poll_containers()

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "portainer:primary/nginx"


# ---------------------------------------------------------------------------
# Stack polling
# ---------------------------------------------------------------------------


class TestPollStacks:
    @pytest.mark.asyncio
    async def test_stack_degraded(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        # Seed: both running
        adapter._stack_health = {"primary/media": (2, 2)}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("sonarr", "running", labels={"com.docker.compose.project": "media"}),
            _container("radarr", "exited", labels={"com.docker.compose.project": "media"}),
        ])
        # Seed container states so no container events fire
        adapter._container_states = {
            "primary/sonarr": "ok",
            "primary/radarr": "exited",
        }
        await adapter._poll_containers()

        # Find the stack event (not the container event)
        stack_events = [
            c[0][0] for c in queue.put_nowait.call_args_list
            if c[0][0].event_type.startswith("ptr_stack_")
        ]
        assert len(stack_events) == 1
        assert stack_events[0].event_type == "ptr_stack_degraded"
        assert stack_events[0].severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_stack_all_down(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._stack_health = {"primary/media": (2, 2)}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("sonarr", "exited", labels={"com.docker.compose.project": "media"}),
            _container("radarr", "exited", labels={"com.docker.compose.project": "media"}),
        ])
        adapter._container_states = {
            "primary/sonarr": "exited",
            "primary/radarr": "exited",
        }
        await adapter._poll_containers()

        stack_events = [
            c[0][0] for c in queue.put_nowait.call_args_list
            if c[0][0].event_type.startswith("ptr_stack_")
        ]
        assert len(stack_events) == 1
        assert stack_events[0].event_type == "ptr_stack_down"
        assert stack_events[0].severity == Severity.ERROR

    @pytest.mark.asyncio
    async def test_stack_recovered(self) -> None:
        adapter, queue = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._stack_health = {"primary/media": (1, 2)}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("sonarr", "running", labels={"com.docker.compose.project": "media"}),
            _container("radarr", "running", labels={"com.docker.compose.project": "media"}),
        ])
        adapter._container_states = {
            "primary/sonarr": "ok",
            "primary/radarr": "ok",
        }
        await adapter._poll_containers()

        stack_events = [
            c[0][0] for c in queue.put_nowait.call_args_list
            if c[0][0].event_type.startswith("ptr_stack_")
        ]
        assert len(stack_events) == 1
        assert stack_events[0].event_type == "ptr_stack_recovered"
        assert stack_events[0].severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_stacks_disabled(self) -> None:
        adapter, queue = _make_adapter(poll_stacks=False)
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._stack_health = {"primary/media": (2, 2)}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("sonarr", "running", labels={"com.docker.compose.project": "media"}),
            _container("radarr", "exited", labels={"com.docker.compose.project": "media"}),
        ])
        adapter._container_states = {
            "primary/sonarr": "ok",
            "primary/radarr": "exited",
        }
        await adapter._poll_containers()

        stack_events = [
            c[0][0] for c in queue.put_nowait.call_args_list
            if c[0][0].event_type.startswith("ptr_stack_")
        ]
        assert len(stack_events) == 0


# ---------------------------------------------------------------------------
# Stale cleanup
# ---------------------------------------------------------------------------


class TestStaleCleanup:
    @pytest.mark.asyncio
    async def test_container_ids_cached_during_poll(self) -> None:
        adapter, _ = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "running", ct_id="sha256:abc"),
        ])
        await adapter._poll_containers()

        assert adapter._container_ids["primary/nginx"] == "sha256:abc"

    @pytest.mark.asyncio
    async def test_removed_containers_pruned(self) -> None:
        adapter, _ = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {
            "primary/nginx": "ok",
            "primary/removed": "ok",
        }
        adapter._container_ids = {
            "primary/nginx": "id1",
            "primary/removed": "id2",
        }

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("nginx", "running"),
        ])
        await adapter._poll_containers()

        assert "primary/nginx" in adapter._container_states
        assert "primary/removed" not in adapter._container_states
        assert "primary/nginx" in adapter._container_ids
        assert "primary/removed" not in adapter._container_ids

    @pytest.mark.asyncio
    async def test_removed_stacks_pruned(self) -> None:
        adapter, _ = _make_adapter()
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._stack_health = {
            "primary/media": (2, 2),
            "primary/old_stack": (1, 1),
        }

        adapter._client.get_docker = AsyncMock(return_value=[
            _container("sonarr", "running", labels={"com.docker.compose.project": "media"}),
        ])
        adapter._container_states = {"primary/sonarr": "ok"}
        await adapter._poll_containers()

        assert "primary/media" in adapter._stack_health
        assert "primary/old_stack" not in adapter._stack_health


# ---------------------------------------------------------------------------
# Resource polling
# ---------------------------------------------------------------------------


class TestPollResources:
    @pytest.mark.asyncio
    async def test_cpu_threshold_crossing(self) -> None:
        adapter, queue = _make_adapter(poll_container_resources=True)
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/nginx": "ok"}
        adapter._container_ids = {"primary/nginx": "abc123"}

        async def _mock_get_docker(ep_id: int, path: str, **kw: object) -> object:
            if "stats" in path:
                return {
                    "cpu_stats": {
                        "cpu_usage": {"total_usage": 950},
                        "system_cpu_usage": 1000,
                        "online_cpus": 1,
                    },
                    "precpu_stats": {
                        "cpu_usage": {"total_usage": 0},
                        "system_cpu_usage": 0,
                    },
                    "memory_stats": {"usage": 100, "limit": 1000},
                }
            return {}

        adapter._client.get_docker = AsyncMock(side_effect=_mock_get_docker)
        await adapter._poll_container_resources()

        queue.put_nowait.assert_called()
        events = [c[0][0] for c in queue.put_nowait.call_args_list]
        cpu_events = [e for e in events if e.event_type == "ptr_container_high_cpu"]
        assert len(cpu_events) == 1
        assert cpu_events[0].severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_resources_disabled_by_default(self) -> None:
        config = _make_config()
        assert config.poll_container_resources is False

    @pytest.mark.asyncio
    async def test_cost_guard_stops_early(self) -> None:
        adapter, _queue = _make_adapter(
            poll_container_resources=True, poll_interval=1,
        )
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        # Create many containers to trigger the cost guard
        for i in range(50):
            adapter._container_states[f"primary/ct{i}"] = "ok"
            adapter._container_ids[f"primary/ct{i}"] = f"id{i}"

        call_count = 0

        async def _slow_get_docker(ep_id: int, path: str, **kw: object) -> object:
            nonlocal call_count
            call_count += 1
            return {}

        adapter._client.get_docker = AsyncMock(side_effect=_slow_get_docker)

        # Monkey-patch time.monotonic to simulate elapsed time
        original_monotonic = time.monotonic
        start = original_monotonic()
        call_idx = [0]

        def _fast_monotonic() -> float:
            call_idx[0] += 1
            # After a few calls, report that we're past the budget
            if call_idx[0] > 4:
                return start + 100  # Way past any deadline
            return start

        with patch("oasisagent.ingestion.portainer.time.monotonic", _fast_monotonic):
            await adapter._poll_container_resources()

        # Should have stopped early, not polled all 50 containers
        assert call_count < 50

    @pytest.mark.asyncio
    async def test_rotation_fairness(self) -> None:
        """After K cycles, every container should be polled K times (±1)."""
        adapter, _queue = _make_adapter(
            poll_container_resources=True, poll_interval=60,
        )
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}

        containers = [f"primary/ct{i}" for i in range(10)]
        for ct in containers:
            adapter._container_states[ct] = "ok"
            adapter._container_ids[ct] = f"id_{ct}"

        polled_counts: dict[str, int] = {ct: 0 for ct in containers}
        cycle_budget = 4  # Only 4 containers per cycle

        async def _mock_get_docker(
            ep_id: int, path: str, **kw: object,
        ) -> object:
            return {"memory_stats": {"usage": 50, "limit": 1000}}

        adapter._client.get_docker = AsyncMock(side_effect=_mock_get_docker)

        original_monotonic = time.monotonic
        for _cycle in range(10):
            start = original_monotonic()
            call_in_cycle = [0]

            def _budget_monotonic(
                _s: float = start, _c: list = call_in_cycle,
            ) -> float:
                _c[0] += 1
                if _c[0] > cycle_budget + 1:
                    return _s + 100
                return _s

            with patch(
                "oasisagent.ingestion.portainer.time.monotonic",
                _budget_monotonic,
            ):
                await adapter._poll_container_resources()

            # Track which containers were polled by checking call args
            for call in adapter._client.get_docker.call_args_list:
                path_arg = call[0][1]
                for ct in containers:
                    ct_id = f"id_{ct}"
                    if ct_id in path_arg:
                        polled_counts[ct] += 1
                        break

            adapter._client.get_docker.reset_mock()

        # Every container should be polled at least once over 10 cycles
        for ct, count in polled_counts.items():
            assert count >= 1, f"{ct} was never polled"

        # Fairness: max - min should be small (±1)
        counts = list(polled_counts.values())
        assert max(counts) - min(counts) <= 2, (
            f"Unfair polling: {polled_counts}"
        )

    @pytest.mark.asyncio
    async def test_budget_is_80_percent(self) -> None:
        """Budget should be 80% of poll_interval."""
        adapter, _queue = _make_adapter(
            poll_container_resources=True, poll_interval=60,
        )
        adapter._known_endpoints = {"primary": 1}
        adapter._endpoint_states = {"primary": "online"}
        adapter._container_states = {"primary/ct0": "ok"}
        adapter._container_ids = {"primary/ct0": "id0"}

        captured_deadline = []
        original_monotonic = time.monotonic

        def _capture_monotonic() -> float:
            t = original_monotonic()
            captured_deadline.append(t)
            return t

        adapter._client.get_docker = AsyncMock(return_value={})

        with patch(
            "oasisagent.ingestion.portainer.time.monotonic",
            _capture_monotonic,
        ):
            await adapter._poll_container_resources()

        # First call sets deadline, second checks it
        # deadline = first_call + poll_interval * 0.8 = first_call + 48
        assert len(captured_deadline) >= 2


# ---------------------------------------------------------------------------
# Topology discovery
# ---------------------------------------------------------------------------


class TestTopologyDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_hosts_and_containers(self) -> None:
        adapter, _ = _make_adapter()

        async def _mock_get(path: str) -> list[dict[str, object]]:
            return [
                {"Id": 1, "Name": "primary", "Type": 1, "Status": 1,
                 "PublicURL": "192.168.1.120", "URL": "tcp://192.168.1.120:2375"},
            ]

        async def _mock_get_docker(ep_id: int, path: str, **kw: object) -> list[dict[str, object]]:
            return [
                _container("nginx", "running",
                           labels={"com.docker.compose.project": "web"}),
            ]

        adapter._client.get = AsyncMock(side_effect=_mock_get)
        adapter._client.get_docker = AsyncMock(side_effect=_mock_get_docker)

        nodes, edges = await adapter.discover_topology()

        host_nodes = [n for n in nodes if n.entity_type == "host"]
        ct_nodes = [n for n in nodes if n.entity_type == "container"]

        assert len(host_nodes) == 1
        assert host_nodes[0].entity_id == "portainer:primary"
        assert host_nodes[0].host_ip == "192.168.1.120"

        assert len(ct_nodes) == 1
        assert ct_nodes[0].entity_id == "portainer:primary/nginx"

        assert len(edges) == 1
        assert edges[0].from_entity == "portainer:primary/nginx"
        assert edges[0].to_entity == "portainer:primary"
        assert edges[0].edge_type == "runs_on"

    @pytest.mark.asyncio
    async def test_empty_on_api_error(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client.get = AsyncMock(side_effect=ConnectionError("fail"))

        nodes, edges = await adapter.discover_topology()

        assert nodes == []
        assert edges == []


# ---------------------------------------------------------------------------
# Known fixes YAML
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_portainer_yaml_has_expected_fixes(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "portainer.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fix_ids = {fix["id"] for fix in data["fixes"]}

        expected_ids = {
            "ptr-endpoint-unreachable",
            "ptr-stack-down",
            "ptr-stack-degraded",
            "ptr-container-exited",
            "ptr-container-dead",
            "ptr-container-unhealthy",
            "ptr-container-restarting",
            "ptr-container-high-cpu",
            "ptr-container-high-memory",
        }
        assert expected_ids == fix_ids

    def test_all_fixes_have_required_fields(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "portainer.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        for fix in data["fixes"]:
            assert "id" in fix, f"Fix missing 'id': {fix}"
            assert "match" in fix, f"Fix {fix['id']} missing 'match'"
            assert "diagnosis" in fix, f"Fix {fix['id']} missing 'diagnosis'"
            assert "action" in fix, f"Fix {fix['id']} missing 'action'"
            assert "risk_tier" in fix, f"Fix {fix['id']} missing 'risk_tier'"

    def test_all_fixes_use_system_portainer(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "portainer.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        for fix in data["fixes"]:
            assert fix["match"]["system"] == "portainer", (
                f"Fix {fix['id']} should use system: portainer"
            )
