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


class FakeTool:
    def __init__(self, name, description, input_schema, output_schema):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema


class FakeMCPSession:
    """Stands in for the real _MCPSession -- only tool_names/tools are read
    by the code paths these tests exercise (_pick_tool / describe_tool /
    the presence check in _call_tool_sync, which itself is monkeypatched
    away in most tests)."""

    def __init__(self, tool_names: set[str], tools: list | None = None):
        self.tool_names = tool_names
        self.tools = tools or []


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
    broker._mcp_session = FakeMCPSession({"get_accounts", "get_equity_quotes"})
    assert broker._pick_tool("get_portfolio", "get_accounts") == "get_accounts"


def test_pick_tool_raises_when_nothing_matches():
    broker, _ = make_broker()
    broker._mcp_session = FakeMCPSession({"search"})
    with pytest.raises(MCPError):
        broker._pick_tool("get_portfolio", "get_accounts")


def test_get_buying_power_parses_top_level_field():
    broker, _ = make_broker()
    broker._mcp_session = FakeMCPSession({"get_portfolio"})
    broker._call_tool_sync = lambda name, args: {"buying_power": "1234.56"}
    assert broker.get_buying_power() == 1234.56


def test_get_buying_power_falls_back_to_accounts_list():
    broker, _ = make_broker()
    broker._mcp_session = FakeMCPSession({"get_accounts"})
    broker._call_tool_sync = lambda name, args: {"accounts": [{"buyingPower": 500.0}]}
    assert broker.get_buying_power() == 500.0


def test_get_buying_power_raises_when_unparseable():
    broker, _ = make_broker()
    broker._mcp_session = FakeMCPSession({"get_portfolio"})
    broker._call_tool_sync = lambda name, args: {"unexpected": "shape"}
    with pytest.raises(MCPError):
        broker.get_buying_power()


def test_get_underlying_price_parses_quotes_list():
    broker, _ = make_broker()
    broker._mcp_session = FakeMCPSession({"get_equity_quotes"})
    broker._call_tool_sync = lambda name, args: {"quotes": [{"last_trade_price": "201.5"}]}
    assert broker.get_underlying_price("IWM") == 201.5


def test_get_underlying_price_raises_when_unparseable():
    broker, _ = make_broker()
    broker._mcp_session = FakeMCPSession({"get_equity_quotes"})
    broker._call_tool_sync = lambda name, args: {"quotes": [{}]}
    with pytest.raises(MCPError):
        broker.get_underlying_price("IWM")


def test_get_intraday_bars_delegates_to_fallback():
    broker, fallback = make_broker()
    since = dt.datetime(2024, 1, 2, 9, 30)

    broker.get_intraday_bars("IWM", since)

    assert ("get_intraday_bars", "IWM", since) in fallback.calls


class FakeCallTool:
    """Records every _call_tool_sync invocation and dispatches by tool name.
    Each response may be a plain value or a callable(args) -> value, so
    tests can vary the response based on arguments (e.g. pagination)."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name, args):
        self.calls.append((name, args))
        if name not in self.responses:
            raise AssertionError(f"Unexpected call to tool {name!r} with {args!r}")
        resp = self.responses[name]
        return resp(args) if callable(resp) else resp


TODAY = dt.date.today().isoformat()


def test_get_0dte_chain_builds_contracts_from_chain_instruments_and_quotes():
    broker, _ = make_broker()
    call_tool = FakeCallTool({
        "get_option_chains": {"data": {"chains": [
            {"id": "chain-1", "expiration_dates": [TODAY], "can_open_position": True},
        ]}},
        "get_option_instruments": {"data": {"instruments": [
            {"id": "instr-call", "chain_symbol": "IWM", "expiration_date": TODAY, "strike_price": "200.0000", "type": "call"},
            {"id": "instr-put", "chain_symbol": "IWM", "expiration_date": TODAY, "strike_price": "200.0000", "type": "put"},
        ], "next": ""}},
        "get_option_quotes": {"data": {"results": [
            {"quote": {"instrument_id": "instr-call", "bid_price": "1.00", "ask_price": "1.10", "mark_price": "1.05"}},
            {"quote": {"instrument_id": "instr-put", "bid_price": "0.90", "ask_price": "1.00", "mark_price": "0.95"}},
        ]}},
    })
    broker._call_tool_sync = call_tool

    contracts = broker.get_0dte_chain("IWM")

    assert len(contracts) == 2
    calls = {c.contract_id: c for c in contracts}
    assert calls["instr-call"].option_type == OptionType.CALL
    assert calls["instr-call"].strike == 200.0
    assert calls["instr-call"].bid == 1.00 and calls["instr-call"].ask == 1.10
    assert calls["instr-put"].option_type == OptionType.PUT
    assert broker._instrument_cache["instr-call"]["id"] == "instr-call"


def test_get_0dte_chain_returns_empty_when_no_chain_matches_today():
    broker, _ = make_broker()
    broker._call_tool_sync = FakeCallTool({
        "get_option_chains": {"data": {"chains": [
            {"id": "chain-1", "expiration_dates": ["2099-01-01"], "can_open_position": True},
        ]}},
    })
    assert broker.get_0dte_chain("IWM") == []


def test_get_0dte_chain_paginates_instruments():
    broker, _ = make_broker()

    def instruments_page(args):
        if "cursor" not in args:
            return {"data": {"instruments": [
                {"id": "instr-1", "chain_symbol": "IWM", "expiration_date": TODAY, "strike_price": "200.0", "type": "call"},
            ], "next": "https://api/x?cursor=page2"}}
        assert args["cursor"] == "page2"
        return {"data": {"instruments": [
            {"id": "instr-2", "chain_symbol": "IWM", "expiration_date": TODAY, "strike_price": "201.0", "type": "call"},
        ], "next": ""}}

    call_tool = FakeCallTool({
        "get_option_chains": {"data": {"chains": [
            {"id": "chain-1", "expiration_dates": [TODAY], "can_open_position": True},
        ]}},
        "get_option_instruments": instruments_page,
        "get_option_quotes": {"data": {"results": [
            {"quote": {"instrument_id": "instr-1", "bid_price": "1.0", "ask_price": "1.1", "mark_price": "1.05"}},
            {"quote": {"instrument_id": "instr-2", "bid_price": "0.5", "ask_price": "0.6", "mark_price": "0.55"}},
        ]}},
    })
    broker._call_tool_sync = call_tool

    contracts = broker.get_0dte_chain("IWM")

    assert {c.contract_id for c in contracts} == {"instr-1", "instr-2"}
    assert sum(1 for name, _ in call_tool.calls if name == "get_option_instruments") == 2


def test_get_option_quote_uses_cache_when_available():
    broker, _ = make_broker()
    broker._instrument_cache["instr-1"] = {
        "id": "instr-1", "chain_symbol": "IWM", "expiration_date": TODAY, "strike_price": "200.0", "type": "call",
    }
    call_tool = FakeCallTool({
        "get_option_quotes": {"data": {"results": [
            {"quote": {"instrument_id": "instr-1", "bid_price": "1.0", "ask_price": "1.1", "mark_price": "1.05"}},
        ]}},
    })
    broker._call_tool_sync = call_tool

    contract = broker.get_option_quote("instr-1")

    assert contract.bid == 1.0
    assert all(name != "get_option_instruments" for name, _ in call_tool.calls)


def test_get_option_quote_fetches_instrument_when_not_cached():
    broker, _ = make_broker()
    call_tool = FakeCallTool({
        "get_option_instruments": {"data": {"instruments": [
            {"id": "instr-1", "chain_symbol": "IWM", "expiration_date": TODAY, "strike_price": "200.0", "type": "call"},
        ]}},
        "get_option_quotes": {"data": {"results": [
            {"quote": {"instrument_id": "instr-1", "bid_price": "1.0", "ask_price": "1.1", "mark_price": "1.05"}},
        ]}},
    })
    broker._call_tool_sync = call_tool

    contract = broker.get_option_quote("instr-1")

    assert contract.strike == 200.0


def test_get_option_quote_raises_when_instrument_not_found():
    broker, _ = make_broker()
    broker._call_tool_sync = FakeCallTool({
        "get_option_instruments": {"data": {"instruments": []}},
    })
    with pytest.raises(MCPError):
        broker.get_option_quote("does-not-exist")


def test_resolve_account_number_prefers_default_among_eligible():
    broker, _ = make_broker()
    broker._call_tool_sync = FakeCallTool({
        "get_accounts": {"data": {"accounts": [
            {"account_number": "AAA", "agentic_allowed": False, "option_level": "option_level_2", "state": "active", "deactivated": False, "permanently_deactivated": False, "is_default": False},
            {"account_number": "BBB", "agentic_allowed": True, "option_level": "option_level_2", "state": "active", "deactivated": False, "permanently_deactivated": False, "is_default": False},
            {"account_number": "CCC", "agentic_allowed": True, "option_level": "option_level_3", "state": "active", "deactivated": False, "permanently_deactivated": False, "is_default": True},
        ]}},
    })
    assert broker._resolve_account_number() == "CCC"


def test_resolve_account_number_raises_when_none_eligible():
    broker, _ = make_broker()
    broker._call_tool_sync = FakeCallTool({
        "get_accounts": {"data": {"accounts": [
            {"account_number": "AAA", "agentic_allowed": False, "option_level": "option_level_2", "state": "active", "deactivated": False, "permanently_deactivated": False, "is_default": True},
        ]}},
    })
    with pytest.raises(MCPError):
        broker._resolve_account_number()


def test_resolve_account_number_is_cached():
    broker, _ = make_broker()
    call_tool = FakeCallTool({
        "get_accounts": {"data": {"accounts": [
            {"account_number": "AAA", "agentic_allowed": True, "option_level": "option_level_2", "state": "active", "deactivated": False, "permanently_deactivated": False, "is_default": True},
        ]}},
    })
    broker._call_tool_sync = call_tool

    broker._resolve_account_number()
    broker._resolve_account_number()

    assert len(call_tool.calls) == 1


def test_extract_cursor_parses_url():
    assert MCPBroker._extract_cursor("https://api.example.com/x?cursor=abc123") == "abc123"


def test_extract_cursor_returns_none_for_empty():
    assert MCPBroker._extract_cursor("") is None
    assert MCPBroker._extract_cursor(None) is None


def _account_setup():
    return {"data": {"accounts": [
        {"account_number": "ACCT1", "agentic_allowed": True, "option_level": "option_level_2", "state": "active", "deactivated": False, "permanently_deactivated": False, "is_default": True},
    ]}}


def test_submit_order_places_after_clean_review():
    broker, _ = make_broker()
    contract = OptionContract("IWM", 200.0, OptionType.CALL, TODAY, 1.0, 1.1, 1.05, "instr-1")
    call_tool = FakeCallTool({
        "get_accounts": _account_setup(),
        "review_option_order": {"data": {"order_checks": {}}},
        "place_option_order": {"data": {"order": {"id": "order-1", "state": "confirmed"}}},
    })
    broker._call_tool_sync = call_tool

    result = broker.submit_order(contract, 2, 1.05, "buy")

    assert result.submitted is True
    assert result.broker_order_id == "order-1"
    place_calls = [args for name, args in call_tool.calls if name == "place_option_order"]
    assert place_calls[0]["legs"] == [{"option_id": "instr-1", "side": "buy", "position_effect": "open"}]
    assert place_calls[0]["quantity"] == "2"


def test_submit_order_uses_close_position_effect_when_selling():
    broker, _ = make_broker()
    contract = OptionContract("IWM", 200.0, OptionType.CALL, TODAY, 1.0, 1.1, 1.05, "instr-1")
    call_tool = FakeCallTool({
        "get_accounts": _account_setup(),
        "review_option_order": {"data": {"order_checks": {}}},
        "place_option_order": {"data": {"order": {"id": "order-2", "state": "confirmed"}}},
    })
    broker._call_tool_sync = call_tool

    broker.submit_order(contract, 1, 1.20, "sell")

    place_calls = [args for name, args in call_tool.calls if name == "place_option_order"]
    assert place_calls[0]["legs"] == [{"option_id": "instr-1", "side": "sell", "position_effect": "close"}]


def test_submit_order_declines_when_order_checks_present():
    broker, _ = make_broker()
    contract = OptionContract("IWM", 200.0, OptionType.CALL, TODAY, 1.0, 1.1, 1.05, "instr-1")
    call_tool = FakeCallTool({
        "get_accounts": _account_setup(),
        "review_option_order": {"data": {"order_checks": {"alertType": "insufficient_buying_power", "details": {}}}},
    })
    broker._call_tool_sync = call_tool

    result = broker.submit_order(contract, 2, 1.05, "buy")

    assert result.submitted is False
    assert "insufficient_buying_power" in result.detail
    assert all(name != "place_option_order" for name, _ in call_tool.calls)


def test_submit_order_fails_cleanly_when_no_eligible_account():
    broker, _ = make_broker()
    contract = OptionContract("IWM", 200.0, OptionType.CALL, TODAY, 1.0, 1.1, 1.05, "instr-1")
    call_tool = FakeCallTool({
        "get_accounts": {"data": {"accounts": []}},
    })
    broker._call_tool_sync = call_tool

    result = broker.submit_order(contract, 2, 1.05, "buy")

    assert result.submitted is False
    assert all(name not in ("review_option_order", "place_option_order") for name, _ in call_tool.calls)


def test_login_connects_then_logs_into_fallback():
    broker, fallback = make_broker()
    broker.connect = lambda: setattr(broker, "_mcp_session", FakeMCPSession({"get_accounts"}))

    broker.login()

    assert "get_accounts" in broker.list_discovered_tools()
    assert "login" in fallback.calls


def test_call_tool_sync_raises_when_not_connected():
    broker, _ = make_broker()
    with pytest.raises(MCPError):
        broker._call_tool_sync("get_portfolio", {})


def test_describe_tool_returns_schema_for_known_tool():
    broker, _ = make_broker()
    tool = FakeTool(
        name="get_option_chains", description="Get option chains for a symbol",
        input_schema={"type": "object", "properties": {"symbol": {"type": "string"}}},
        output_schema={"type": "object"},
    )
    broker._mcp_session = FakeMCPSession({"get_option_chains"}, tools=[tool])

    info = broker.describe_tool("get_option_chains")

    assert info["description"] == "Get option chains for a symbol"
    assert info["input_schema"]["properties"]["symbol"]["type"] == "string"


def test_describe_tool_returns_none_for_unknown_tool():
    broker, _ = make_broker()
    broker._mcp_session = FakeMCPSession({"get_accounts"}, tools=[])
    assert broker.describe_tool("does_not_exist") is None


def test_describe_tool_returns_none_when_not_connected():
    broker, _ = make_broker()
    assert broker.describe_tool("get_accounts") is None
