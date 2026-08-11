import datetime as dt

from iwm_0dte_agent.agent import _build_agent_status, _handle_status_requests, _try_open_position
from iwm_0dte_agent.config import Config
from iwm_0dte_agent.models import Bar, OpenPosition, OptionContract, OptionType
from iwm_0dte_agent.notifier import StatusRequest
from iwm_0dte_agent.risk import RiskManager


class FakeBroker:
    def __init__(self, quote_bid: float, buying_power: float):
        self.quote_bid = quote_bid
        self.buying_power = buying_power
        self.get_option_quote_calls = 0
        self.get_buying_power_calls = 0

    def get_option_quote(self, contract_id):
        self.get_option_quote_calls += 1
        return OptionContract("IWM", 228.0, OptionType.CALL, "2026-08-10", self.quote_bid, self.quote_bid + 0.05, self.quote_bid, contract_id)

    def get_buying_power(self):
        self.get_buying_power_calls += 1
        return self.buying_power


class FakeStatusNotifier:
    def __init__(self, pending):
        self._pending = pending
        self.posted = []
        self.updated = []

    def poll_status_requests(self):
        return self._pending

    def post_status(self, status):
        self.posted.append(status)

    def update_status(self, message_id, status):
        self.updated.append((message_id, status))


def _make_position(entry_price=2.00, stop_loss=1.00, profit_target=4.00) -> OpenPosition:
    contract = OptionContract("IWM", 228.0, OptionType.CALL, "2026-08-10", 2.00, 2.05, 2.02, "instr-1")
    return OpenPosition(
        contract=contract, quantity=3, entry_price=entry_price,
        stop_loss_price=stop_loss, profit_target_price=profit_target, opened_at=dt.datetime.now(),
    )


def test_build_agent_status_with_open_position_computes_unrealized_pnl():
    broker = FakeBroker(quote_bid=3.00, buying_power=25_000.0)
    risk = RiskManager(config=Config())
    risk.trades_today = 1
    risk.realized_pnl_today = 50.0
    position = _make_position(entry_price=2.00)

    status = _build_agent_status(position, broker, risk, Config(max_trades_per_day=2))

    assert len(status.positions) == 1
    assert status.positions[0].current_bid == 3.00
    assert status.positions[0].pnl_pct == 50.0  # (3.00 - 2.00) / 2.00 * 100
    assert status.positions[0].pnl_dollars == 300.0  # (3.00 - 2.00) * 3 * 100
    assert status.buying_power == 25_000.0
    assert status.trades_today == 1
    assert status.max_trades_per_day == 2
    assert status.realized_pnl_today == 50.0


def test_build_agent_status_with_no_position():
    broker = FakeBroker(quote_bid=3.00, buying_power=50.0)
    risk = RiskManager(config=Config())

    status = _build_agent_status(None, broker, risk, Config())

    assert status.positions == []
    assert broker.get_option_quote_calls == 0  # no position -> no quote fetch needed


def test_handle_status_requests_does_nothing_when_no_pending():
    broker = FakeBroker(quote_bid=3.00, buying_power=50.0)
    risk = RiskManager(config=Config())
    notifier = FakeStatusNotifier(pending=[])

    _handle_status_requests(notifier, broker, risk, None, Config())

    assert notifier.posted == []
    assert notifier.updated == []
    assert broker.get_buying_power_calls == 0  # never builds a status if nothing asked for one


def test_handle_status_requests_dispatches_new_to_post_status():
    broker = FakeBroker(quote_bid=3.00, buying_power=50.0)
    risk = RiskManager(config=Config())
    notifier = FakeStatusNotifier(pending=[StatusRequest(kind="new")])

    _handle_status_requests(notifier, broker, risk, None, Config())

    assert len(notifier.posted) == 1
    assert notifier.updated == []


def test_handle_status_requests_dispatches_refresh_to_update_status_with_message_id():
    broker = FakeBroker(quote_bid=3.00, buying_power=50.0)
    risk = RiskManager(config=Config())
    notifier = FakeStatusNotifier(pending=[StatusRequest(kind="refresh", message_id=555)])

    _handle_status_requests(notifier, broker, risk, None, Config())

    assert notifier.posted == []
    assert len(notifier.updated) == 1
    assert notifier.updated[0][0] == 555


def test_handle_status_requests_reuses_one_snapshot_for_multiple_pending():
    broker = FakeBroker(quote_bid=3.00, buying_power=50.0)
    risk = RiskManager(config=Config())
    notifier = FakeStatusNotifier(pending=[StatusRequest(kind="new"), StatusRequest(kind="refresh", message_id=1)])

    _handle_status_requests(notifier, broker, risk, None, Config())

    assert broker.get_buying_power_calls == 1  # one snapshot built, reused for both requests
    assert len(notifier.posted) == 1
    assert len(notifier.updated) == 1


# --- _try_open_position: skip-branch diagnostic logging ---
#
# These exercise the two "signal fired but nothing tradeable" branches that
# were previously silent in trade_log.csv (no record of *why* a repeated
# signal never became a trade) -- added after a real session showed the
# same PUT signal re-firing every poll cycle with "no usable contract" and
# there was nothing in the log to explain what threshold it was missing by.

BASE_DAY = dt.datetime(2024, 1, 2)


def _entry_bar(minute_offset: int, o: float, h: float, l: float, c: float) -> Bar:
    ts = BASE_DAY.replace(hour=9, minute=30) + dt.timedelta(minutes=minute_offset)
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=1000.0)


def _put_breakdown_bars() -> list[Bar]:
    # ORB low ends up at 200.2; the last bar closes at 199.8, breaking
    # below it -- and 199.8 is unambiguously closer to strike 200 than 199
    # in _otm_put_chain() below, so atm_contract() picks 200 deterministically.
    return [
        _entry_bar(0, 205, 206, 204, 205.5),
        _entry_bar(5, 205.5, 207, 205, 205.8),
        _entry_bar(10, 205.8, 206.5, 200.2, 202.0),
        _entry_bar(15, 202.0, 202.0, 199.0, 199.8),
    ]


def _otm_put_chain() -> list[OptionContract]:
    # ATM (200) ask=3.00, decaying as strikes move further OTM (downward,
    # away from spot) -- same shape as test_strategy.py's fixture.
    asks = {202: 3.6, 201: 3.3, 200: 3.0, 199: 2.0, 198: 1.3, 197: 0.85, 196: 0.5, 195: 0.3, 194: 0.2}
    return [
        OptionContract("IWM", strike, OptionType.PUT, "2024-01-02", ask - 0.05, ask, ask - 0.02, f"p{strike}")
        for strike, ask in asks.items()
    ]


class FakeEntryBroker:
    def __init__(self, buying_power: float, chain: list[OptionContract]):
        self.buying_power = buying_power
        self.chain = chain

    def get_buying_power(self):
        return self.buying_power

    def get_intraday_bars(self, symbol, since):
        return _put_breakdown_bars()

    def get_0dte_chain(self, symbol):
        return self.chain


class FakeTradeLog:
    def __init__(self):
        self.entries: list[dict] = []

    def write(self, event: str, **fields) -> None:
        self.entries.append({"event": event, **fields})


class FakeAlertNotifier:
    def __init__(self):
        self.alerts: list[str] = []

    def alert(self, text: str) -> None:
        self.alerts.append(text)

    def confirm(self, order, live: bool) -> int:
        raise AssertionError("confirm() should not be reached when the signal is skipped")


def _entry_config(**overrides) -> Config:
    defaults = dict(
        vwap_filter=False, cheap_otm_mode=True, entry_cutoff=dt.time.max,
        hard_exit=dt.time.max, max_trades_per_day=2, max_daily_loss_pct=0.5,
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_try_open_position_logs_diagnostics_when_no_usable_contract():
    # Discount target so strict (99%) nothing in the chain clears it --
    # the same failure mode as a chain that just doesn't extend far enough OTM.
    config = _entry_config(otm_min_discount_pct=0.99)
    broker = FakeEntryBroker(buying_power=50.0, chain=_otm_put_chain())
    trade_log = FakeTradeLog()
    notifier = FakeAlertNotifier()
    risk = RiskManager(config=config)

    result = _try_open_position(broker, risk, trade_log, config, live=False, notifier=notifier, alerted_reasons=set())

    assert result is None
    logged = [e for e in trade_log.entries if e["event"] == "skipped_no_contract"]
    assert len(logged) == 1
    assert logged[0]["option_type"] == "put"
    assert "atm_ask=3.0" in logged[0]["detail"]
    assert "threshold=0.03" in logged[0]["detail"]
    # Logged, but no Telegram/terminal alert -- a signal missing the
    # discount target isn't worth pinging about every time it happens.
    assert notifier.alerts == []


def test_try_open_position_logs_diagnostics_when_no_affordable_quantity():
    # Default-ish 70% discount finds strike 197 (ask 0.85, $85/contract) --
    # affordable on a normal account, but not on $50 buying power.
    config = _entry_config(otm_min_discount_pct=0.70)
    broker = FakeEntryBroker(buying_power=50.0, chain=_otm_put_chain())
    trade_log = FakeTradeLog()
    notifier = FakeAlertNotifier()
    risk = RiskManager(config=config)

    result = _try_open_position(broker, risk, trade_log, config, live=False, notifier=notifier, alerted_reasons=set())

    assert result is None
    logged = [e for e in trade_log.entries if e["event"] == "skipped_no_quantity"]
    assert len(logged) == 1
    assert logged[0]["strike"] == 197
    assert logged[0]["price"] == 0.85
    assert "buying_power=50.00" in logged[0]["detail"]
    assert "cost_per_contract=85.00" in logged[0]["detail"]
    assert notifier.alerts == []
