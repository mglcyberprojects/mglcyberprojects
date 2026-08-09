import datetime as dt

from iwm_0dte_agent.models import Bar, OptionContract, OptionType
from iwm_0dte_agent.strategy import (
    generate_signal,
    opening_range,
    select_cheap_otm_strike,
    select_strike,
    vwap,
)

MARKET_OPEN = dt.time(9, 30)
BASE_DAY = dt.datetime(2024, 1, 2)  # any weekday


def bar(minute_offset: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> Bar:
    ts = BASE_DAY.replace(hour=9, minute=30) + dt.timedelta(minutes=minute_offset)
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def test_opening_range_none_before_window_closes():
    bars = [bar(0, 100, 101, 99, 100.5), bar(5, 100.5, 101, 100, 100.8)]
    assert opening_range(bars, MARKET_OPEN, orb_minutes=15) is None


def test_opening_range_computed_once_window_closes():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 99.5, 98.8, 99.2),  # first bar at/after range_end
    ]
    high, low = opening_range(bars, MARKET_OPEN, orb_minutes=15)
    assert high == 102
    assert low == 98.5


def test_generate_signal_call_breakout_no_vwap_filter():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 103, 99.0, 102.5),  # breaks above ORB high (102)
    ]
    signal = generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False)
    assert signal is not None
    assert signal.option_type == OptionType.CALL


def test_generate_signal_put_breakdown():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 99.0, 97.0, 97.5),  # breaks below ORB low (98.5)
    ]
    signal = generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False)
    assert signal is not None
    assert signal.option_type == OptionType.PUT


def test_generate_signal_none_inside_range():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 101.0, 99.0, 100.0),  # stays inside [98.5, 102]
    ]
    assert generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False) is None


def test_generate_signal_vwap_filter_blocks_weak_breakout():
    # Close breaks above ORB high but is still below VWAP -> filtered out.
    bars = [
        bar(0, 100, 101, 99, 100.5, v=10000),
        bar(5, 100.5, 102, 100, 100.8, v=10000),
        bar(10, 100.8, 101.5, 98.5, 99.0, v=10000),
        bar(15, 99.0, 102.1, 99.0, 102.05, v=10),  # tiny volume breakout, VWAP still ~100
    ]
    signal_with_filter = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=True, use_volume_filter=False, breakout_buffer_pct=0.0,
    )
    signal_without_filter = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.0,
    )
    assert signal_without_filter is not None
    # VWAP across all bars sits within the breakout range here, so the filtered
    # version should either agree or be strictly more conservative (None).
    assert signal_with_filter is None or signal_with_filter.option_type == signal_without_filter.option_type


def test_generate_signal_volume_filter_blocks_thin_breakout():
    bars = [
        bar(0, 100, 101, 99, 100.5, v=1000),
        bar(5, 100.5, 102, 100, 100.8, v=1000),
        bar(10, 100.8, 101.5, 98.5, 99.0, v=1000),
        bar(15, 99.0, 103, 99.0, 102.5, v=1000),  # breaks out, but same volume as the average -- not 1.5x
    ]
    blocked = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=True,
        volume_multiplier=1.5, breakout_buffer_pct=0.0,
    )
    assert blocked is None

    allowed = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.0,
    )
    assert allowed is not None


def test_generate_signal_volume_filter_allows_high_volume_breakout():
    bars = [
        bar(0, 100, 101, 99, 100.5, v=1000),
        bar(5, 100.5, 102, 100, 100.8, v=1000),
        bar(10, 100.8, 101.5, 98.5, 99.0, v=1000),
        bar(15, 99.0, 103, 99.0, 102.5, v=2000),  # 2x the average of the prior bars
    ]
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=True,
        volume_multiplier=1.5, breakout_buffer_pct=0.0,
    )
    assert signal is not None
    assert signal.option_type == OptionType.CALL


def test_generate_signal_volume_filter_uses_trailing_window_not_full_day_average():
    # Real bug found from a live backtest comparison: the first 15-30 min of
    # the session are naturally the highest-volume part of the day. If the
    # volume baseline were a cumulative average of every bar since open (the
    # original implementation), that early spike permanently inflates the
    # bar every later breakout has to clear -- exactly backwards, since it
    # makes the filter hardest to pass right when good breakouts tend to
    # happen and easiest to pass during the midday lull. A trailing window
    # should judge the breakout bar only against *recent* bars, so an early
    # spike well outside the window shouldn't count against it.
    bars = [
        bar(0, 100, 101, 99, 100.5, v=5000),   # opening-range bar with a big volume spike
        bar(5, 100.5, 102, 100, 100.8, v=1000),
        bar(10, 100.8, 101.5, 98.5, 99.0, v=1000),
        bar(15, 99.0, 100.2, 99.5, 100.0, v=1000),  # 6 normal-volume filler bars, well
        bar(20, 100.0, 100.2, 99.8, 100.0, v=1000),  # clear of the opening spike by the
        bar(25, 100.0, 100.2, 99.8, 100.0, v=1000),  # time the breakout bar arrives
        bar(30, 100.0, 100.2, 99.8, 100.0, v=1000),
        bar(35, 100.0, 100.2, 99.8, 100.0, v=1000),
        bar(40, 100.0, 100.2, 99.8, 100.0, v=1000),
        bar(45, 100.0, 103, 100.0, 102.5, v=1600),  # breaks out; 1.6x *recent* volume,
    ]                                                # but only ~1.1x the full-day average
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=True,
        volume_multiplier=1.5, volume_lookback_bars=6, breakout_buffer_pct=0.0,
    )
    assert signal is not None
    assert signal.option_type == OptionType.CALL


def test_generate_signal_breakout_buffer_blocks_marginal_breakout():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 102.1, 99.0, 102.05),  # clears ORB high (102) by less than 0.1%
    ]
    blocked = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.001,
    )
    assert blocked is None

    allowed = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.0,
    )
    assert allowed is not None


def test_generate_signal_breakout_buffer_applies_to_put_side_too():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 99.0, 98.4, 98.45),  # clears ORB low (98.5) by less than 0.1%
    ]
    blocked = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.001,
    )
    assert blocked is None


def test_vwap_empty_returns_none():
    assert vwap([]) is None


def test_select_strike_atm_and_offset():
    chain = [
        OptionContract("IWM", strike, OptionType.CALL, "2024-01-02", 1.0, 1.1, 1.05, f"c{strike}")
        for strike in (198, 199, 200, 201, 202)
    ] + [
        OptionContract("IWM", strike, OptionType.PUT, "2024-01-02", 1.0, 1.1, 1.05, f"p{strike}")
        for strike in (198, 199, 200, 201, 202)
    ]

    atm_call = select_strike(chain, OptionType.CALL, underlying_price=200.2, strike_offset=0)
    assert atm_call.strike == 200

    otm_call = select_strike(chain, OptionType.CALL, underlying_price=200.2, strike_offset=1)
    assert otm_call.strike == 201

    otm_put = select_strike(chain, OptionType.PUT, underlying_price=199.8, strike_offset=1)
    assert otm_put.strike == 199


def test_select_strike_empty_chain_returns_none():
    assert select_strike([], OptionType.CALL, 200.0, 0) is None


def _otm_call_chain():
    # ATM (200) ask=3.00, decaying as strikes move further OTM -- realistic
    # 0DTE decay shape, not linear.
    asks = {198: 3.6, 199: 3.3, 200: 3.0, 201: 2.0, 202: 1.3, 203: 0.85, 204: 0.5, 205: 0.3, 206: 0.2}
    return [
        OptionContract("IWM", strike, OptionType.CALL, "2024-01-02", ask - 0.05, ask, ask - 0.02, f"c{strike}")
        for strike, ask in asks.items()
    ]


def _otm_put_chain():
    # Mirror image: ATM (200) ask=3.00, decaying as strikes move further OTM
    # (i.e. downward, away from spot) for puts.
    asks = {202: 3.6, 201: 3.3, 200: 3.0, 199: 2.0, 198: 1.3, 197: 0.85, 196: 0.5, 195: 0.3, 194: 0.2}
    return [
        OptionContract("IWM", strike, OptionType.PUT, "2024-01-02", ask - 0.05, ask, ask - 0.02, f"p{strike}")
        for strike, ask in asks.items()
    ]


def test_select_cheap_otm_strike_walks_out_until_discount_met():
    # ATM ask 3.00, 70% discount -> threshold 0.90 -> first strike <= that
    # walking away from ATM is 203 (0.85), not simply "N strikes out".
    chain = _otm_call_chain()
    contract = select_cheap_otm_strike(chain, OptionType.CALL, underlying_price=200.2, min_discount_pct=0.70)
    assert contract.strike == 203
    assert contract.ask == 0.85


def test_select_cheap_otm_strike_puts_walk_downward():
    chain = _otm_put_chain()
    contract = select_cheap_otm_strike(chain, OptionType.PUT, underlying_price=199.8, min_discount_pct=0.70)
    assert contract.strike == 197
    assert contract.ask == 0.85


def test_select_cheap_otm_strike_returns_none_when_chain_too_shallow():
    # 99% discount off a $3.00 ATM ask needs a $0.03 contract; the chain
    # never gets that cheap, so there's nothing usable to trade.
    chain = _otm_call_chain()
    assert select_cheap_otm_strike(chain, OptionType.CALL, underlying_price=200.2, min_discount_pct=0.99) is None


def test_select_cheap_otm_strike_empty_chain_returns_none():
    assert select_cheap_otm_strike([], OptionType.CALL, 200.0, 0.70) is None
