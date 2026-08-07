"""Telegram integration: fire-and-forget alerts plus an approve/decline
confirmation channel that can replace the terminal prompt.

Talks to the raw Telegram Bot HTTP API via `requests` (long-polling
`getUpdates`) rather than pulling in an async bot framework, to stay
consistent with the rest of this project's synchronous design.

Security: only updates whose `chat.id` matches the configured
`TELEGRAM_CHAT_ID` are ever treated as an approve/decline. Anyone else who
messages the bot -- including in a group the bot is added to -- is ignored
and logged as unauthorized. Treat the bot token and chat id as secrets.
"""

from __future__ import annotations

import logging
import time as time_module
import uuid

import requests

from .config import Config
from .models import ProposedOrder

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_LONG_POLL_SECONDS = 25


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(self, config: Config):
        if not config.telegram_bot_token or not config.telegram_chat_id:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID must both be set to use TelegramNotifier."
            )
        self._token = config.telegram_bot_token
        self._chat_id = str(config.telegram_chat_id)
        self._timeout_seconds = config.telegram_confirm_timeout_seconds
        self._update_offset = 0

    def _call(self, method: str, **params) -> dict | list:
        url = _API_BASE.format(token=self._token, method=method)
        resp = requests.post(url, json=params, timeout=params.get("timeout", 15) + 10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"Telegram API error on {method}: {data}")
        return data["result"]

    def alert(self, text: str) -> None:
        try:
            self._call("sendMessage", chat_id=self._chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram alert: %s", text)

    def confirm(self, order: ProposedOrder, live: bool) -> bool:
        mode = "LIVE (real money)" if live else "PAPER (simulated)"
        nonce = uuid.uuid4().hex[:10]
        minutes = max(1, self._timeout_seconds // 60)
        text = (
            f"PROPOSED TRADE -- {mode}\n"
            f"{order.contract.option_type.value.upper()} {order.contract.symbol} "
            f"${order.contract.strike:g} exp {order.contract.expiration}\n"
            f"Quantity: {order.quantity}\n"
            f"Limit: ${order.limit_price:.2f} (bid ${order.contract.bid:.2f} / ask ${order.contract.ask:.2f})\n"
            f"Est. cost: ${order.limit_price * order.quantity * 100:.2f}\n"
            f"Stop loss: ${order.stop_loss_price:.2f}\n"
            f"Profit target: ${order.profit_target_price:.2f}\n"
            f"Reason: {order.reason}\n\n"
            f"No response within {minutes} min = treated as decline."
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{nonce}"},
                {"text": "❌ Decline", "callback_data": f"decline:{nonce}"},
            ]]
        }
        try:
            sent = self._call(
                "sendMessage", chat_id=self._chat_id, text=text, reply_markup=keyboard
            )
        except Exception:
            logger.exception("Failed to send Telegram confirmation request, defaulting to decline")
            return False

        return self._await_response(nonce, sent["message_id"])

    def _await_response(self, nonce: str, message_id: int) -> bool:
        deadline = time_module.monotonic() + self._timeout_seconds
        while time_module.monotonic() < deadline:
            remaining = deadline - time_module.monotonic()
            poll_timeout = max(1, min(_LONG_POLL_SECONDS, int(remaining)))
            try:
                updates = self._call(
                    "getUpdates",
                    offset=self._update_offset,
                    timeout=poll_timeout,
                    allowed_updates=["callback_query"],
                )
            except Exception:
                logger.exception("Telegram getUpdates failed, retrying")
                time_module.sleep(2)
                continue

            for update in updates:
                self._update_offset = update["update_id"] + 1
                decision = self._handle_update(update, nonce, message_id)
                if decision is not None:
                    return decision

        logger.warning("Telegram confirmation timed out after %ss, treating as decline", self._timeout_seconds)
        self.alert("No response in time -- treated as decline.")
        return False

    def _handle_update(self, update: dict, nonce: str, message_id: int) -> bool | None:
        cq = update.get("callback_query")
        if not cq:
            return None
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.warning("Ignoring Telegram callback from unauthorized chat %s", chat_id)
            return None
        data = cq.get("data", "")
        if not data.endswith(nonce):
            return None  # button press from a stale/earlier prompt

        approved = data.startswith("approve:")
        try:
            self._call(
                "answerCallbackQuery", callback_query_id=cq["id"],
                text="Approved" if approved else "Declined",
            )
            self._call(
                "editMessageReplyMarkup", chat_id=self._chat_id, message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except Exception:
            logger.exception("Failed to acknowledge Telegram callback")
        return approved
