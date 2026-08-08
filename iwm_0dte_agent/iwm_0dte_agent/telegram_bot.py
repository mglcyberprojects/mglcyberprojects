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
from html import escape as _esc

import requests

from .config import Config
from .gameplan_strategy import GameplanZones, parse_zone_message
from .models import OptionType, ProposedOrder

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_LONG_POLL_SECONDS = 25

# Checked in order against the start of an alert's text; first match wins.
# Keeps every plain-text status line visually scannable at a glance without
# agent.py needing to know anything about Telegram formatting.
_ALERT_ICONS = [
    ("Agent started", "🟢"),
    ("Agent failed to start", "🔴"),
    ("Market closed", "🌙"),
    ("Filled:", "✅"),
    ("Closed (", "✅"),
    ("Declined", "🚫"),
    ("Order FAILED", "⚠️"),
    ("Close order FAILED", "⚠️"),
    ("No new entries", "⏸️"),
    ("No gameplan zones", "⏸️"),
    ("Signal fired", "👀"),
    ("Gameplan: support broken", "⚠️"),
    ("Gameplan: resistance broken", "⚠️"),
    ("Zones set:", "🗺️"),
    ("Could not parse", "❓"),
    ("No zones received", "⏱️"),
    ("No response in time", "⏱️"),
    ("Error in agent loop", "🔥"),
]


def _icon_for(text: str) -> str:
    for prefix, icon in _ALERT_ICONS:
        if text.startswith(prefix):
            return icon
    return "ℹ️"


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
        # Plain emoji prefix, no HTML markup involved -- text is whatever
        # opaque string agent.py built and needs no escaping here.
        icon = _icon_for(text)
        try:
            self._call("sendMessage", chat_id=self._chat_id, text=f"{icon} {text}")
        except Exception:
            logger.exception("Failed to send Telegram alert: %s", text)

    def confirm(self, order: ProposedOrder, live: bool) -> bool:
        mode = "LIVE (real money)" if live else "PAPER (simulated)"
        mode_icon = "🟢" if live else "🧪"
        direction_icon = "📈" if order.contract.option_type == OptionType.CALL else "📉"
        nonce = uuid.uuid4().hex[:10]
        minutes = max(1, self._timeout_seconds // 60)
        pnl_line = ""
        if order.entry_price is not None:
            pnl_icon = "🟢" if order.pnl_dollars >= 0 else "🔴"
            pnl_line = f"{pnl_icon} P&L: <b>{order.pnl_pct:+.1f}%</b> (${order.pnl_dollars:+.2f})\n"

        # HTML parse_mode below -- every dynamic value must be escaped, since
        # strategy/risk reason strings routinely contain literal "<=" / ">="
        # (e.g. "stop loss hit (bid 1.05 <= 1.05)") that would otherwise be
        # parsed as broken tags rather than displayed as text.
        text = (
            f"{mode_icon} <b>PROPOSED TRADE</b> · {_esc(mode)}\n\n"
            f"{direction_icon} <b>{_esc(order.contract.option_type.value.upper())}</b> "
            f"{_esc(order.contract.symbol)} ${order.contract.strike:g} · exp {_esc(order.contract.expiration)}\n\n"
            f"Quantity: <b>{order.quantity}</b>\n"
            f"Limit: <b>${order.limit_price:.2f}</b> (bid ${order.contract.bid:.2f} / ask ${order.contract.ask:.2f})\n"
            f"Est. cost: <b>${order.limit_price * order.quantity * 100:.2f}</b>\n"
            f"Stop loss: ${order.stop_loss_price:.2f}\n"
            f"Profit target: ${order.profit_target_price:.2f}\n"
            f"{pnl_line}\n"
            f"💬 <i>{_esc(order.reason)}</i>\n\n"
            f"⏱ No response in {minutes} min → treated as decline"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{nonce}"},
                {"text": "❌ Decline", "callback_data": f"decline:{nonce}"},
            ]]
        }
        try:
            sent = self._call(
                "sendMessage", chat_id=self._chat_id, text=text,
                parse_mode="HTML", reply_markup=keyboard,
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

    def request_zones(self, timeout_seconds: int) -> GameplanZones | None:
        minutes = max(1, timeout_seconds // 60)
        prompt = (
            "🗺️ <b>Gameplan strategy</b> — send today's zones as:\n"
            "<code>hold_low hold_high reject_low reject_high</code>\n"
            "e.g. <code>228.50 229.20 231.00 232.50</code>\n\n"
            f"⏱ No reply within {minutes} min = no trading today."
        )
        try:
            self._call("sendMessage", chat_id=self._chat_id, text=prompt, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send Telegram zone request")
        deadline = time_module.monotonic() + timeout_seconds
        while time_module.monotonic() < deadline:
            remaining = deadline - time_module.monotonic()
            poll_timeout = max(1, min(_LONG_POLL_SECONDS, int(remaining)))
            try:
                updates = self._call(
                    "getUpdates", offset=self._update_offset, timeout=poll_timeout,
                    allowed_updates=["message"],
                )
            except Exception:
                logger.exception("Telegram getUpdates failed while waiting for zones, retrying")
                time_module.sleep(2)
                continue

            for update in updates:
                self._update_offset = update["update_id"] + 1
                zones = self._handle_zone_update(update)
                if zones is not None:
                    return zones

        logger.warning("No gameplan zones received within %ss", timeout_seconds)
        self.alert("No zones received in time -- no trading today.")
        return None

    def _handle_zone_update(self, update: dict) -> GameplanZones | None:
        message = update.get("message")
        if not message:
            return None
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.warning("Ignoring Telegram message from unauthorized chat %s", chat_id)
            return None
        text = message.get("text", "")
        zones = parse_zone_message(text)
        if zones is None:
            self.alert(f'Could not parse "{text}" -- expected 4 numbers, try again.')
            return None
        self.alert(
            f"Zones set: hold [{zones.hold_low:g}-{zones.hold_high:g}], "
            f"reject [{zones.reject_low:g}-{zones.reject_high:g}]"
        )
        return zones

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
