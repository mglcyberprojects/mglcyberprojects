"""Notifier abstraction: where alerts and trade confirmations go.

`agent.py` only ever talks to this interface, never to Telegram or the
terminal directly, so the confirmation gate described in the project README
is enforced the same way regardless of channel: nothing in `broker.py` gets
called until `confirm()` returns True.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .config import Config
from .models import ProposedOrder

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def alert(self, text: str) -> None: ...

    def confirm(self, order: ProposedOrder, live: bool) -> bool: ...


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
