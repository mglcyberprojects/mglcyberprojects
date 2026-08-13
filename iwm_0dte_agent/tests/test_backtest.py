import datetime as dt

from iwm_0dte_agent.backtest import simulate_day
from iwm_0dte_agent.config import Config
from iwm_0dte_agent.gameplan_strategy import GameplanZones
from iwm_0dte_agent.models import Bar, OptionType

BASE_DAY = dt.datetime(2024, 1, 2)  # any weekday


def bar(hour: int, minute: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> Bar:
    ts = BASE_DAY.replace(hour=hour, minute=minute)
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def make_config(**overrides) -> Config:
    defaults = dict(
        market_open=dt.time(9, 30),
        orb_minutes=15,
        vwap_filter=True,
        volume_filter=False,  # these tests use flat volume bars, not testing this filter
        ema_cloud_filter=False,  # these tests use 3-6 synthetic bars, nowhere near enough for a
                                 # meaningful 34/50 cloud reading -- not what these tests are about
        stop_loss_pct=0.10,     # loose enough that 5 min of pure theta decay won't trip it
        profit_target_pct=5.0,  # high enough that it won't spuriously trip either
        hard_exit=dt.time(9, 50),
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_simulate_day_no_data_returns_skipped_result():
    result = simulate_day([], make_config())
    assert result.trades == []
    assert result.skipped_reason == "no data"


def test_simulate_day_no_breakout_produces_no_trades():
    bars = [
        bar(9, 30, 100, 101, 99, 100.5),
        bar(9, 35, 100.5, 101, 100, 100.8),
        bar(9, 40, 100.8, 101.5, 98.5, 99.0),
        bar(9, 45, 99.0, 101.0, 99.0, 100.0),  # stays inside the opening range
        bar(9, 50, 100.0, 101.0, 99.5, 100.2),
    ]
    result = simulate_day(bars, make_config())
    assert result.trades == []


def test_simulate_day_breakout_then_hard_exit_closes_position():
    bars = [
        bar(9, 30, 100, 101, 99, 100.5),
        bar(9, 35, 100.5, 101, 100, 100.8),
        bar(9, 40, 100.8, 101.5, 98.5, 99.0),
        bar(9, 45, 99.0, 106, 99.0, 105.0),  # breaks well above the ORB high
        bar(9, 50, 105.0, 105.2, 104.8, 105.0),  # flat -- hard exit at 9:50 should fire here
    ]
    result = simulate_day(bars, make_config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.option_type == OptionType.CALL
    assert trade.entry_time.time() == dt.time(9, 45)
    assert trade.exit_time.time() == dt.time(9, 50)
    assert "hard exit" in trade.exit_reason
    assert trade.quantity > 0


def test_simulate_day_respects_max_trades_per_day():
    # Same breakout bars as above, but capped at 0 trades/day.
    bars = [
        bar(9, 30, 100, 101, 99, 100.5),
        bar(9, 35, 100.5, 101, 100, 100.8),
        bar(9, 40, 100.8, 101.5, 98.5, 99.0),
        bar(9, 45, 99.0, 106, 99.0, 105.0),
        bar(9, 50, 105.0, 105.2, 104.8, 105.0),
    ]
    result = simulate_day(bars, make_config(max_trades_per_day=0))
    assert result.trades == []


def test_simulate_day_gameplan_strategy_touch_then_confirm_then_hard_exit():
    bars = [
        bar(9, 30, 99.5, 99.6, 98.5, 98.7),   # low dips into hold zone [98, 99]
        bar(9, 35, 98.7, 100.5, 98.6, 100.2),  # bullish close back above hold_high -> CALL confirmed
        bar(9, 40, 100.2, 100.4, 100.0, 100.2),  # flat -- hard exit at 9:40 fires here
    ]
    zones = GameplanZones(hold_low=98.0, hold_high=99.0, reject_low=102.0, reject_high=103.0)
    config = make_config(strategy="gameplan", hard_exit=dt.time(9, 40))

    result = simulate_day(bars, config, gameplan_zones=zones)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.option_type == OptionType.CALL
    assert trade.entry_time.time() == dt.time(9, 35)
    assert trade.exit_time.time() == dt.time(9, 40)
    assert "hard exit" in trade.exit_reason


def test_simulate_day_gameplan_strategy_without_zones_never_trades():
    bars = [
        bar(9, 30, 99.5, 99.6, 98.5, 98.7),
        bar(9, 35, 98.7, 100.5, 98.6, 100.2),
        bar(9, 40, 100.2, 100.4, 100.0, 100.2),
    ]
    config = make_config(strategy="gameplan", hard_exit=dt.time(9, 40))

    result = simulate_day(bars, config, gameplan_zones=None)

    assert result.trades == []


# Opening range gives orb_high=102, then a strong breakout to a close of
# 104 at 9:45.
_BREAKOUT_BARS = [
    bar(9, 30, 100, 101, 99, 100.5),
    bar(9, 35, 100.5, 102, 100, 100.8),
    bar(9, 40, 100.8, 101.5, 98.5, 99.0),
    bar(9, 45, 99.0, 104, 99.0, 104.0),  # breaks well above the ORB high (102)
    bar(9, 50, 104.0, 104.2, 103.8, 104.0),  # flat -- hard exit at 9:50 fires here
]


def test_simulate_day_picks_strike_by_dollar_offset():
    # Breakout closes at 104.0; default STRIKE_DOLLAR_OFFSET=1.0 targets a
    # $105 call (underlying + 1), not the $104 ATM strike -- confirms
    # backtest.py's strike selection matches agent.py's, not the old
    # STRIKE_OFFSET/select_strike mechanism.
    config = make_config(vwap_filter=False, strike_dollar_offset=1.0)

    result = simulate_day(_BREAKOUT_BARS, config, starting_buying_power=25_000.0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.option_type == OptionType.CALL
    assert trade.strike == 105.0
