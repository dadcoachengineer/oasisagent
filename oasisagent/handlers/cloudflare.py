"""Cloudflare handler — executes actions via the Cloudflare API v4.

Operations: notify, purge_cache, purge_urls, block_ip, unblock_ip.

Uses the shared CloudflareClient for bearer token auth.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.clients.cloudflare import CloudflareClient
from oasisagent.handlers.base import Handler
from oasisagent.models import ActionResult, ActionStatus, VerifyResult

if TYPE_CHECKING:
    from oasisagent.config import CloudflareHandlerConfig
    from oasisagent.models import Event, RecommendedAction

logger = logging.getLogger(__name__)

_KNOWN_OPERATIONS: frozenset[str] = frozenset({
    "notify",
    "purge_cache",
    "purge_urls",
    "block_ip",
    "unblock_ip",
})


class CloudflareHandler(Handler):
    """Executes actions against the Cloudflare API v4.

    Must be started with ``await handler.start()`` before use.
    The CloudflareClient session is created in start() and closed in stop().
    """

    def __init__(self, config: CloudflareHandlerConfig) -> None:
        self._config = config
        self._client: CloudflareClient | None = None

    def name(self) -> str:
        return "cloudflare"

    async def start(self) -> None:
        """Create the Cloudflare client and open the HTTP session."""
        self._client = CloudflareClient(
            api_token=self._config.api_token,
            timeout=self._config.timeout,
        )
        await self._client.start()
        logger.info("Cloudflare handler started")

    async def stop(self) -> None:
        """Close the Cloudflare client session."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("Cloudflare handler stopped")

    async def can_handle(
        self, event: Event, action: RecommendedAction,
    ) -> bool:
        return (
            action.handler == "cloudflare"
            and action.operation in _KNOWN_OPERATIONS
        )

    async def execute(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Dispatch to the appropriate operation method."""
        self._ensure_started()

        dispatch = {
            "notify": self._op_notify,
            "purge_cache": self._op_purge_cache,
            "purge_urls": self._op_purge_urls,
            "block_ip": self._op_block_ip,
            "unblock_ip": self._op_unblock_ip,
        }

        handler_fn = dispatch.get(action.operation)
        if handler_fn is None:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"Unknown operation: {action.operation}",
            )

        start = time.monotonic()
        try:
            result = await handler_fn(event, action)
            elapsed = (time.monotonic() - start) * 1000
            return result.model_copy(update={"duration_ms": elapsed})
        except aiohttp.ClientError as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.error(
                "Cloudflare handler HTTP error for %s: %s",
                action.operation, exc,
            )
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=f"HTTP error: {exc}",
                duration_ms=elapsed,
            )

    async def verify(
        self, event: Event, action: RecommendedAction, result: ActionResult,
    ) -> VerifyResult:
        """Verify an action had the desired effect.

        Cloudflare operations are fire-and-forget — no polling verification.
        purge_cache and purge_urls return verified=True on success.
        block_ip verifies the rule exists in the firewall.
        """
        if action.operation == "block_ip" and result.status == ActionStatus.SUCCESS:
            return await self._verify_block_ip(result)

        return VerifyResult(
            verified=True, message="No verification needed",
        )

    async def get_context(self, event: Event) -> dict[str, Any]:
        """Gather Cloudflare-specific context for diagnosis."""
        self._ensure_started()
        assert self._client is not None
        context: dict[str, Any] = {}

        # Zone details
        if self._config.zone_id:
            try:
                data = await self._client.get(
                    f"/zones/{self._config.zone_id}",
                )
                zone = data.get("result", {})
                context["zone"] = {
                    "name": zone.get("name", ""),
                    "status": zone.get("status", ""),
                    "paused": zone.get("paused", False),
                    "plan": zone.get("plan", {}).get("name", ""),
                }
            except aiohttp.ClientError as exc:
                context["zone_error"] = str(exc)

        # Tunnel status
        if self._config.account_id:
            try:
                data = await self._client.get(
                    f"/accounts/{self._config.account_id}/cfd_tunnel",
                )
                tunnels = data.get("result", [])
                context["tunnels"] = [
                    {
                        "id": t.get("id", ""),
                        "name": t.get("name", ""),
                        "status": t.get("status", ""),
                    }
                    for t in tunnels
                ]
            except aiohttp.ClientError as exc:
                context["tunnel_error"] = str(exc)

        return context

    # -------------------------------------------------------------------
    # Operation implementations
    # -------------------------------------------------------------------

    async def _op_notify(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Notify — no system changes. Returns the diagnosis message."""
        message = action.params.get("message", action.description)
        logger.info(
            "Cloudflare notify: %s (entity=%s)", message, event.entity_id,
        )
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"message": message, "entity_id": event.entity_id},
        )

    async def _op_purge_cache(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Purge entire cache for the configured zone."""
        assert self._client is not None
        zone_id = action.params.get("zone_id", self._config.zone_id)
        if not zone_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="purge_cache requires 'zone_id' in params or config",
            )

        await self._client.post(
            f"/zones/{zone_id}/purge_cache",
            {"purge_everything": True},
        )

        logger.info("Cloudflare purge_cache: zone=%s", zone_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={"zone_id": zone_id, "operation": "purge_cache"},
        )

    async def _op_purge_urls(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Purge specific URLs from the cache."""
        assert self._client is not None
        zone_id = action.params.get("zone_id", self._config.zone_id)
        if not zone_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="purge_urls requires 'zone_id' in params or config",
            )

        urls = action.params.get("urls", [])
        if not urls:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="purge_urls requires 'urls' list in params",
            )

        await self._client.post(
            f"/zones/{zone_id}/purge_cache",
            {"files": urls},
        )

        logger.info(
            "Cloudflare purge_urls: zone=%s, count=%d", zone_id, len(urls),
        )
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={
                "zone_id": zone_id,
                "operation": "purge_urls",
                "url_count": len(urls),
            },
        )

    async def _op_block_ip(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Block an IP via Cloudflare firewall access rule."""
        assert self._client is not None
        ip = action.params.get("ip")
        if not ip:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="block_ip requires 'ip' in params",
            )

        note = action.params.get(
            "note", f"Blocked by OasisAgent: {action.description}",
        )

        data = await self._client.post(
            f"/accounts/{self._config.account_id}/firewall/access_rules/rules",
            {
                "mode": "block",
                "configuration": {"target": "ip", "value": ip},
                "notes": note,
            },
        )

        rule_id = data.get("result", {}).get("id", "")
        logger.info("Cloudflare block_ip: ip=%s, rule_id=%s", ip, rule_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={
                "ip": ip,
                "operation": "block_ip",
                "rule_id": rule_id,
            },
        )

    async def _op_unblock_ip(
        self, event: Event, action: RecommendedAction,
    ) -> ActionResult:
        """Remove a firewall access rule to unblock an IP."""
        assert self._client is not None
        rule_id = action.params.get("rule_id")
        if not rule_id:
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message="unblock_ip requires 'rule_id' in params",
            )

        await self._client.delete(
            f"/accounts/{self._config.account_id}/firewall/access_rules/rules/{rule_id}",
        )

        logger.info("Cloudflare unblock_ip: rule_id=%s", rule_id)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            details={
                "rule_id": rule_id,
                "operation": "unblock_ip",
            },
        )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the handler hasn't been started."""
        if self._client is None:
            msg = "CloudflareHandler.start() must be called before use"
            raise RuntimeError(msg)

    async def _verify_block_ip(self, result: ActionResult) -> VerifyResult:
        """Verify a block_ip rule was created by checking it exists."""
        assert self._client is not None
        rule_id = (result.details or {}).get("rule_id", "")
        if not rule_id:
            return VerifyResult(
                verified=False,
                message="No rule_id returned from block_ip",
            )

        try:
            data = await self._client.get(
                f"/accounts/{self._config.account_id}"
                f"/firewall/access_rules/rules/{rule_id}",
            )
            rule = data.get("result", {})
            if rule.get("id") == rule_id:
                return VerifyResult(
                    verified=True,
                    message=f"Firewall rule {rule_id} confirmed",
                )
        except aiohttp.ClientError as exc:
            return VerifyResult(
                verified=False,
                message=f"Failed to verify rule {rule_id}: {exc}",
            )

        return VerifyResult(
            verified=False,
            message=f"Rule {rule_id} not found after creation",
        )
