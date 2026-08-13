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


def _ema(values: list[float], length: int) -> float | None:
    """Standard recursive EMA, seeded with the first value. Well-defined
    for any number of samples >= 1, but converges toward the textbook
    weighting only after roughly `length` samples -- see ema_cloud_bias()'s
    docstring for what that means for the 34/50 cloud early in a session."""
    if not values:
        return None
    alpha = 2 / (length + 1)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def ema_cloud_bias(bars: list[Bar]) -> OptionType | None:
    """Ripster EMA Cloud confluence, computed on hl2 ((high+low)/2) per the
    indicator's own default source (not close): bullish when both the fast
    cloud (EMA8/EMA9) and slow cloud (EMA34/EMA50) agree bullish (short >=
    long), bearish when both agree bearish. Returns CALL for bullish, PUT
    for bearish, None when the clouds disagree -- i.e. not an "A+" setup.

    Matches the indicator's "Cloud 1 + Cloud 3 (strict)" confluence mode:
    cloud 1 = EMA8/EMA9 (green=bull/magenta=bear), cloud 3 = EMA34/EMA50
    (blue=bull/orange=bear).

    Caveat: bars only ever cover the current session (this system doesn't
    carry bar history across days), so the 34/50 cloud is still in its
    early "warm-up" window for roughly the first 30-50 minutes after open
    -- less settled than the same indicator on a TradingView chart, which
    has continuous multi-day history to draw on. It's still a well-defined
    number the whole time, just a noisier one early on.
    """
    if not bars:
        return None
    hl2 = [(b.high + b.low) / 2 for b in bars]
    ema8 = _ema(hl2, 8)
    ema9 = _ema(hl2, 9)
    ema34 = _ema(hl2, 34)
    ema50 = _ema(hl2, 50)
    if ema8 >= ema9 and ema34 >= ema50:
        return OptionType.CALL
    if ema8 < ema9 and ema34 < ema50:
        return OptionType.PUT
    return None


def generate_signal(
    bars: list[Bar],
    market_open: dt.time,
    orb_minutes: int,
    use_vwap_filter: bool = True,
    use_volume_filter: bool = True,
    volume_multiplier: float = 1.5,
    volume_lookback_bars: int = 6,
    breakout_buffer_pct: float = 0.001,
    use_ema_cloud_filter: bool = True,
) -> TradeSignal | None:
    """Evaluate the latest bar against the opening range, plus four optional
    confirming filters (each independently toggleable):

    - VWAP: breakout must be on the correct side of session VWAP too.
    - Volume: the breakout bar's volume must beat volume_multiplier x the
      average volume of the last volume_lookback_bars bars, counting only
      bars *after* the opening range closed -- filters out breakouts on
      unconvincing (thin) participation. Two deliberate choices here, both
      found necessary from a real backtest comparison, not assumed upfront:
      a trailing window rather than a cumulative average of every bar since
      open (the first 15-30 minutes of the session is naturally the
      highest-volume part of the day, so a cumulative average stays
      permanently skewed high right when it matters most), AND excluding
      the range-forming bars themselves from that window (they're
      structurally the highest-volume bars of the day, so for the earliest
      breakouts -- where a trailing window has nowhere else to look back to
      yet -- they'd inflate the baseline exactly when a window alone
      couldn't rescue it). Together these keep the baseline representative
      of "recent normal trading" regardless of what time of day the
      breakout happens.
    - Breakout buffer: the close must clear the ORB level by
      breakout_buffer_pct, not just tick through it by any amount -- cuts
      down on immediate-failure breakouts right at the level.
    - EMA cloud confluence: the breakout direction must agree with
      ema_cloud_bias() (Ripster EMA Cloud 8/9 + 34/50 confluence) -- an "A+"
      setup requires the clouds to agree with the breakout, not just VWAP
      and volume. Unlike the other filters, this one blocks (rather than
      passes through) when there isn't a clear bias yet, since "no cloud
      confluence" is the literal definition of not being an A+ setup.

    All four trade signal frequency for signal quality; each can be
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

    # Baseline built only from bars *after* the opening range closed, never
    # from the range-forming bars themselves -- those are structurally the
    # highest-volume bars of the day (that's part of why they're used to
    # form the range) and would otherwise inflate the baseline for exactly
    # the earliest breakouts, the ones a trailing window alone can't rescue
    # since there's nothing else to look back on yet.
    prior_bars = post_range_bars[:-1]
    lookback_bars = prior_bars[-volume_lookback_bars:] if volume_lookback_bars > 0 else prior_bars
    avg_volume = (sum(b.volume for b in lookback_bars) / len(lookback_bars)) if lookback_bars else None

    def volume_confirms() -> bool:
        if not use_volume_filter or not avg_volume:
            return True  # no baseline yet, or filter disabled -- don't block on it
        return latest.volume >= avg_volume * volume_multiplier

    cloud_bias = ema_cloud_bias(bars) if use_ema_cloud_filter else None

    call_trigger = orb_high * (1 + breakout_buffer_pct)
    put_trigger = orb_low * (1 - breakout_buffer_pct)

    if latest.close > call_trigger:
        if use_vwap_filter and current_vwap is not None and latest.close <= current_vwap:
            return None
        if not volume_confirms():
            return None
        if use_ema_cloud_filter and cloud_bias != OptionType.CALL:
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
        if use_ema_cloud_filter and cloud_bias != OptionType.PUT:
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


def atm_contract(chain, option_type: OptionType, underlying_price: float):
    """The contract of the given type whose strike is nearest underlying_price.

    Exposed separately (not just inlined in select_strike_by_dollar_offset
    below) so callers like agent.py can look up "what would ATM even cost"
    for diagnostics when a signal gets skipped, without duplicating this
    lookup.
    """
    same_type = sorted(
        (c for c in chain if c.option_type == option_type), key=lambda c: c.strike
    )
    if not same_type:
        return None
    atm_index = min(range(len(same_type)), key=lambda i: abs(same_type[i].strike - underlying_price))
    return same_type[atm_index]


def select_strike_by_dollar_offset(
    chain, option_type: OptionType, underlying_price: float, dollar_offset: float
):
    """Pick the contract whose strike is closest to underlying_price plus
    (CALL) or minus (PUT) dollar_offset -- e.g. SPY breaking out at $775.00
    with dollar_offset=1.0 targets a $776 strike for a CALL / $774 for a
    PUT, then picks whichever available strike is nearest that target price
    (chains aren't always priced in exact $1 increments near the money, so
    the target itself may not exist as a real strike)."""
    same_type = [c for c in chain if c.option_type == option_type]
    if not same_type:
        return None
    direction = 1 if option_type == OptionType.CALL else -1
    target = underlying_price + direction * dollar_offset
    return min(same_type, key=lambda c: abs(c.strike - target))
