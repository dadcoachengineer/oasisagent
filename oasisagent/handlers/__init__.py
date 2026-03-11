"""System handlers — execute actions against managed infrastructure."""

from oasisagent.handlers.base import Handler
from oasisagent.handlers.docker import DockerHandler
from oasisagent.handlers.homeassistant import HomeAssistantHandler
from oasisagent.handlers.portainer import PortainerHandler
from oasisagent.handlers.proxmox import ProxmoxHandler

__all__ = [
    "DockerHandler",
    "Handler",
    "HomeAssistantHandler",
    "PortainerHandler",
    "ProxmoxHandler",
]
