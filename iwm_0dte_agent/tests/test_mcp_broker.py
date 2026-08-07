import datetime as dt

import pytest

from iwm_0dte_agent.config import Config
from iwm_0dte_agent.mcp_broker import MCPBroker, MCPError, _first_present
from iwm_0dte_agent.models import OptionContract, OptionType, OrderResult


class FakeFallback:
    def __init__(self):
        self.calls = []

    def login(self):
        self.calls.append("login")

    def get_buying_power(self):
        raise NotImplementedError

    def get_underlying_price(self, symbol):
        raise NotImplementedError

    def get_intraday_bars(self, symbol, since):
        self.calls.append(("get_intraday_bars", symbol, since))
        return ["bars"]

    def get_0dte_chain(self, symbol):
        self.calls.append(("get_0dte_chain", symbol))
        return ["chain"]

    def get_option_quote(self, contract_id):
        self.calls.append(("get_option_quote", contract_id))
        return "quote"

    def submit_order(self, contract, quantity, limit_price, side):
        self.calls.append(("submit_order", contract, quantity, limit_price, side))
        return OrderResult(submitted=True, broker_order_id="fake-1", detail="ok")


def make_broker() -> tuple[MCPBroker, FakeFallback]:
    fallback = FakeFallback()
    broker = MCPBroker(Config(), fallback=fallback)
    return broker, fallback


def test_first_present_returns_first_matching_key():
    assert _first_present({"a": None, "b": 5}, "a", "b") == 5
    assert _first_present({"a": 1}, "a", "b") == 1
    assert _first_present({}, "a", "b") is None


def test_pick_tool_returns_first_available_candidate():
    broker, _ = make_broker()
    broker._tool_names = {"get_accounts", "get_equity_quotes"}
    assert broker._pick_tool("get_portfolio", "get_accounts") == "get_accounts"


def test_pick_tool_raises_when_nothing_matches():
    broker, _ = make_broker()
    broker._tool_names = {"search"}
    with pytest.raises(MCPError):
        broker._pick_tool("get_portfolio", "get_accounts")


def test_get_buying_power_parses_top_level_field():
    broker, _ = make_broker()
    broker._tool_names = {"get_portfolio"}
    broker._call_tool_sync = lambda name, args: {"buying_power": "1234.56"}
    assert broker.get_buying_power() == 1234.56


def test_get_buying_power_falls_back_to_accounts_list():
    broker, _ = make_broker()
    broker._tool_names = {"get_accounts"}
    broker._call_tool_sync = lambda name, args: {"accounts": [{"buyingPower": 500.0}]}
    assert broker.get_buying_power() == 500.0


def test_get_buying_power_raises_when_unparseable():
    broker, _ = make_broker()
    broker._tool_names = {"get_portfolio"}
    broker._call_tool_sync = lambda name, args: {"unexpected": "shape"}
    with pytest.raises(MCPError):
        broker.get_buying_power()


def test_get_underlying_price_parses_quotes_list():
    broker, _ = make_broker()
    broker._tool_names = {"get_equity_quotes"}
    broker._call_tool_sync = lambda name, args: {"quotes": [{"last_trade_price": "201.5"}]}
    assert broker.get_underlying_price("IWM") == 201.5


def test_get_underlying_price_raises_when_unparseable():
    broker, _ = make_broker()
    broker._tool_names = {"get_equity_quotes"}
    broker._call_tool_sync = lambda name, args: {"quotes": [{}]}
    with pytest.raises(MCPError):
        broker.get_underlying_price("IWM")


def test_options_related_methods_delegate_to_fallback():
    broker, fallback = make_broker()
    since = dt.datetime(2024, 1, 2, 9, 30)

    broker.get_intraday_bars("IWM", since)
    broker.get_0dte_chain("IWM")
    broker.get_option_quote("contract-1")

    contract = OptionContract("IWM", 200.0, OptionType.CALL, "2024-01-02", 1.0, 1.1, 1.05, "c200")
    broker.submit_order(contract, 2, 1.05, "buy")

    assert ("get_intraday_bars", "IWM", since) in fallback.calls
    assert ("get_0dte_chain", "IWM") in fallback.calls
    assert ("get_option_quote", "contract-1") in fallback.calls
    assert any(call[0] == "submit_order" for call in fallback.calls)


def test_login_connects_then_logs_into_fallback():
    broker, fallback = make_broker()
    broker.connect = lambda: broker._tool_names.add("get_accounts")

    broker.login()

    assert "get_accounts" in broker._tool_names
    assert "login" in fallback.calls
