#!/usr/bin/env python3
"""Smoke test — validate live connectivity and the event pipeline.

Loads config, connects to HA WebSocket + MQTT + HA Log Poller,
collects events for a configurable duration, runs them through T0
known fixes matching, and prints results.

Usage:
    python scripts/smoke_test.py [--duration 30]

Requires .env and config.yaml in the project root.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from argparse import ArgumentParser
from datetime import UTC, datetime
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from oasisagent.config import load_config
from oasisagent.engine.known_fixes import KnownFixRegistry
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.ha_log_poller import HaLogPollerAdapter
from oasisagent.ingestion.ha_websocket import HaWebSocketAdapter
from oasisagent.ingestion.mqtt import MqttAdapter

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("smoke_test")


def parse_args() -> int:
    parser = ArgumentParser(description="OasisAgent smoke test")
    parser.add_argument(
        "--duration", type=int, default=30,
        help="Seconds to collect events (default: 30)",
    )
    return parser.parse_args().duration


async def run_smoke_test(duration: int) -> None:
    # --- Load environment and config ---
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("Loaded .env from %s", env_path)

    config_path = project_root / "config.yaml"
    logger.info("Loading config from %s", config_path)
    config = load_config(config_path)
    logger.info("Config loaded: agent=%s, log_level=%s", config.agent.name, config.agent.log_level)

    # --- Load T0 known fixes ---
    registry = KnownFixRegistry()
    fixes_dir = project_root / config.agent.known_fixes_dir
    registry.load(fixes_dir)
    logger.info("T0 registry: %d known fixes loaded", len(registry.fixes))

    # --- Create event queue ---
    queue = EventQueue(
        max_size=config.agent.event_queue_size,
        dedup_window_seconds=config.agent.dedup_window_seconds,
    )
    logger.info("Event queue created (max_size=%d)", config.agent.event_queue_size)

    # --- Start adapters ---
    adapters = []
    tasks = []

    if config.ingestion.ha_websocket.enabled:
        ws_adapter = HaWebSocketAdapter(config.ingestion.ha_websocket, queue)
        adapters.append(ws_adapter)
        tasks.append(asyncio.create_task(ws_adapter.start(), name="ha_websocket"))
        logger.info("Starting HA WebSocket adapter → %s", config.ingestion.ha_websocket.url)

    if config.ingestion.mqtt.enabled:
        mqtt_adapter = MqttAdapter(config.ingestion.mqtt, queue)
        adapters.append(mqtt_adapter)
        tasks.append(asyncio.create_task(mqtt_adapter.start(), name="mqtt"))
        logger.info("Starting MQTT adapter → %s", config.ingestion.mqtt.broker)

    if config.ingestion.ha_log_poller.enabled:
        log_adapter = HaLogPollerAdapter(config.ingestion.ha_log_poller, queue)
        adapters.append(log_adapter)
        tasks.append(asyncio.create_task(log_adapter.start(), name="ha_log_poller"))
        logger.info("Starting HA Log Poller → %s", config.ingestion.ha_log_poller.url)

    if not adapters:
        logger.error("No adapters enabled in config — nothing to test")
        return

    # --- Wait for connections, then collect events ---
    logger.info("Waiting 3s for adapters to connect...")
    await asyncio.sleep(3)

    # Health check
    for adapter in adapters:
        healthy = await adapter.healthy()
        status = "CONNECTED" if healthy else "NOT CONNECTED"
        logger.info("  %s: %s", adapter.name, status)

    logger.info("Collecting events for %d seconds...", duration)
    await asyncio.sleep(duration)

    # --- Stop adapters ---
    logger.info("Stopping adapters...")
    for adapter in adapters:
        await adapter.stop()

    # Give tasks a moment to finish
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # --- Drain and analyze ---
    events = queue.drain()
    logger.info("=" * 70)
    logger.info("RESULTS: %d events collected in %d seconds", len(events), duration)
    logger.info("=" * 70)

    if not events:
        logger.info("No events received. This could mean:")
        logger.info("  - HA is running normally (no errors/state changes)")
        logger.info("  - Connection failed (check logs above)")
        logger.info("  - Adapters need more time to receive events")
        return

    # Categorize events
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    t0_matches = 0

    for event in events:
        by_source[event.source] = by_source.get(event.source, 0) + 1
        by_type[event.event_type] = by_type.get(event.event_type, 0) + 1

        # Run T0 matching
        fix = registry.match(event)
        if fix:
            t0_matches += 1
            logger.info(
                "  T0 MATCH: event=%s entity=%s → fix=%s diagnosis=%s",
                event.event_type,
                event.entity_id,
                fix.id,
                fix.diagnosis[:80],
            )

    logger.info("")
    logger.info("Events by source:")
    for source, count in sorted(by_source.items()):
        logger.info("  %s: %d", source, count)

    logger.info("Events by type:")
    for etype, count in sorted(by_type.items()):
        logger.info("  %s: %d", etype, count)

    logger.info("")
    logger.info("T0 matches: %d / %d events", t0_matches, len(events))

    # Print first 5 events as samples
    logger.info("")
    logger.info("Sample events (first 5):")
    for event in events[:5]:
        logger.info(
            "  [%s] %s | %s | entity=%s | severity=%s",
            event.source,
            event.event_type,
            event.system,
            event.entity_id,
            event.severity,
        )
        if event.payload:
            # Truncate payload for readability
            payload_str = str(event.payload)
            if len(payload_str) > 120:
                payload_str = payload_str[:120] + "..."
            logger.info("    payload: %s", payload_str)


def main() -> None:
    duration = parse_args()
    logger.info("OasisAgent Smoke Test — %s", datetime.now(UTC).isoformat())
    logger.info("Duration: %d seconds", duration)

    try:
        asyncio.run(run_smoke_test(duration))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
