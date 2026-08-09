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

import datetime as dt
import logging
import time as time_module
import uuid
from html import escape as _esc

import requests

from .config import Config
from .gameplan_strategy import GameplanZones, parse_zone_message
from .models import AgentStatus, OptionType, ProposedOrder
from .notifier import StatusRequest

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


_POSITIONS_BUTTON_TEXT = "📊 Positions"
_STATUS_COMMANDS = {"/status", "/positions", _POSITIONS_BUTTON_TEXT.lower()}


def _format_status(status: AgentStatus) -> str:
    now_str = dt.datetime.now().strftime("%I:%M:%S %p").lstrip("0")
    lines = [f"📊 <b>Status</b> · as of {_esc(now_str)}", ""]

    if status.positions:
        multiple = len(status.positions) > 1
        lines.append(f"<b>Open positions ({len(status.positions)}):</b>")
        for i, p in enumerate(status.positions, start=1):
            direction_icon = "📈" if p.contract.option_type == OptionType.CALL else "📉"
            pnl_icon = "🟢" if p.pnl_dollars >= 0 else "🔴"
            prefix = f"{i}. " if multiple else ""
            lines += [
                f"{prefix}{direction_icon} <b>{_esc(p.contract.option_type.value.upper())}</b> "
                f"{_esc(p.contract.symbol)} ${p.contract.strike:g} · exp {_esc(p.contract.expiration)}",
                f"Qty: {p.quantity} @ entry ${p.entry_price:.2f}",
                f"Current bid: ${p.current_bid:.2f}",
                f"{pnl_icon} Unrealized P&L: <b>{p.pnl_pct:+.1f}%</b> (${p.pnl_dollars:+.2f})",
                f"Stop loss: ${p.stop_loss_price:.2f} · Profit target: ${p.profit_target_price:.2f}",
                "",
            ]
    else:
        lines += ["No open positions.", ""]

    lines.append(f"Buying power: ${status.buying_power:,.2f}")
    lines.append(
        f"Trades today: {status.trades_today}/{status.max_trades_per_day} · "
        f"Realized P&L today: ${status.realized_pnl_today:+.2f}"
    )
    return "\n".join(lines)


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

    def poll_status_requests(self) -> list[StatusRequest]:
        # A single non-blocking (timeout=0) poll, meant to be called once
        # per agent.py loop iteration -- unlike confirm()/request_zones(),
        # this never blocks waiting for a reply, so a /status command or
        # Refresh tap is only ever picked up on the next loop cycle (i.e.
        # up to POLL_SECONDS of latency, not instant).
        try:
            updates = self._call(
                "getUpdates", offset=self._update_offset, timeout=0,
                allowed_updates=["message", "callback_query"],
            )
        except Exception:
            logger.exception("Telegram getUpdates failed while polling for status requests")
            return []

        found: list[StatusRequest] = []
        for update in updates:
            self._update_offset = update["update_id"] + 1

            message = update.get("message")
            if message is not None:
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = (message.get("text") or "").strip().lower()
                if chat_id == self._chat_id and text in _STATUS_COMMANDS:
                    found.append(StatusRequest(kind="new"))
                continue

            cq = update.get("callback_query")
            if cq is None:
                continue
            chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            data = cq.get("data", "")
            if chat_id != self._chat_id or not data.startswith("refresh:"):
                continue  # not ours, or a stale/foreign refresh tap
            try:
                self._call("answerCallbackQuery", callback_query_id=cq["id"], text="Refreshing…")
            except Exception:
                logger.exception("Failed to acknowledge Telegram refresh callback")
            found.append(StatusRequest(kind="refresh", message_id=cq["message"]["message_id"]))
        return found

    def post_status(self, status: AgentStatus) -> None:
        try:
            sent = self._call(
                "sendMessage", chat_id=self._chat_id, text=_format_status(status), parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to send Telegram status message")
            return
        keyboard = {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": f"refresh:{sent['message_id']}"}]]}
        try:
            self._call(
                "editMessageReplyMarkup", chat_id=self._chat_id, message_id=sent["message_id"],
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to attach refresh button to status message")

    def update_status(self, message_id: int, status: AgentStatus) -> None:
        try:
            self._call(
                "editMessageText", chat_id=self._chat_id, message_id=message_id,
                text=_format_status(status), parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to update Telegram status message")

    def show_positions_shortcut(self) -> None:
        # A persistent custom keyboard (not an inline button attached to one
        # message) -- it replaces the chat's normal keyboard with a single
        # "Positions" button that stays there across every message until
        # changed, so there's always a tap-to-check-positions option
        # available without hunting for a specific message or retyping
        # /status. Tapping it just sends its label as an ordinary text
        # message, which poll_status_requests() recognizes the same as
        # /status -- no separate handling needed on the receiving end.
        keyboard = {
            "keyboard": [[{"text": _POSITIONS_BUTTON_TEXT}]],
            "resize_keyboard": True,
            "is_persistent": True,
        }
        try:
            self._call(
                "sendMessage", chat_id=self._chat_id,
                text="Tap the button below any time to check open positions.",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to send Telegram positions button")
