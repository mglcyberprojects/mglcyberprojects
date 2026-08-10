import datetime as dt

from iwm_0dte_agent.agent import _maybe_alert_blocked_entry, _maybe_alert_loop_error
from iwm_0dte_agent.config import Config
from iwm_0dte_agent.notifier import build_notifier
from iwm_0dte_agent.terminal_notifier import TerminalNotifier
from iwm_0dte_agent.telegram_bot import TelegramNotifier


class FakeNotifier:
    def __init__(self):
        self.alerts: list[str] = []

    def alert(self, text: str) -> None:
        self.alerts.append(text)

    def confirm(self, order, live: bool) -> int:
        raise NotImplementedError


def test_build_notifier_falls_back_to_terminal_when_unconfigured():
    config = Config(telegram_bot_token="", telegram_chat_id="")
    assert isinstance(build_notifier(config), TerminalNotifier)


def test_build_notifier_uses_telegram_when_configured():
    config = Config(telegram_bot_token="123:abc", telegram_chat_id="42")
    notifier = build_notifier(config)
    assert isinstance(notifier, TelegramNotifier)


def test_build_notifier_falls_back_if_only_one_field_set():
    config = Config(telegram_bot_token="123:abc", telegram_chat_id="")
    assert isinstance(build_notifier(config), TerminalNotifier)


def test_blocked_entry_alert_fires_once_per_reason_per_day():
    notifier = FakeNotifier()
    alerted: set[tuple[dt.date, str]] = set()

    _maybe_alert_blocked_entry("max trades/day reached (2)", notifier, alerted)
    _maybe_alert_blocked_entry("max trades/day reached (2)", notifier, alerted)
    _maybe_alert_blocked_entry("past entry cutoff (14:30)", notifier, alerted)

    assert len(notifier.alerts) == 2
    assert any("max trades" in a for a in notifier.alerts)
    assert any("entry cutoff" in a for a in notifier.alerts)


def test_blocked_entry_alert_refires_on_new_day():
    notifier = FakeNotifier()
    alerted: set[tuple[dt.date, str]] = set()
    yesterday = dt.date.today() - dt.timedelta(days=1)
    alerted.add((yesterday, "max trades/day reached (2)"))

    _maybe_alert_blocked_entry("max trades/day reached (2)", notifier, alerted)

    assert len(notifier.alerts) == 1


def test_loop_error_alert_fires_once_per_error_per_day():
    # A persistent loop error (e.g. a broker/MCP schema mismatch) used to
    # re-alert every poll cycle -- once a minute at the default
    # POLL_SECONDS -- for the rest of the session. Same fix as
    # _maybe_alert_blocked_entry, applied here.
    notifier = FakeNotifier()
    alerted: set[tuple[dt.date, str]] = set()

    _maybe_alert_loop_error(ValueError("boom"), notifier, alerted)
    _maybe_alert_loop_error(ValueError("boom"), notifier, alerted)
    _maybe_alert_loop_error(ValueError("boom"), notifier, alerted)

    assert len(notifier.alerts) == 1
    assert "boom" in notifier.alerts[0]


def test_loop_error_alert_fires_again_for_a_different_error():
    notifier = FakeNotifier()
    alerted: set[tuple[dt.date, str]] = set()

    _maybe_alert_loop_error(ValueError("boom"), notifier, alerted)
    _maybe_alert_loop_error(RuntimeError("different problem"), notifier, alerted)

    assert len(notifier.alerts) == 2


def test_loop_error_alert_refires_on_new_day():
    notifier = FakeNotifier()
    alerted: set[tuple[dt.date, str]] = set()
    yesterday = dt.date.today() - dt.timedelta(days=1)
    alerted.add((yesterday, "error: ValueError('boom')"))

    _maybe_alert_loop_error(ValueError("boom"), notifier, alerted)

    assert len(notifier.alerts) == 1
