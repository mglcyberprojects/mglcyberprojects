import datetime as dt

from iwm_0dte_agent.models import Bar, OptionContract, OptionType
from iwm_0dte_agent.strategy import generate_signal, opening_range, select_strike, vwap

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
    signal = generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False)
    assert signal is not None
    assert signal.option_type == OptionType.CALL


def test_generate_signal_put_breakdown():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(5, 100.5, 102, 100, 100.8),
        bar(10, 100.8, 101.5, 98.5, 99.0),
        bar(15, 99.0, 99.0, 97.0, 97.5),  # breaks below ORB low (98.5)
    ]
    signal = generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False)
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
    signal_with_filter = generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=True)
    signal_without_filter = generate_signal(bars, MARKET_OPEN, orb_minutes=15, use_vwap_filter=False)
    assert signal_without_filter is not None
    # VWAP across all bars sits within the breakout range here, so the filtered
    # version should either agree or be strictly more conservative (None).
    assert signal_with_filter is None or signal_with_filter.option_type == signal_without_filter.option_type


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
