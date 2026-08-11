from iwm_0dte_agent.models import OptionContract, OptionType, ProposedOrder
from iwm_0dte_agent.terminal_notifier import TerminalNotifier


def _make_order(quantity_choices=None, quantity=3, limit_price=2.10, entry_price=None):
    contract = OptionContract("IWM", 228.0, OptionType.CALL, "2026-08-10", 2.00, limit_price, 2.05, "instr-1")
    return ProposedOrder(
        contract=contract, quantity=quantity, limit_price=limit_price,
        stop_loss_price=1.05, profit_target_price=4.20, reason="orb breakout",
        entry_price=entry_price, quantity_choices=quantity_choices,
    )


def test_confirm_with_choices_returns_picked_quantity(monkeypatch, capsys):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")

    result = notifier.confirm(_make_order(quantity_choices=[1, 3, 5]), live=True)

    assert result == 3
    out = capsys.readouterr().out
    assert "Quantity options:" in out
    assert "1  ->  est. cost $210.00" in out
    assert "3  ->  est. cost $630.00" in out
    assert "5  ->  est. cost $1050.00" in out


def test_confirm_with_choices_blank_input_declines(monkeypatch):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert notifier.confirm(_make_order(quantity_choices=[1, 3, 5]), live=True) == 0


def test_confirm_with_choices_non_numeric_input_declines(monkeypatch):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")

    assert notifier.confirm(_make_order(quantity_choices=[1, 3, 5]), live=True) == 0


def test_confirm_with_choices_rejects_quantity_not_offered(monkeypatch):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "7")

    assert notifier.confirm(_make_order(quantity_choices=[1, 3, 5]), live=True) == 0


def test_confirm_without_choices_yes_returns_fixed_quantity(monkeypatch):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert notifier.confirm(_make_order(quantity_choices=None, quantity=3), live=True) == 3


def test_confirm_without_choices_no_returns_zero(monkeypatch):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    assert notifier.confirm(_make_order(quantity_choices=None, quantity=3), live=True) == 0


def test_confirm_live_start_exact_live_returns_true(monkeypatch, capsys):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "LIVE")

    assert notifier.confirm_live_start("some warning text") is True
    assert "some warning text" in capsys.readouterr().out


def test_confirm_live_start_wrong_case_returns_false(monkeypatch):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "live")

    assert notifier.confirm_live_start("warning") is False


def test_confirm_live_start_blank_returns_false(monkeypatch):
    notifier = TerminalNotifier()
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert notifier.confirm_live_start("warning") is False
