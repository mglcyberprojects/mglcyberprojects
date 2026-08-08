import datetime as dt

from iwm_0dte_agent.broker import RobinhoodBroker
from iwm_0dte_agent.config import Config


class FakeStocks:
    def __init__(self, historicals):
        self._historicals = historicals

    def get_stock_historicals(self, symbol, interval, span, bounds):
        return self._historicals


class FakeRobinStocks:
    def __init__(self, historicals):
        self.stocks = FakeStocks(historicals)


def _historical(begins_at_utc: str, close: float) -> dict:
    return {
        "begins_at": begins_at_utc,
        "open_price": str(close),
        "high_price": str(close),
        "low_price": str(close),
        "close_price": str(close),
        "volume": "1000",
    }


def _to_local_naive(begins_at_utc: str) -> dt.datetime:
    return dt.datetime.fromisoformat(begins_at_utc.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def test_get_intraday_bars_does_not_crash_comparing_utc_bars_to_naive_since():
    # robin_stocks returns begins_at as UTC ("...Z"); `since` (from
    # agent._today_open) is always naive local time -- comparing them
    # directly used to raise "can't compare offset-naive and
    # offset-aware datetimes" on every live call, before the underlying
    # price bars strategy.py needs could ever be fetched.
    recent = (dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    broker = RobinhoodBroker(Config())
    broker._rh = FakeRobinStocks([_historical(recent, 100.0)])

    since = dt.datetime.now() - dt.timedelta(days=1)
    bars = broker.get_intraday_bars("IWM", since=since)  # must not raise

    assert len(bars) == 1
    assert bars[0].close == 100.0
    assert bars[0].timestamp.tzinfo is None  # normalized to naive local, like every other timestamp here


def test_get_intraday_bars_filters_bars_before_since():
    early, late = "2024-01-02T09:00:00Z", "2024-01-02T20:00:00Z"
    broker = RobinhoodBroker(Config())
    broker._rh = FakeRobinStocks([_historical(early, 90.0), _historical(late, 110.0)])

    midpoint = _to_local_naive(early) + (_to_local_naive(late) - _to_local_naive(early)) / 2
    bars = broker.get_intraday_bars("IWM", since=midpoint)

    assert len(bars) == 1
    assert bars[0].close == 110.0
