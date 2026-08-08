import datetime as dt

from iwm_0dte_agent.agent import _build_agent_status, _handle_status_requests
from iwm_0dte_agent.config import Config
from iwm_0dte_agent.models import OpenPosition, OptionContract, OptionType
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

    assert status.position is not None
    assert status.position.current_bid == 3.00
    assert status.position.pnl_pct == 50.0  # (3.00 - 2.00) / 2.00 * 100
    assert status.position.pnl_dollars == 300.0  # (3.00 - 2.00) * 3 * 100
    assert status.buying_power == 25_000.0
    assert status.trades_today == 1
    assert status.max_trades_per_day == 2
    assert status.realized_pnl_today == 50.0


def test_build_agent_status_with_no_position():
    broker = FakeBroker(quote_bid=3.00, buying_power=50.0)
    risk = RiskManager(config=Config())

    status = _build_agent_status(None, broker, risk, Config())

    assert status.position is None
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
