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
) -> TradeSignal | None:
    """Evaluate the latest bar against the opening range and (optionally) VWAP."""
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

    if latest.close > orb_high:
        if use_vwap_filter and current_vwap is not None and latest.close <= current_vwap:
            return None
        return TradeSignal(
            option_type=OptionType.CALL,
            reason=f"close {latest.close:.2f} broke above ORB high {orb_high:.2f}",
            underlying_price=latest.close,
            orb_high=orb_high,
            orb_low=orb_low,
            vwap=current_vwap,
        )

    if latest.close < orb_low:
        if use_vwap_filter and current_vwap is not None and latest.close >= current_vwap:
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
