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


def test_simulate_day_dynamic_profit_target_closes_on_underlying_move():
    # Breakout candle (9:45) has range 104-99=5.0; with
    # dynamic_pt_multiplier=1.0 the underlying target is 104 + 5*1 = 109.
    # The next bar's close (110) reaches it -- confirms the underlying-price
    # exit trigger fires end-to-end through simulate_day, independent of
    # the option premium's synthetic price.
    bars = [
        bar(9, 30, 100, 101, 99, 100.5),
        bar(9, 35, 100.5, 102, 100, 100.8),
        bar(9, 40, 100.8, 101.5, 98.5, 99.0),
        bar(9, 45, 99.0, 104, 99.0, 104.0),      # breakout; range=5.0
        bar(9, 50, 104.0, 110.5, 103.8, 110.0),  # underlying rallies past the 109 target
    ]
    config = make_config(
        vwap_filter=False, dynamic_profit_target=True, dynamic_pt_multiplier=1.0,
        hard_exit=dt.time(9, 55),
    )

    result = simulate_day(bars, config, starting_buying_power=25_000.0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.option_type == OptionType.CALL
    assert trade.exit_time.time() == dt.time(9, 50)
    assert "underlying target hit" in trade.exit_reason


def test_simulate_day_retest_entry_fires_when_primary_breakout_is_blocked():
    # breakout_buffer_pct=0.05 blocks the PRIMARY ORB signal at 9:45 (close
    # 104 doesn't clear orb_high(102) by 5%) -- but retest_signal() doesn't
    # take a buffer at all, so it still recognizes 9:45 as a breakout and
    # fires a continuation-long entry on the 9:50 hammer-shaped retest of
    # orb_high. The 9:55 bar then rallies past the retest's own R:R target
    # (108.5) -- an "underlying target hit" exit is only possible via a
    # retest (or dynamic-PT) entry, so seeing it here confirms the retest
    # fallback -- not the primary signal -- is what actually opened this.
    bars = [
        bar(9, 30, 100, 101, 99, 100.5),
        bar(9, 35, 100.5, 102, 100, 100.8),
        bar(9, 40, 100.8, 101.5, 98.5, 99.0),
        bar(9, 45, 99.0, 104, 99.0, 104.0),        # breakout close 104, buffer blocks the primary signal
        bar(9, 50, 103.0, 103.6, 101.0, 103.5),    # retest: hammer shape (low wicks to orb_high area)
        bar(9, 55, 103.5, 109.2, 103.4, 109.0),    # rallies past the retest's target (108.5)
    ]
    config = make_config(
        vwap_filter=False, breakout_buffer_pct=0.05, enable_retest_entries=True,
        retest_require_trend_context=False, hard_exit=dt.time(10, 0),
    )

    result = simulate_day(bars, config, starting_buying_power=25_000.0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.option_type == OptionType.CALL
    assert trade.entry_time.time() == dt.time(9, 50)
    assert "underlying target hit" in trade.exit_reason


def test_simulate_day_retest_entries_off_by_default_does_not_fire():
    # Same setup as the retest test above (primary signal blocked by the
    # 5% buffer at every bar, close never clears 102*1.05=107.1), but
    # enable_retest_entries left at its default (False) -- with no retest
    # fallback, nothing should trade at all.
    bars = [
        bar(9, 30, 100, 101, 99, 100.5),
        bar(9, 35, 100.5, 102, 100, 100.8),
        bar(9, 40, 100.8, 101.5, 98.5, 99.0),
        bar(9, 45, 99.0, 104, 99.0, 104.0),
        bar(9, 50, 103.0, 103.6, 101.0, 103.5),
        bar(9, 55, 103.5, 107.0, 103.4, 106.5),  # rallies, but still under the 107.1 buffer threshold
    ]
    config = make_config(vwap_filter=False, breakout_buffer_pct=0.05, hard_exit=dt.time(10, 0))

    result = simulate_day(bars, config, starting_buying_power=25_000.0)

    assert result.trades == []
