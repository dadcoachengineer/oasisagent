"""Telegram notification + approval channel via aiogram.

Sends notifications as formatted HTML messages and approval requests
with inline keyboard buttons (Approve / Reject). Listens for button
presses and the /status command via long-polling.

Uses aiogram v3 which is fully asyncio-native — the polling loop runs
as an asyncio.Task inside the existing event loop (single-process).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from oasisagent.approval.pending import ApprovalDecision, PendingStatus
from oasisagent.notifications.interactive import InteractiveNotificationChannel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from oasisagent.approval.pending import PendingAction
    from oasisagent.config import TelegramNotificationConfig
    from oasisagent.models import Notification

logger = logging.getLogger(__name__)

# Telegram API rate limit: 1 msg/sec per chat
_MIN_SEND_INTERVAL = 1.0

# Severity → emoji mapping for notifications
_SEVERITY_EMOJI = {
    "critical": "\U0001f534",  # 🔴
    "warning": "\U0001f7e0",   # 🟠
    "info": "\U0001f535",      # 🔵
    "debug": "\u26aa",         # ⚪
}

# Callback data prefixes (must fit in 64 bytes with UUID)
_CB_APPROVE = "approve:"
_CB_REJECT = "reject:"


class TelegramChannel(InteractiveNotificationChannel):
    """Telegram notification channel with interactive approval buttons.

    Sends notifications to a configured chat and listens for approval
    responses via inline keyboard callbacks and the /status command.
    """

    def __init__(self, config: TelegramNotificationConfig) -> None:
        self._config = config
        self._bot: Bot | None = None
        self._dispatcher: Dispatcher | None = None
        self._router = Router(name="oasis")
        self._polling_task: asyncio.Task[None] | None = None
        self._approval_callback: Callable[[str, ApprovalDecision], Awaitable[None]] | None = None
        self._last_send_time: float = 0.0

        # Track sent approval messages: action_id → telegram message_id
        self._approval_messages: dict[str, int] = {}

        # Register handlers on the router
        self._router.callback_query(F.data.startswith(_CB_APPROVE))(self._handle_approve)
        self._router.callback_query(F.data.startswith(_CB_REJECT))(self._handle_reject)
        self._router.message(F.text == "/status")(self._handle_status)

    def name(self) -> str:
        return "telegram"

    async def healthy(self) -> bool:
        """Check bot health by calling getMe."""
        if not self._config.enabled or self._bot is None:
            return True  # disabled = healthy (no-op)
        try:
            await self._bot.me()
            return True
        except Exception:
            logger.warning("Telegram health check failed")
            return False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def start(self) -> None:
        """Create the bot instance and verify the token."""
        if not self._config.enabled or not self._config.bot_token:
            logger.info("Telegram channel disabled or unconfigured")
            return

        parse_mode = (
            ParseMode.HTML
            if self._config.parse_mode.upper() == "HTML"
            else ParseMode.MARKDOWN_V2
        )
        self._bot = Bot(
            token=self._config.bot_token,
            default=DefaultBotProperties(parse_mode=parse_mode),
        )

        try:
            me = await self._bot.me()
            logger.info("Telegram bot connected: @%s", me.username)
        except Exception as exc:
            logger.error("Telegram bot token verification failed: %s", exc)
            await self._bot.session.close()
            self._bot = None
            raise

    async def stop(self) -> None:
        """Close the bot session. Tears down listener first if running."""
        await self.stop_listener()
        if self._bot is not None:
            await self._bot.session.close()
            self._bot = None
            logger.info("Telegram bot session closed")

    # -----------------------------------------------------------------
    # Send notifications
    # -----------------------------------------------------------------

    async def send(self, notification: Notification) -> bool:
        """Send a notification message to the configured chat."""
        if not self._config.enabled or self._bot is None:
            return True  # disabled = no-op success

        if not self._config.chat_id:
            logger.warning("Telegram chat_id not configured, dropping notification")
            return False

        text = self._format_notification(notification)

        await self._rate_limit()
        try:
            await self._bot.send_message(
                chat_id=self._config.chat_id,
                text=text,
            )
            logger.debug("Telegram notification sent: %s", notification.id)
            return True
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)
            return False

    # -----------------------------------------------------------------
    # Interactive approval
    # -----------------------------------------------------------------

    async def send_approval_request(self, pending: PendingAction) -> None:
        """Send an approval message with inline Approve/Reject buttons."""
        if not self._config.enabled or self._bot is None:
            return

        if not self._config.chat_id:
            logger.warning("Telegram chat_id not configured, dropping approval request")
            return

        text = self._format_approval(pending)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="\u2705 Approve",
                    callback_data=f"{_CB_APPROVE}{pending.id}",
                ),
                InlineKeyboardButton(
                    text="\u274c Reject",
                    callback_data=f"{_CB_REJECT}{pending.id}",
                ),
            ],
        ])

        await self._rate_limit()
        try:
            msg = await self._bot.send_message(
                chat_id=self._config.chat_id,
                text=text,
                reply_markup=keyboard,
            )
            self._approval_messages[pending.id] = msg.message_id
            logger.info(
                "Telegram approval request sent: action=%s, msg_id=%d",
                pending.id, msg.message_id,
            )
        except Exception as exc:
            logger.warning("Telegram approval request failed: %s", exc)

    async def update_approval_message(
        self, action_id: str, status: PendingStatus,
    ) -> None:
        """Edit the approval message to show resolution and remove buttons."""
        if self._bot is None or not self._config.chat_id:
            return

        msg_id = self._approval_messages.pop(action_id, None)
        if msg_id is None:
            return

        status_text = {
            PendingStatus.APPROVED: "\u2705 Approved",
            PendingStatus.REJECTED: "\u274c Rejected",
            PendingStatus.EXPIRED: "\u23f0 Expired",
        }.get(status, str(status))

        try:
            await self._bot.edit_message_text(
                chat_id=self._config.chat_id,
                message_id=msg_id,
                text=f"<b>Action resolved: {status_text}</b>",
            )
        except Exception as exc:
            logger.debug("Telegram message update failed (may be deleted): %s", exc)

    # -----------------------------------------------------------------
    # Listener (polling)
    # -----------------------------------------------------------------

    async def start_listener(
        self,
        callback: Callable[[str, ApprovalDecision], Awaitable[None]],
    ) -> None:
        """Start the aiogram polling loop as a background task."""
        if not self._config.enabled or self._bot is None:
            return

        self._approval_callback = callback
        self._dispatcher = Dispatcher()
        self._dispatcher.include_router(self._router)

        self._polling_task = asyncio.create_task(
            self._run_polling(),
            name="telegram-polling",
        )
        logger.info("Telegram listener started (polling)")

    async def stop_listener(self) -> None:
        """Stop the polling loop."""
        if self._dispatcher is not None:
            await self._dispatcher.stop_polling()
            self._dispatcher = None

        if self._polling_task is not None and not self._polling_task.done():
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
            self._polling_task = None

        logger.info("Telegram listener stopped")

    async def _run_polling(self) -> None:
        """Run the aiogram polling loop. Reconnects on error."""
        assert self._bot is not None
        assert self._dispatcher is not None

        while True:
            try:
                await self._dispatcher.start_polling(
                    self._bot,
                    handle_signals=False,
                )
                break  # Clean shutdown via stop_polling
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Telegram polling error, reconnecting in 5s: %s", exc)
                await asyncio.sleep(5)

    # -----------------------------------------------------------------
    # Callback handlers
    # -----------------------------------------------------------------

    async def _handle_approve(self, callback_query: CallbackQuery) -> None:
        """Handle an Approve button press."""
        if callback_query.data is None:
            return

        action_id = callback_query.data[len(_CB_APPROVE):]
        await callback_query.answer("Approved")

        if self._approval_callback is not None:
            await self._approval_callback(action_id, ApprovalDecision.APPROVED)

    async def _handle_reject(self, callback_query: CallbackQuery) -> None:
        """Handle a Reject button press."""
        if callback_query.data is None:
            return

        action_id = callback_query.data[len(_CB_REJECT):]
        await callback_query.answer("Rejected")

        if self._approval_callback is not None:
            await self._approval_callback(action_id, ApprovalDecision.REJECTED)

    async def _handle_status(self, message: Message) -> None:
        """Handle the /status command."""
        if self._bot is None:
            return

        pending_count = len(self._approval_messages)
        text = (
            f"<b>OasisAgent Status</b>\n"
            f"Pending approvals: {pending_count}\n"
            f"Channel: connected"
        )
        await message.reply(text=text)

    # -----------------------------------------------------------------
    # Formatting helpers
    # -----------------------------------------------------------------

    def _format_notification(self, notification: Notification) -> str:
        """Format a notification as HTML for Telegram."""
        emoji = _SEVERITY_EMOJI.get(notification.severity.value, "\u2139\ufe0f")
        lines = [
            f"{emoji} <b>{notification.title}</b>",
            "",
            notification.message,
        ]
        if notification.event_id:
            lines.append(f"\n<code>Event: {notification.event_id}</code>")
        return "\n".join(lines)

    def _format_approval(self, pending: PendingAction) -> str:
        """Format an approval request as HTML for Telegram."""
        lines = [
            "\U0001f6a8 <b>Action Requires Approval</b>",
            "",
            f"<b>Action:</b> {pending.action.description}",
            f"<b>Handler:</b> {pending.action.handler}",
            f"<b>Operation:</b> {pending.action.operation}",
            f"<b>Risk tier:</b> {pending.action.risk_tier.value}",
            "",
            f"<b>Diagnosis:</b> {pending.diagnosis}",
            "",
            f"<code>ID: {pending.id}</code>",
            f"<i>Expires: {pending.expires_at.strftime('%H:%M UTC')}</i>",
        ]
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Rate limiting
    # -----------------------------------------------------------------

    async def _rate_limit(self) -> None:
        """Enforce Telegram's 1 msg/sec per chat rate limit."""
        now = time.monotonic()
        elapsed = now - self._last_send_time
        if elapsed < _MIN_SEND_INTERVAL:
            await asyncio.sleep(_MIN_SEND_INTERVAL - elapsed)
        self._last_send_time = time.monotonic()
