"""Approval queue CLI — stateless operator commands via MQTT.

Connects directly to the MQTT broker to list, show, approve, and reject
pending actions. Does not require the agent process to be running (reads
retained messages). All commands are fire-and-forget publishes or
single-message subscribes with timeout.

ARCHITECTURE.md §16.3 describes the CLI specification.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    import argparse

import aiomqtt

# Timeout for subscribing to retained messages (seconds)
_SUBSCRIBE_TIMEOUT = 3.0

# Default broker URL
_DEFAULT_BROKER = "mqtt://localhost:1883"


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------


def _parse_broker(broker: str) -> tuple[str, int]:
    """Parse a broker URL into (hostname, port)."""
    parsed = urlparse(broker)
    return parsed.hostname or "localhost", parsed.port or 1883


async def _read_retained(
    broker: str,
    username: str | None,
    password: str | None,
    topic: str,
) -> str | None:
    """Subscribe to a topic and read a single retained message.

    Returns the payload as a string, or None if no retained message
    arrives within the timeout.
    """
    hostname, port = _parse_broker(broker)

    async with aiomqtt.Client(
        hostname=hostname,
        port=port,
        username=username or None,
        password=password or None,
        identifier="oasisagent-cli",
    ) as client:
        await client.subscribe(topic, qos=1)

        try:
            async with asyncio.timeout(_SUBSCRIBE_TIMEOUT):
                async for message in client.messages:
                    payload = message.payload
                    if isinstance(payload, (bytes, bytearray)):
                        return payload.decode("utf-8")
                    if isinstance(payload, str):
                        return payload
                    return None
        except TimeoutError:
            return None

    return None


async def _publish(
    broker: str,
    username: str | None,
    password: str | None,
    topic: str,
    payload: str = "",
) -> None:
    """Publish a message to an MQTT topic."""
    hostname, port = _parse_broker(broker)

    async with aiomqtt.Client(
        hostname=hostname,
        port=port,
        username=username or None,
        password=password or None,
        identifier="oasisagent-cli",
    ) as client:
        await client.publish(topic, payload=payload, qos=1)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_table(actions: list[dict[str, Any]]) -> str:
    """Format pending actions as a table."""
    if not actions:
        return "No pending actions."

    headers = ["ID", "DIAGNOSIS", "HANDLER", "OPERATION", "EXPIRES"]
    rows: list[list[str]] = []

    for a in actions:
        action_data = a.get("action", {})
        expires = a.get("expires_at", "")
        if isinstance(expires, str) and len(expires) > 19:
            expires = expires[:19] + "Z"

        rows.append([
            a.get("id", "")[:36],
            (a.get("diagnosis", "") or "")[:40],
            (action_data.get("handler", "") or "")[:20],
            (action_data.get("operation", "") or "")[:20],
            expires,
        ])

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build output
    lines: list[str] = []
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)

    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))

    return "\n".join(lines)


def _format_detail(action: dict[str, Any]) -> str:
    """Format a single pending action as readable JSON."""
    return json.dumps(action, indent=2, default=str)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def _cmd_list(broker: str, username: str | None, password: str | None) -> int:
    """List all pending actions."""
    payload = await _read_retained(broker, username, password, "oasis/pending/list")

    if payload is None or payload.strip() == "":
        print("No pending actions.")
        return 0

    try:
        actions = json.loads(payload)
    except json.JSONDecodeError:
        print("Error: invalid JSON from oasis/pending/list", file=sys.stderr)
        return 1

    if not isinstance(actions, list):
        print("Error: expected JSON array from oasis/pending/list", file=sys.stderr)
        return 1

    print(_format_table(actions))
    return 0


async def _cmd_show(
    broker: str, username: str | None, password: str | None, action_id: str
) -> int:
    """Show details of a specific pending action."""
    topic = f"oasis/pending/{action_id}"
    payload = await _read_retained(broker, username, password, topic)

    if payload is None or payload.strip() == "":
        print(
            f"Action {action_id} not found or already resolved.",
            file=sys.stderr,
        )
        return 1

    try:
        action = json.loads(payload)
    except json.JSONDecodeError:
        print(f"Error: invalid JSON for action {action_id}", file=sys.stderr)
        return 1

    print(_format_detail(action))
    return 0


async def _cmd_approve(
    broker: str, username: str | None, password: str | None, action_id: str
) -> int:
    """Send an approval request for a pending action."""
    await _publish(broker, username, password, f"oasis/approve/{action_id}")
    print(f"Approval request sent for {action_id}")
    return 0


async def _cmd_reject(
    broker: str, username: str | None, password: str | None, action_id: str
) -> int:
    """Send a rejection request for a pending action."""
    await _publish(broker, username, password, f"oasis/reject/{action_id}")
    print(f"Rejection request sent for {action_id}")
    return 0


async def _cmd_approve_all(
    broker: str, username: str | None, password: str | None
) -> int:
    """Approve all pending actions after confirmation."""
    payload = await _read_retained(broker, username, password, "oasis/pending/list")

    if payload is None or payload.strip() == "":
        print("No pending actions.")
        return 0

    try:
        actions = json.loads(payload)
    except json.JSONDecodeError:
        print("Error: invalid JSON from oasis/pending/list", file=sys.stderr)
        return 1

    if not isinstance(actions, list) or not actions:
        print("No pending actions.")
        return 0

    print(_format_table(actions))
    print(f"\nApprove all {len(actions)} pending actions? [y/N] ", end="", flush=True)

    try:
        response = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if response != "y":
        print("Aborted.")
        return 1

    for action in actions:
        action_id = action.get("id", "")
        if action_id:
            await _publish(
                broker, username, password, f"oasis/approve/{action_id}"
            )
            print(f"Approval request sent for {action_id}")

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _env_default(env_var: str, default: str) -> str:
    """Get a default value from env var, falling back to hardcoded default.

    Precedence: --flag > env var > hardcoded default.
    """
    return os.environ.get(env_var, default)


def build_queue_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the 'queue' sub-command group to the argument parser."""
    queue_parser = subparsers.add_parser(
        "queue",
        help="Manage the pending approval queue",
    )

    # Shared MQTT connection flags
    queue_parser.add_argument(
        "--broker",
        default=_env_default("OASIS_MQTT_BROKER", _DEFAULT_BROKER),
        help=(
            "MQTT broker URL (default: $OASIS_MQTT_BROKER or "
            f"{_DEFAULT_BROKER})"
        ),
    )
    queue_parser.add_argument(
        "--username",
        default=_env_default("OASIS_MQTT_USERNAME", ""),
        help="MQTT username (default: $OASIS_MQTT_USERNAME)",
    )
    queue_parser.add_argument(
        "--password",
        default=_env_default("OASIS_MQTT_PASSWORD", ""),
        help="MQTT password (default: $OASIS_MQTT_PASSWORD)",
    )

    queue_sub = queue_parser.add_subparsers(dest="queue_command")

    queue_sub.add_parser("list", help="List all pending actions")

    show_parser = queue_sub.add_parser("show", help="Show details of a pending action")
    show_parser.add_argument("action_id", help="Pending action ID")

    approve_parser = queue_sub.add_parser("approve", help="Approve a pending action")
    approve_parser.add_argument("action_id", help="Pending action ID to approve")

    reject_parser = queue_sub.add_parser("reject", help="Reject a pending action")
    reject_parser.add_argument("action_id", help="Pending action ID to reject")

    queue_sub.add_parser("approve-all", help="Approve all pending actions")


def run_queue_command(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate queue sub-command."""
    broker = args.broker
    username = args.username or None
    password = args.password or None

    try:
        if args.queue_command == "list":
            exit_code = asyncio.run(_cmd_list(broker, username, password))
        elif args.queue_command == "show":
            exit_code = asyncio.run(
                _cmd_show(broker, username, password, args.action_id)
            )
        elif args.queue_command == "approve":
            exit_code = asyncio.run(
                _cmd_approve(broker, username, password, args.action_id)
            )
        elif args.queue_command == "reject":
            exit_code = asyncio.run(
                _cmd_reject(broker, username, password, args.action_id)
            )
        elif args.queue_command == "approve-all":
            exit_code = asyncio.run(
                _cmd_approve_all(broker, username, password)
            )
        else:
            print("Usage: oasisagent queue {list,show,approve,reject,approve-all}")
            exit_code = 1
    except aiomqtt.MqttError as exc:
        print(f"MQTT connection error: {exc}", file=sys.stderr)
        exit_code = 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        exit_code = 1

    sys.exit(exit_code)
