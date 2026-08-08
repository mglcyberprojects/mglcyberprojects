import datetime as dt

from iwm_0dte_agent.gameplan_strategy import GameplanState, GameplanZones, parse_zone_message
from iwm_0dte_agent.models import Bar, OptionType

DAY1 = dt.date(2024, 1, 2)
DAY2 = dt.date(2024, 1, 3)
ZONES = GameplanZones(hold_low=98.0, hold_high=99.0, reject_low=102.0, reject_high=103.0)


def bar(day: dt.date, hour: int, minute: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(
        timestamp=dt.datetime.combine(day, dt.time(hour, minute)),
        open=o, high=h, low=l, close=c, volume=1000.0,
    )


def test_touch_hold_then_bullish_close_above_confirms_call():
    state = GameplanState()
    touch = bar(DAY1, 9, 30, 99.5, 99.6, 98.5, 98.7)  # low dips into hold zone
    confirm = bar(DAY1, 9, 35, 98.7, 100.5, 98.6, 100.2)  # bullish close above hold_high

    assert state.evaluate(touch, ZONES).signal is None
    evaluation = state.evaluate(confirm, ZONES)

    assert evaluation.signal is not None
    assert evaluation.signal.option_type == OptionType.CALL
    assert evaluation.signal.underlying_price == 100.2


def test_touch_reject_then_bearish_close_below_confirms_put():
    state = GameplanState()
    touch = bar(DAY1, 9, 30, 102.0, 102.6, 101.9, 102.3)  # high pokes into reject zone
    confirm = bar(DAY1, 9, 35, 102.3, 102.4, 100.5, 100.8)  # bearish close below reject_low

    assert state.evaluate(touch, ZONES).signal is None
    evaluation = state.evaluate(confirm, ZONES)

    assert evaluation.signal is not None
    assert evaluation.signal.option_type == OptionType.PUT


def test_close_above_without_touch_does_not_confirm():
    state = GameplanState()
    bar_no_touch = bar(DAY1, 9, 30, 100.0, 105.0, 100.0, 104.5)  # never dipped into hold zone
    evaluation = state.evaluate(bar_no_touch, ZONES)
    assert evaluation.signal is None


def test_hold_confirmation_fires_only_once_per_day():
    state = GameplanState()
    touch = bar(DAY1, 9, 30, 99.5, 99.6, 98.5, 98.7)
    confirm1 = bar(DAY1, 9, 35, 98.7, 100.5, 98.6, 100.2)
    confirm2 = bar(DAY1, 9, 40, 100.2, 100.6, 100.0, 100.4)  # still above hold_high

    state.evaluate(touch, ZONES)
    first = state.evaluate(confirm1, ZONES)
    second = state.evaluate(confirm2, ZONES)

    assert first.signal is not None
    assert second.signal is None


def test_state_resets_on_new_day():
    state = GameplanState()
    touch = bar(DAY1, 9, 30, 99.5, 99.6, 98.5, 98.7)
    confirm = bar(DAY1, 9, 35, 98.7, 100.5, 98.6, 100.2)
    state.evaluate(touch, ZONES)
    state.evaluate(confirm, ZONES)
    assert state.hold_confirmed_today is True

    # New day: touching+confirming again should fire a fresh signal.
    touch2 = bar(DAY2, 9, 30, 99.5, 99.6, 98.5, 98.7)
    confirm2 = bar(DAY2, 9, 35, 98.7, 100.5, 98.6, 100.2)
    state.evaluate(touch2, ZONES)
    evaluation = state.evaluate(confirm2, ZONES)

    assert evaluation.signal is not None
    assert evaluation.signal.option_type == OptionType.CALL


def test_support_and_resistance_broken_flags():
    state = GameplanState()
    breakdown = bar(DAY1, 9, 30, 98.0, 98.0, 97.0, 97.5)  # close below hold_low
    breakout = bar(DAY1, 9, 35, 103.5, 104.0, 103.4, 103.8)  # close above reject_high

    down_eval = state.evaluate(breakdown, ZONES)
    up_eval = state.evaluate(breakout, ZONES)

    assert down_eval.support_broken is True
    assert down_eval.signal is None  # breaks don't generate trade signals on their own
    assert up_eval.resistance_broken is True


def test_broken_flags_fire_only_once_per_day():
    state = GameplanState()
    breakdown1 = bar(DAY1, 9, 30, 98.0, 98.0, 97.0, 97.5)
    breakdown2 = bar(DAY1, 9, 35, 97.5, 97.6, 97.0, 97.2)

    first = state.evaluate(breakdown1, ZONES)
    second = state.evaluate(breakdown2, ZONES)

    assert first.support_broken is True
    assert second.support_broken is False


def test_invalid_zones_never_signal():
    state = GameplanState()
    invalid = GameplanZones(hold_low=0.0, hold_high=0.0, reject_low=0.0, reject_high=0.0)
    b = bar(DAY1, 9, 30, 100.0, 105.0, 95.0, 102.0)
    evaluation = state.evaluate(b, invalid)
    assert evaluation.signal is None
    assert evaluation.support_broken is False
    assert evaluation.resistance_broken is False


def test_require_bull_close_false_allows_bearish_candle_to_confirm_hold():
    state = GameplanState()
    touch = bar(DAY1, 9, 30, 99.5, 99.6, 98.5, 98.7)
    # Bearish candle (close < open) that still closes above hold_high.
    confirm = bar(DAY1, 9, 35, 101.0, 101.2, 100.0, 100.2)

    state.evaluate(touch, ZONES, require_bull_close=True)
    blocked = state.evaluate(confirm, ZONES, require_bull_close=True)
    assert blocked.signal is None  # bearish candle, filter requires bull

    state2 = GameplanState()
    state2.evaluate(touch, ZONES, require_bull_close=False)
    allowed = state2.evaluate(confirm, ZONES, require_bull_close=False)
    assert allowed.signal is not None


def test_parse_zone_message_space_separated():
    zones = parse_zone_message("228.50 229.20 231.00 232.50")
    assert zones == GameplanZones(hold_low=228.50, hold_high=229.20, reject_low=231.00, reject_high=232.50)


def test_parse_zone_message_comma_separated():
    zones = parse_zone_message("228.50, 229.20, 231.00, 232.50")
    assert zones == GameplanZones(hold_low=228.50, hold_high=229.20, reject_low=231.00, reject_high=232.50)


def test_parse_zone_message_wrong_count_returns_none():
    assert parse_zone_message("228.50 229.20 231.00") is None
    assert parse_zone_message("228.50 229.20 231.00 232.50 999") is None


def test_parse_zone_message_non_numeric_returns_none():
    assert parse_zone_message("hold high reject high") is None


def test_parse_zone_message_empty_returns_none():
    assert parse_zone_message("") is None
