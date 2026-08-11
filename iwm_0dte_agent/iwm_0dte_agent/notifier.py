"""Notifier abstraction: where alerts and trade confirmations go.

`agent.py` only ever talks to this interface, never to Telegram or the
terminal directly, so the confirmation gate described in the project README
is enforced the same way regardless of channel: nothing in `broker.py` gets
called until `confirm()` returns True.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from .config import Config
from .gameplan_strategy import GameplanZones
from .models import AgentStatus, ProposedOrder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatusRequest:
    """A pending on-demand status query from the user -- "new" for a fresh
    /status command, "refresh" for a tap on an existing status message's
    Refresh button (message_id is which message to edit in place)."""

    kind: Literal["new", "refresh"]
    message_id: int | None = None


class Notifier(Protocol):
    def alert(self, text: str) -> None: ...

    def confirm(self, order: ProposedOrder, live: bool) -> int: ...
    # Returns 0 for declined/timed-out, else the approved quantity -- for
    # order.quantity_choices proposals that's whichever choice was tapped;
    # for fixed-quantity proposals (closes) that's order.quantity. 0 is
    # falsy, so `if not notifier.confirm(...)` still reads as "declined".

    def confirm_live_start(self, warning_text: str) -> bool: ...
    # One-time gate before --live is allowed to place any real order. Only
    # called when live=True. Must require an explicit, deliberate response
    # (not a single button tap) since this is the one confirmation standing
    # between a --live session and real money -- TerminalNotifier requires
    # typing the literal word LIVE; TelegramNotifier requires replying with
    # the literal word LIVE as text within the confirm timeout. Anything
    # else, or no response in time, returns False and the agent never starts.

    def request_zones(self, timeout_seconds: int) -> GameplanZones | None: ...

    def poll_status_requests(self) -> list[StatusRequest]: ...

    def post_status(self, status: AgentStatus) -> None: ...

    def update_status(self, message_id: int, status: AgentStatus) -> None: ...

    def show_positions_shortcut(self) -> None: ...


def build_notifier(config: Config) -> Notifier:
    if config.telegram_bot_token and config.telegram_chat_id:
        from .telegram_bot import TelegramNotifier

        logger.info("Telegram alerts + confirmation enabled (chat %s)", config.telegram_chat_id)
        return TelegramNotifier(config)

    logger.info(
        "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- falling back to terminal "
        "confirmation only, no remote alerts."
    )
    from .terminal_notifier import TerminalNotifier

    return TerminalNotifier()
