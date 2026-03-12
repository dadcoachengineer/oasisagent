"""Tests for the Docker container health scanner."""

from __future__ import annotations

from oasisagent.config import DockerHandlerConfig, DockerHealthCheckConfig
from oasisagent.models import Severity
from oasisagent.scanner.docker_health import DockerHealthScannerAdapter


def _make_config(**overrides: object) -> DockerHealthCheckConfig:
    defaults: dict = {
        "enabled": True,
        "ignore_containers": [],
        "interval": 60,
    }
    defaults.update(overrides)
    return DockerHealthCheckConfig(**defaults)


def _make_docker_config(**overrides: object) -> DockerHandlerConfig:
    defaults: dict = {
        "enabled": True,
        "socket": "unix:///var/run/docker.sock",
    }
    defaults.update(overrides)
    return DockerHandlerConfig(**defaults)


def _mock_queue():  # noqa: ANN202
    from unittest.mock import MagicMock

    return MagicMock()


def _make_scanner(
    config: DockerHealthCheckConfig | None = None,
    docker_config: DockerHandlerConfig | None = None,
) -> DockerHealthScannerAdapter:
    return DockerHealthScannerAdapter(
        config=config or _make_config(),
        queue=_mock_queue(),
        interval=60,
        docker_config=docker_config or _make_docker_config(),
    )


def _make_container(
    name: str = "oasis-agent",
    state: str = "running",
    status: str = "Up 2 hours",
    image: str = "oasisagent:latest",
) -> dict:
    return {
        "Names": [f"/{name}"],
        "State": state,
        "Status": status,
        "Image": image,
        "Id": "abc123def456",
    }


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestDockerHealthConfig:
    def test_defaults(self) -> None:
        cfg = DockerHealthCheckConfig()
        assert cfg.enabled is False
        assert cfg.ignore_containers == []
        assert cfg.interval == 900


# ---------------------------------------------------------------------------
# Name property
# ---------------------------------------------------------------------------


class TestDockerHealthScanner:
    def test_name(self) -> None:
        scanner = _make_scanner()
        assert scanner.name == "scanner.docker_health"


# ---------------------------------------------------------------------------
# State-based dedup (evaluate_containers)
# ---------------------------------------------------------------------------


class TestDockerHealthEvaluate:
    def test_running_container_first_poll_no_event(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([_make_container()])
        assert events == []

    def test_exited_container_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(state="exited", status="Exited (1) 5 minutes ago"),
        ])
        assert len(events) == 1
        assert events[0].event_type == "container_exited"
        assert events[0].severity == Severity.ERROR
        assert events[0].entity_id == "oasis-agent"

    def test_unhealthy_container_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(status="Up 2 hours (unhealthy)"),
        ])
        assert len(events) == 1
        assert events[0].event_type == "container_unhealthy"
        assert events[0].severity == Severity.WARNING

    def test_restarting_container_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(state="restarting"),
        ])
        assert len(events) == 1
        assert events[0].event_type == "container_restarting"
        assert events[0].severity == Severity.WARNING

    def test_dead_container_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(state="dead"),
        ])
        assert len(events) == 1
        assert events[0].severity == Severity.ERROR

    def test_same_state_dedup(self) -> None:
        scanner = _make_scanner()
        container = _make_container(state="exited", status="Exited (1)")
        events1 = scanner._evaluate_containers([container])
        events2 = scanner._evaluate_containers([container])
        assert len(events1) == 1
        assert len(events2) == 0

    def test_recovery_emits(self) -> None:
        scanner = _make_scanner()
        scanner._evaluate_containers([
            _make_container(state="exited", status="Exited (1)"),
        ])
        events = scanner._evaluate_containers([
            _make_container(state="running", status="Up 1 minute"),
        ])
        assert len(events) == 1
        assert events[0].event_type == "container_recovered"
        assert events[0].severity == Severity.INFO
        assert events[0].payload["previous_state"] == "exited"

    def test_ignore_containers(self) -> None:
        scanner = _make_scanner(
            config=_make_config(ignore_containers=["temp-builder"]),
        )
        events = scanner._evaluate_containers([
            _make_container(name="temp-builder", state="exited"),
        ])
        assert events == []

    def test_dedup_key_includes_name(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(name="redis", state="exited", status="Exited"),
        ])
        assert events[0].metadata.dedup_key == "scanner.docker_health:redis"

    def test_multiple_containers_tracked_independently(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(name="redis", state="exited", status="Exited"),
            _make_container(name="postgres", state="running"),
        ])
        assert len(events) == 1
        assert events[0].entity_id == "redis"

    def test_healthy_status_not_alerted(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(status="Up 2 hours (healthy)"),
        ])
        assert events == []

    def test_source_and_system(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(state="exited", status="Exited"),
        ])
        assert events[0].source == "scanner.docker_health"
        assert events[0].system == "docker"

    def test_payload_includes_image(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_containers([
            _make_container(
                state="exited", status="Exited", image="redis:7-alpine",
            ),
        ])
        assert events[0].payload["image"] == "redis:7-alpine"

    def test_container_without_name_uses_id(self) -> None:
        scanner = _make_scanner()
        container = {
            "Names": [],
            "State": "exited",
            "Status": "Exited (1)",
            "Image": "test:latest",
            "Id": "abc123def456789",
        }
        events = scanner._evaluate_containers([container])
        assert len(events) == 1
        assert events[0].entity_id == "abc123def456"  # truncated to 12 chars
