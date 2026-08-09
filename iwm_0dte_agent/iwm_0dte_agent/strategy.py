"""Opening Range Breakout (ORB) strategy with an optional VWAP trend filter.

Rules:
  1. Wait for `orb_minutes` after the open to establish a high/low range.
  2. A close above the range high (and above VWAP, if the filter is on)
     signals a CALL. A close below the range low (and below VWAP) signals a
     PUT.
  3. Only one signal is taken per day per direction -- the caller is
     responsible for tracking whether a position is already open.

Pure functions only: no I/O, no broker calls, so this is trivially unit
testable against synthetic bar data.
"""

from __future__ import annotations

import datetime as dt

from .models import Bar, OptionType, TradeSignal


def opening_range(bars: list[Bar], market_open: dt.time, orb_minutes: int) -> tuple[float, float] | None:
    """Return (high, low) of the opening range, or None if not enough bars yet."""
    if not bars:
        return None
    open_dt = bars[0].timestamp.replace(
        hour=market_open.hour, minute=market_open.minute, second=0, microsecond=0
    )
    range_end = open_dt + dt.timedelta(minutes=orb_minutes)
    range_bars = [b for b in bars if open_dt <= b.timestamp < range_end]
    if not range_bars:
        return None
    latest_bar_time = bars[-1].timestamp
    if latest_bar_time < range_end:
        return None  # opening range window hasn't closed yet
    high = max(b.high for b in range_bars)
    low = min(b.low for b in range_bars)
    return high, low


def vwap(bars: list[Bar]) -> float | None:
    if not bars:
        return None
    total_pv = sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars)
    total_v = sum(b.volume for b in bars)
    if total_v == 0:
        return None
    return total_pv / total_v


def generate_signal(
    bars: list[Bar],
    market_open: dt.time,
    orb_minutes: int,
    use_vwap_filter: bool = True,
    use_volume_filter: bool = True,
    volume_multiplier: float = 1.5,
    breakout_buffer_pct: float = 0.001,
) -> TradeSignal | None:
    """Evaluate the latest bar against the opening range, plus three optional
    confirming filters (each independently toggleable):

    - VWAP: breakout must be on the correct side of session VWAP too.
    - Volume: the breakout bar's volume must beat volume_multiplier x the
      average volume of every bar so far today -- filters out breakouts on
      unconvincing (thin) participation.
    - Breakout buffer: the close must clear the ORB level by
      breakout_buffer_pct, not just tick through it by any amount -- cuts
      down on immediate-failure breakouts right at the level.

    All three trade signal frequency for signal quality; each can be
    disabled independently for testing/comparison.
    """
    orb = opening_range(bars, market_open, orb_minutes)
    if orb is None:
        return None
    orb_high, orb_low = orb

    post_range_bars = [
        b for b in bars
        if b.timestamp >= bars[0].timestamp.replace(
            hour=market_open.hour, minute=market_open.minute, second=0, microsecond=0
        ) + dt.timedelta(minutes=orb_minutes)
    ]
    if not post_range_bars:
        return None

    latest = post_range_bars[-1]
    current_vwap = vwap(bars) if use_vwap_filter else None

    prior_bars = bars[:-1]  # everything before the latest bar -- today's volume baseline
    avg_volume = (sum(b.volume for b in prior_bars) / len(prior_bars)) if prior_bars else None

    def volume_confirms() -> bool:
        if not use_volume_filter or not avg_volume:
            return True  # no baseline yet, or filter disabled -- don't block on it
        return latest.volume >= avg_volume * volume_multiplier

    call_trigger = orb_high * (1 + breakout_buffer_pct)
    put_trigger = orb_low * (1 - breakout_buffer_pct)

    if latest.close > call_trigger:
        if use_vwap_filter and current_vwap is not None and latest.close <= current_vwap:
            return None
        if not volume_confirms():
            return None
        return TradeSignal(
            option_type=OptionType.CALL,
            reason=f"close {latest.close:.2f} broke above ORB high {orb_high:.2f}",
            underlying_price=latest.close,
            orb_high=orb_high,
            orb_low=orb_low,
            vwap=current_vwap,
        )

    if latest.close < put_trigger:
        if use_vwap_filter and current_vwap is not None and latest.close >= current_vwap:
            return None
        if not volume_confirms():
            return None
        return TradeSignal(
            option_type=OptionType.PUT,
            reason=f"close {latest.close:.2f} broke below ORB low {orb_low:.2f}",
            underlying_price=latest.close,
            orb_high=orb_high,
            orb_low=orb_low,
            vwap=current_vwap,
        )

    return None


def select_strike(chain, option_type: OptionType, underlying_price: float, strike_offset: int):
    """Pick the contract nearest ATM, shifted `strike_offset` strikes OTM."""
    same_type = sorted(
        (c for c in chain if c.option_type == option_type), key=lambda c: c.strike
    )
    if not same_type:
        return None
    atm_index = min(range(len(same_type)), key=lambda i: abs(same_type[i].strike - underlying_price))
    direction = 1 if option_type == OptionType.CALL else -1
    target_index = atm_index + direction * strike_offset
    target_index = max(0, min(target_index, len(same_type) - 1))
    return same_type[target_index]


def select_cheap_otm_strike(
    chain, option_type: OptionType, underlying_price: float, min_discount_pct: float
):
    """Walk strikes out-of-the-money (away from ATM) until the contract's ask
    is at least `min_discount_pct` cheaper than the ATM contract's ask.

    For small-account smoke testing, where even one ATM 0DTE contract can
    cost more than the whole account -- picks the first strike cheap enough,
    rather than a fixed number of strikes out, since how many strikes that
    takes varies with the day's implied volatility.
    """
    same_type = sorted(
        (c for c in chain if c.option_type == option_type), key=lambda c: c.strike
    )
    if not same_type:
        return None
    atm_index = min(range(len(same_type)), key=lambda i: abs(same_type[i].strike - underlying_price))
    atm_ask = same_type[atm_index].ask
    if atm_ask <= 0:
        return None
    threshold = atm_ask * (1 - min_discount_pct)
    direction = 1 if option_type == OptionType.CALL else -1
    index = atm_index
    while 0 <= index < len(same_type):
        candidate = same_type[index]
        if 0 < candidate.ask <= threshold:
            return candidate
        index += direction
    return None  # chain doesn't extend far enough OTM to hit the discount target
