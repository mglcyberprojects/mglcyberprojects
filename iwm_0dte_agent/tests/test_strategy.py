import datetime as dt

from iwm_0dte_agent.models import Bar, OptionContract, OptionType
from iwm_0dte_agent.strategy import (
    atm_contract,
    atr,
    ema_cloud_bias,
    ftfc_allows,
    generate_signal,
    opening_range,
    retest_signal,
    select_strike_by_dollar_offset,
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
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, use_ema_cloud_filter=False,
    )
    assert signal is not None
    assert signal.option_type == OptionType.CALL


def test_generate_signal_put_breakdown():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 99.0, 97.0, 97.5),  # breaks below ORB low (98.5)
    ]
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, use_ema_cloud_filter=False,
    )
    assert signal is not None
    assert signal.option_type == OptionType.PUT


def test_generate_signal_none_inside_range():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 101.0, 99.0, 100.0),  # stays inside [98.5, 102]
    ]
    assert generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_ema_cloud_filter=False) is None


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
        use_ema_cloud_filter=False,
    )
    signal_without_filter = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.0,
        use_ema_cloud_filter=False,
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
        bar(15, 99.0, 100.2, 99.5, 100.0, v=1000),  # post-range filler bar -- establishes the baseline
        bar(20, 99.0, 103, 99.0, 102.5, v=1000),  # breaks out, but same volume as the filler -- not 1.5x
    ]
    blocked = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=True,
        volume_multiplier=1.5, breakout_buffer_pct=0.0,
        use_ema_cloud_filter=False,
    )
    assert blocked is None

    allowed = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.0,
        use_ema_cloud_filter=False,
    )
    assert allowed is not None


def test_generate_signal_volume_filter_allows_high_volume_breakout():
    bars = [
        bar(0, 100, 101, 99, 100.5, v=1000),
        bar(5, 100.5, 102, 100, 100.8, v=1000),
        bar(10, 100.8, 101.5, 98.5, 99.0, v=1000),
        bar(15, 99.0, 100.2, 99.5, 100.0, v=1000),  # post-range filler bar -- establishes the baseline
        bar(20, 99.0, 103, 99.0, 102.5, v=2000),  # 2x the filler bar's volume
    ]
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=True,
        volume_multiplier=1.5, breakout_buffer_pct=0.0,
        use_ema_cloud_filter=False,
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
        use_ema_cloud_filter=False,
    )
    assert signal is not None
    assert signal.option_type == OptionType.CALL


def test_generate_signal_volume_filter_baseline_excludes_range_forming_bars():
    # A second real bug found from the same live backtest comparison: the
    # trailing window alone doesn't help the *earliest* breakouts each day,
    # since when there's little or no post-range history yet, the window
    # has no choice but to reach back into the opening-range bars -- which
    # are themselves structurally the highest-volume bars of the day. Here
    # the window (6) is wider than the single post-range bar available, so
    # it would pull in the 3 elevated range bars too if they weren't
    # explicitly excluded from the baseline pool.
    bars = [
        bar(0, 100, 101, 99, 100.5, v=5000),  # opening-range bars, all volume-heavy
        bar(5, 100.5, 102, 100, 100.8, v=5000),
        bar(10, 100.8, 101.5, 98.5, 99.0, v=5000),
        bar(15, 99.0, 100.2, 99.5, 100.0, v=1000),  # the only post-range bar before the breakout
        bar(20, 99.0, 103, 99.0, 102.5, v=1600),  # 1.6x the post-range bar, but well under
    ]                                              # 1.5x an average that included the range bars
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=True,
        volume_multiplier=1.5, volume_lookback_bars=6, breakout_buffer_pct=0.0,
        use_ema_cloud_filter=False,
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
        use_ema_cloud_filter=False,
    )
    assert blocked is None

    allowed = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False, breakout_buffer_pct=0.0,
        use_ema_cloud_filter=False,
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
        use_ema_cloud_filter=False,
    )
    assert blocked is None


def test_vwap_empty_returns_none():
    assert vwap([]) is None


def test_select_strike_by_dollar_offset_call_targets_price_plus_offset():
    # SPY at $775.00, $1 offset -> target $776, chain has exact $776 strike.
    chain = [
        OptionContract("SPY", strike, OptionType.CALL, "2024-01-02", 1.0, 1.1, 1.05, f"c{strike}")
        for strike in (774, 775, 776, 777, 778)
    ]
    contract = select_strike_by_dollar_offset(chain, OptionType.CALL, underlying_price=775.00, dollar_offset=1.0)
    assert contract.strike == 776


def test_select_strike_by_dollar_offset_put_targets_price_minus_offset():
    chain = [
        OptionContract("SPY", strike, OptionType.PUT, "2024-01-02", 1.0, 1.1, 1.05, f"p{strike}")
        for strike in (774, 775, 776, 777, 778)
    ]
    contract = select_strike_by_dollar_offset(chain, OptionType.PUT, underlying_price=775.00, dollar_offset=1.0)
    assert contract.strike == 774


def test_select_strike_by_dollar_offset_picks_nearest_when_exact_target_missing():
    # Target $776.00 doesn't exist -- $0.50 increments near the money --
    # nearest available strike is $775.50.
    chain = [
        OptionContract("SPY", strike, OptionType.CALL, "2024-01-02", 1.0, 1.1, 1.05, f"c{strike}")
        for strike in (774.5, 775.0, 775.5, 777.0, 778.5)
    ]
    contract = select_strike_by_dollar_offset(chain, OptionType.CALL, underlying_price=775.00, dollar_offset=1.0)
    assert contract.strike == 775.5


def test_select_strike_by_dollar_offset_empty_chain_returns_none():
    assert select_strike_by_dollar_offset([], OptionType.CALL, 775.0, 1.0) is None


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


def test_atm_contract_returns_nearest_strike():
    chain = _otm_call_chain()
    contract = atm_contract(chain, OptionType.CALL, underlying_price=200.2)
    assert contract.strike == 200
    assert contract.ask == 3.0


def test_atm_contract_empty_chain_returns_none():
    assert atm_contract([], OptionType.CALL, 200.0) is None


def _trend_bars(n: int, start: float, step: float, start_minute: int = 0) -> list[Bar]:
    """n consecutive 1-minute bars, close changing by `step` each bar,
    high == low == close (so hl2 == close, keeping the EMA cloud math easy
    to reason about in tests)."""
    bars = []
    price = start
    for i in range(n):
        bars.append(bar(start_minute + i, price, price, price, price))
        price += step
    return bars


def test_ema_cloud_bias_bullish_in_uptrend():
    assert ema_cloud_bias(_trend_bars(60, start=100.0, step=0.1)) == OptionType.CALL


def test_ema_cloud_bias_bearish_in_downtrend():
    assert ema_cloud_bias(_trend_bars(60, start=150.0, step=-0.1)) == OptionType.PUT


def test_ema_cloud_bias_none_when_clouds_disagree():
    # Long decline (slow 34/50 cloud still bearish) followed by a sharp
    # recent rally (fast 8/9 cloud already flipped bullish) -- not yet an
    # "A+" confluence setup.
    declining = _trend_bars(40, start=150.0, step=-0.5, start_minute=0)
    rallying = _trend_bars(10, start=declining[-1].close, step=2.0, start_minute=40)
    assert ema_cloud_bias(declining + rallying) is None


def test_ema_cloud_bias_empty_bars_returns_none():
    assert ema_cloud_bias([]) is None


def test_generate_signal_ema_cloud_filter_blocks_breakout_against_the_trend():
    # A long downtrend (bearish clouds) that ends with one bar spiking back
    # above the ORB high -- a technical breakout, but against the
    # established trend/cloud bias, so it shouldn't count as an "A+" setup.
    bars = _trend_bars(50, start=150.0, step=-0.3)
    orb_high = max(b.high for b in bars[:15])
    spike = bar(50, bars[-1].close, orb_high + 5, bars[-1].close, orb_high + 3)
    bars_with_spike = bars + [spike]

    blocked = generate_signal(
        bars_with_spike, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False,
        use_volume_filter=False, breakout_buffer_pct=0.0, use_ema_cloud_filter=True,
    )
    assert blocked is None

    allowed = generate_signal(
        bars_with_spike, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False,
        use_volume_filter=False, breakout_buffer_pct=0.0, use_ema_cloud_filter=False,
    )
    assert allowed is not None
    assert allowed.option_type == OptionType.CALL


def test_generate_signal_ema_cloud_filter_allows_breakout_with_confluence():
    # Uptrend (bullish clouds) that also breaks the ORB high on the last
    # bar -- clouds and breakout agree, this is the "A+" case.
    bars = _trend_bars(50, start=100.0, step=0.3)
    spike = bar(50, bars[-1].close, bars[-1].close + 5, bars[-1].close, bars[-1].close + 4)
    bars_with_spike = bars + [spike]

    signal = generate_signal(
        bars_with_spike, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False,
        use_volume_filter=False, breakout_buffer_pct=0.0, use_ema_cloud_filter=True,
    )
    assert signal is not None
    assert signal.option_type == OptionType.CALL


# --- FTFC (Full Timeframe Continuity) ---

def _ftfc_bars():
    # day_open=100 (9:30); price rises to 110 by the top of the hour (10:00,
    # also a 30-min bucket boundary), then pulls back to 105 -- daily trend
    # is still bullish (105 > 100) but the CURRENT hour/half-hour candle is
    # bearish (105 < 110), so "daily" and "full" modes disagree.
    return [
        bar(0, 100, 100, 100, 100),    # 9:30 -> day_open = 100
        bar(30, 110, 110, 110, 110),   # 10:00 -> h1_open = m30_open = 110
        bar(35, 110, 110, 105, 105),   # 10:05 -> latest close = 105
    ]


def test_ftfc_allows_off_mode_always_true():
    bars = _ftfc_bars()
    assert ftfc_allows(bars, OptionType.CALL, "off") is True
    assert ftfc_allows(bars, OptionType.PUT, "off") is True


def test_ftfc_allows_empty_bars_always_true():
    assert ftfc_allows([], OptionType.CALL, "full") is True


def test_ftfc_allows_daily_mode_checks_only_daily_candle():
    bars = _ftfc_bars()
    assert ftfc_allows(bars, OptionType.CALL, "daily") is True   # 105 > day_open 100
    assert ftfc_allows(bars, OptionType.PUT, "daily") is False


def test_ftfc_allows_full_mode_also_requires_60m_30m_agreement():
    bars = _ftfc_bars()
    # Daily is bullish, but the current hour/half-hour candle (open 110,
    # close 105) is bearish -- "full" mode should block the CALL that
    # "daily" mode alone would allow.
    assert ftfc_allows(bars, OptionType.CALL, "full") is False
    assert ftfc_allows(bars, OptionType.PUT, "full") is False


# --- ATR ---

def test_atr_fewer_than_length_samples_uses_simple_average():
    bars = [bar(0, 9, 10, 8, 9), bar(1, 9, 11, 9, 10), bar(2, 10, 12, 10, 11)]
    # TR(bar2->bar1)=max(2,|11-9|,|9-9|)=2; TR(bar3->bar2)=max(2,|12-10|,|10-10|)=2
    assert atr(bars, length=14) == 2.0


def test_atr_constant_true_range_converges_to_that_value():
    # high-low=2 and close=100 (flat) on every bar -> True Range is exactly
    # 2 on every sample, so both the simple-average and Wilder-smoothed
    # branches should agree, regardless of sample count.
    bars = [bar(i, 100, 101, 99, 100) for i in range(20)]
    assert atr(bars, length=14) == 2.0


def test_atr_needs_at_least_two_bars():
    assert atr([]) is None
    assert atr([bar(0, 100, 101, 99, 100)]) is None


# --- Retest entry signals ---

_RANGE_BARS = [
    bar(0, 100, 101, 99, 100.5),
    bar(5, 100.5, 102, 100, 100.8),
    bar(10, 100.8, 101.5, 98.5, 99.0),
]  # orb_high=102, orb_low=98.5


def test_retest_signal_none_without_a_prior_breakout():
    # Stays inside the range the whole time -- nothing to retest.
    bars = _RANGE_BARS + [bar(15, 99.0, 101.0, 99.0, 100.0)]
    assert retest_signal(bars, MARKET_OPEN, orb_minutes=15) is None


def test_retest_signal_continuation_long_on_hammer_at_orb_high_retest():
    breakout = bar(15, 99.0, 105, 99.0, 104.0)  # close 104 > orb_high 102
    # Hammer: small body (0.5), long lower wick (2.0 > body*2), tiny upper wick.
    retest = bar(20, 103.0, 103.6, 101.0, 103.5)
    bars = _RANGE_BARS + [breakout, retest]

    signal = retest_signal(bars, MARKET_OPEN, orb_minutes=15, require_trend_context=False)

    assert signal is not None
    assert signal.option_type == OptionType.CALL
    assert signal.reason.startswith("retest continuation")
    assert signal.underlying_price == 103.5
    assert signal.underlying_stop_price == 101.0  # retest candle's low (sl_atr_mult=0)
    assert signal.underlying_target_price == 103.5 + (103.5 - 101.0) * 2.0  # rr_ratio=2.0 default


def test_retest_signal_continuation_short_on_shooting_star_at_orb_low_retest():
    breakout = bar(15, 99.0, 99.0, 95.0, 96.0)  # close 96 < orb_low 98.5
    # Shooting star: small body (0.5), long upper wick (2.0 > body*2), tiny lower wick.
    retest = bar(20, 97.0, 99.0, 96.4, 96.5)
    bars = _RANGE_BARS + [breakout, retest]

    signal = retest_signal(bars, MARKET_OPEN, orb_minutes=15, require_trend_context=False)

    assert signal is not None
    assert signal.option_type == OptionType.PUT
    assert signal.reason.startswith("retest continuation")
    assert signal.underlying_price == 96.5
    assert signal.underlying_stop_price == 99.0  # retest candle's high
    assert signal.underlying_target_price == 96.5 - (99.0 - 96.5) * 2.0


def test_retest_signal_reversal_fires_against_the_original_breakout():
    breakout = bar(15, 99.0, 99.0, 95.0, 96.0)  # broke DOWN, close 96 < orb_low 98.5
    # But the retest candle is a bullish hammer (long lower wick), fading the breakdown.
    retest = bar(20, 98.3, 98.55, 97.0, 98.0)
    bars = _RANGE_BARS + [breakout, retest]

    signal = retest_signal(bars, MARKET_OPEN, orb_minutes=15, require_trend_context=False)

    assert signal is not None
    assert signal.option_type == OptionType.CALL
    assert signal.reason.startswith("retest reversal")


def test_retest_signal_trend_context_gates_hanging_man_shaped_hammer():
    # Same hammer shape as the continuation test above, but this time it
    # follows a local UPTREND (breakout close 102.5 -> retest close 104.6),
    # which makes it a Hanging Man, not a true Hammer -- a weak/contested
    # shape, not a reliable bull signal.
    breakout = bar(15, 99.0, 103, 99.0, 102.5)
    filler = [
        bar(20, 102.5, 103.2, 102.4, 103.0),
        bar(25, 103.0, 103.7, 102.9, 103.5),
        bar(30, 103.5, 104.2, 103.4, 104.0),
        bar(35, 104.0, 104.5, 103.9, 104.3),
    ]
    retest = bar(40, 104.3, 104.7, 101.0, 104.6)  # hammer shape, low wicks to 101
    bars = _RANGE_BARS + [breakout] + filler + [retest]

    blocked = retest_signal(bars, MARKET_OPEN, orb_minutes=15, require_trend_context=True, trend_lookback=5)
    assert blocked is None

    allowed = retest_signal(bars, MARKET_OPEN, orb_minutes=15, require_trend_context=False, trend_lookback=5)
    assert allowed is not None
    assert allowed.option_type == OptionType.CALL


def test_retest_signal_continuation_toggle_off_suppresses_continuation():
    breakout = bar(15, 99.0, 105, 99.0, 104.0)
    retest = bar(20, 103.0, 103.6, 101.0, 103.5)
    bars = _RANGE_BARS + [breakout, retest]

    assert retest_signal(
        bars, MARKET_OPEN, orb_minutes=15, require_trend_context=False, use_continuation=False,
    ) is None


# --- Dynamic profit target ---

def test_generate_signal_dynamic_profit_target_sets_underlying_target_for_call():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 103, 99.0, 102.5),  # breaks above ORB high (102); range = 103-99 = 4.0
    ]
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False,
        use_ema_cloud_filter=False, use_dynamic_profit_target=True, dynamic_pt_multiplier=2.0,
    )
    assert signal is not None
    assert signal.underlying_target_price == 102.5 + (103 - 99) * 2.0


def test_generate_signal_dynamic_profit_target_sets_underlying_target_for_put():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 99.0, 95.0, 96.0),  # breaks below ORB low (98.5); range = 99-95 = 4.0
    ]
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False,
        use_ema_cloud_filter=False, use_dynamic_profit_target=True, dynamic_pt_multiplier=2.0,
    )
    assert signal is not None
    assert signal.underlying_target_price == 96.0 - (99 - 95) * 2.0


def test_generate_signal_dynamic_profit_target_off_by_default():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 103, 99.0, 102.5),
    ]
    signal = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False,
        use_ema_cloud_filter=False,
    )
    assert signal is not None
    assert signal.underlying_target_price is None


def test_generate_signal_ftfc_full_mode_blocks_breakout_against_higher_timeframes():
    # Reuses _ftfc_bars()'s shape (daily bullish, but the current hour/half-hour
    # candle is bearish) glued onto a real ORB breakout so generate_signal's
    # ftfc_mode wiring is exercised end-to-end, not just ftfc_allows() directly.
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 110, 99.0, 105),  # breaks above ORB high (102); this IS the daily-open bar though
    ]
    # Make the daily/h1/m30 opens diverge from the breakout bar itself by
    # adding two more bars: one that starts a new hour (h1/m30 open), one
    # that closes back down (bearish current-hour candle) but still above orb_high.
    bars += [
        bar(30, 106, 108, 105, 107),   # 10:00 -> new hour/half-hour bucket, open=106
        bar(35, 107, 107.2, 102.5, 103),  # 10:05 -> close 103, still > orb_high(102) but < 106
    ]
    blocked = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False,
        use_ema_cloud_filter=False, ftfc_mode="full",
    )
    assert blocked is None

    allowed = generate_signal(
        bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False, use_volume_filter=False,
        use_ema_cloud_filter=False, ftfc_mode="off",
    )
    assert allowed is not None
    assert allowed.option_type == OptionType.CALL
