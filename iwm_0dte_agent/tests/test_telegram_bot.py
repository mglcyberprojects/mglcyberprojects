from iwm_0dte_agent.config import Config
from iwm_0dte_agent.models import OptionContract, OptionType, ProposedOrder
from iwm_0dte_agent.telegram_bot import TelegramNotifier, _icon_for


def make_notifier() -> TelegramNotifier:
    return TelegramNotifier(Config(telegram_bot_token="123:abc", telegram_chat_id="42"))


class FakeCall:
    def __init__(self, result=None):
        self.result = result if result is not None else {"message_id": 1}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, **params):
        self.calls.append((method, params))
        return self.result


def test_icon_for_matches_known_prefixes():
    assert _icon_for("Agent started (LIVE mode, orb strategy) for IWM") == "🟢"
    assert _icon_for("Filled: BUY 3x CALL IWM $228 @ $2.10") == "✅"
    assert _icon_for("Declined: CALL $228 entry") == "🚫"
    assert _icon_for("Order FAILED: CALL $228 -- insufficient funds") == "⚠️"
    assert _icon_for("Market closed, agent stopping for the day.") == "🌙"


def test_icon_for_defaults_to_info_icon_for_unmatched_text():
    assert _icon_for("Some brand new alert text nobody wrote a rule for") == "ℹ️"


def test_alert_prefixes_icon_and_sends_plain_text_no_parse_mode():
    notifier = make_notifier()
    fake_call = FakeCall()
    notifier._call = fake_call

    notifier.alert("Filled: BUY 3x CALL IWM $228 @ $2.10")

    method, params = fake_call.calls[0]
    assert method == "sendMessage"
    assert params["text"] == "✅ Filled: BUY 3x CALL IWM $228 @ $2.10"
    assert "parse_mode" not in params


def _make_order(
    option_type=OptionType.CALL, reason="close 228.45 broke above ORB high 228.10",
    entry_price=None, limit_price=2.10, quantity=3,
):
    contract = OptionContract("IWM", 228.0, option_type, "2026-08-10", 2.00, limit_price, 2.05, "instr-1")
    return ProposedOrder(
        contract=contract, quantity=quantity, limit_price=limit_price,
        stop_loss_price=1.05, profit_target_price=4.20, reason=reason,
        entry_price=entry_price,
    )


def test_confirm_sends_html_with_bold_and_direction_icon():
    notifier = make_notifier()
    fake_call = FakeCall()
    notifier._call = fake_call
    notifier._await_response = lambda nonce, message_id: True

    notifier.confirm(_make_order(option_type=OptionType.CALL), live=True)

    method, params = fake_call.calls[0]
    assert method == "sendMessage"
    assert params["parse_mode"] == "HTML"
    assert "<b>PROPOSED TRADE</b>" in params["text"]
    assert "📈" in params["text"]  # call
    assert "<b>CALL</b>" in params["text"]
    assert "🟢" in params["text"]  # live mode icon


def test_confirm_uses_put_icon_and_paper_icon():
    notifier = make_notifier()
    fake_call = FakeCall()
    notifier._call = fake_call
    notifier._await_response = lambda nonce, message_id: True

    notifier.confirm(_make_order(option_type=OptionType.PUT), live=False)

    _, params = fake_call.calls[0]
    assert "📉" in params["text"]
    assert "<b>PUT</b>" in params["text"]
    assert "🧪" in params["text"]  # paper mode icon


def test_confirm_escapes_html_sensitive_reason_text():
    # Real risk.py/strategy.py reason strings routinely contain literal
    # "<=" / ">=" -- these must be escaped or Telegram's HTML parser would
    # choke on (or mangle) the message.
    notifier = make_notifier()
    fake_call = FakeCall()
    notifier._call = fake_call
    notifier._await_response = lambda nonce, message_id: True

    order = _make_order(reason="stop loss hit (bid 1.05 <= 1.05)")
    notifier.confirm(order, live=True)

    _, params = fake_call.calls[0]
    assert "<=" not in params["text"].replace("&lt;=", "")  # raw "<=" must not survive unescaped
    assert "&lt;=" in params["text"]
    assert "stop loss hit (bid 1.05" in params["text"]


def test_confirm_entry_proposal_shows_no_pnl_line():
    # Entry proposals have no entry_price to compare against -- nothing to show.
    notifier = make_notifier()
    fake_call = FakeCall()
    notifier._call = fake_call
    notifier._await_response = lambda nonce, message_id: True

    notifier.confirm(_make_order(entry_price=None), live=True)

    _, params = fake_call.calls[0]
    assert "P&L" not in params["text"]


def test_confirm_close_proposal_shows_gain_with_green_icon():
    notifier = make_notifier()
    fake_call = FakeCall()
    notifier._call = fake_call
    notifier._await_response = lambda nonce, message_id: True

    # Entered at $2.10, closing at $4.25 -> +102.4%, +$645.00 on 3 contracts.
    order = _make_order(entry_price=2.10, limit_price=4.25, reason="profit target hit (bid 4.25 >= 4.20)")
    notifier.confirm(order, live=True)

    _, params = fake_call.calls[0]
    assert "🟢 P&L: <b>+102.4%</b> ($+645.00)" in params["text"]


def test_confirm_close_proposal_shows_loss_with_red_icon():
    notifier = make_notifier()
    fake_call = FakeCall()
    notifier._call = fake_call
    notifier._await_response = lambda nonce, message_id: True

    # Entered at $1.35, closing at $0.65 -> -51.9%, -$280.00 on 4 contracts.
    order = _make_order(entry_price=1.35, limit_price=0.65, quantity=4, reason="stop loss hit (bid 0.65 <= 0.68)")
    notifier.confirm(order, live=True)

    _, params = fake_call.calls[0]
    assert "🔴 P&L: <b>-51.9%</b> ($-280.00)" in params["text"]


def test_request_zones_sends_html_prompt_then_waits():
    notifier = make_notifier()
    call_log = []

    def fake_call(method, **params):
        call_log.append((method, params))
        if method == "getUpdates":
            return []
        return {"message_id": 1}

    notifier._call = fake_call

    # Force the wait loop to exit immediately by making time run out fast --
    # patch the deadline check via a tiny timeout instead of real waiting.
    result = notifier.request_zones(timeout_seconds=0)

    assert result is None
    prompt_calls = [p for m, p in call_log if m == "sendMessage"]
    assert prompt_calls, "expected an initial zone-request prompt to be sent"
    prompt = prompt_calls[0]
    assert prompt["parse_mode"] == "HTML"
    assert "<code>hold_low hold_high reject_low reject_high</code>" in prompt["text"]
