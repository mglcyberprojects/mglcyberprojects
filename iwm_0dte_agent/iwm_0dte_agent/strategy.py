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


def _bucket_open(bars: list[Bar], bucket_minutes: int) -> float | None:
    """Open price of the still-forming bucket_minutes-wide candle containing
    the most recent bar -- e.g. bucket_minutes=60 during a 10:15 bar returns
    the open of the 10:00-11:00 bucket. Buckets are anchored to the clock
    hour/half-hour (minute - minute % bucket_minutes), matching how
    TradingView's own higher-timeframe candles align, not to market open."""
    if not bars:
        return None
    latest_ts = bars[-1].timestamp
    bucket_start = latest_ts.replace(
        minute=latest_ts.minute - (latest_ts.minute % bucket_minutes), second=0, microsecond=0
    )
    bucket_bars = [b for b in bars if b.timestamp >= bucket_start]
    return bucket_bars[0].open if bucket_bars else None


def ftfc_allows(bars: list[Bar], option_type: OptionType, mode: str) -> bool:
    """Full Timeframe Continuity: requires the current (still-forming) daily
    candle -- and, in "full" mode, the current 60-minute and 30-minute
    candles too -- to also be pointing the breakout's direction (close above
    its own open for a CALL, below for a PUT). mode="off" imposes no
    restriction (always True). mode="daily" checks only the daily candle;
    "full" additionally requires the 60m and 30m candles to agree.

    Computed purely from bars accumulated so far today -- only the
    CURRENTLY forming higher-timeframe candles matter here (their open,
    matched against the latest close), not completed prior ones, so no
    multi-day bar history is needed. The daily candle's open is just the
    first bar of the session; see _bucket_open() for the 60m/30m candles.
    """
    if mode == "off" or not bars:
        return True
    close = bars[-1].close
    day_open = bars[0].open
    if mode == "daily":
        return close > day_open if option_type == OptionType.CALL else close < day_open
    h1_open = _bucket_open(bars, 60)
    m30_open = _bucket_open(bars, 30)
    if h1_open is None or m30_open is None:
        return False
    if option_type == OptionType.CALL:
        return close > day_open and close > h1_open and close > m30_open
    return close < day_open and close < h1_open and close < m30_open


def _true_range(curr: Bar, prev: Bar) -> float:
    return max(curr.high - curr.low, abs(curr.high - prev.close), abs(curr.low - prev.close))


def atr(bars: list[Bar], length: int = 14) -> float | None:
    """Wilder's ATR (average true range). Same same-session-only caveat as
    ema_cloud_bias(): with fewer than `length` samples this is a simple
    average of whatever True Range values exist so far, then switches to
    Wilder's smoothing once there are enough -- a well-defined but noisier
    number early in the session rather than nothing at all."""
    if len(bars) < 2:
        return None
    trs = [_true_range(bars[i], bars[i - 1]) for i in range(1, len(bars))]
    if len(trs) < length:
        return sum(trs) / len(trs)
    value = sum(trs[:length]) / length
    for tr in trs[length:]:
        value = (value * (length - 1) + tr) / length
    return value


def _candle_patterns(latest: Bar, prev: Bar | None) -> tuple[bool, bool, bool, bool]:
    """(bull_pin_bar, bull_engulfing, bear_pin_bar, bear_engulfing) for the
    latest candle, matching the Pine script's exact pin-bar/engulfing math."""
    body = abs(latest.close - latest.open)
    upper_wick = latest.high - max(latest.close, latest.open)
    lower_wick = min(latest.close, latest.open) - latest.low
    bull_pin = lower_wick > body * 2 and upper_wick < body
    bear_pin = upper_wick > body * 2 and lower_wick < body
    bull_engulf = bear_engulf = False
    if prev is not None:
        bull_engulf = (
            latest.close > latest.open and prev.close < prev.open
            and latest.close >= prev.open and latest.open <= prev.close
        )
        bear_engulf = (
            latest.close < latest.open and prev.close > prev.open
            and latest.close <= prev.open and latest.open >= prev.close
        )
    return bull_pin, bull_engulf, bear_pin, bear_engulf


def retest_signal(
    bars: list[Bar],
    market_open: dt.time,
    orb_minutes: int,
    use_continuation: bool = True,
    use_reversal: bool = True,
    require_trend_context: bool = True,
    trend_lookback: int = 5,
    rr_ratio: float = 2.0,
    sl_atr_mult: float = 0.0,
    use_cloud_filter: bool = False,
    use_ftfc_filter: bool = False,
    ftfc_mode: str = "off",
) -> TradeSignal | None:
    """Retest entries: wait for a confirmed ORB breakout at some point this
    session, then a LATER bar that wicks back to retest the broken level but
    closes back outside it, combined with a reversal-style candlestick
    pattern (Hammer or Bullish Engulfing for bullish signals, Shooting Star
    or Bearish Engulfing for bearish). Two independent flavors of the same
    retest condition:

    - Continuation: the pattern agrees with the ORIGINAL breakout direction
      (the level held, trend resumes).
    - Reversal: the pattern disagrees with the original breakout (the
      breakout looks exhausted -- betting on a fade back through the range).

    Entry is the retest candle's close; stop is that candle's low (long) or
    high (short), optionally pushed further out by sl_atr_mult x ATR(14) so
    ordinary noise doesn't tag it immediately; target is rr_ratio x that
    stop distance. This is a self-contained, underlying-price-based R:R
    system independent of STOP_LOSS_PCT/PROFIT_TARGET_PCT -- see
    TradeSignal.underlying_stop_price/underlying_target_price and
    agent.py's _check_exit, which checks both this AND the premium-based
    stop/target and exits on whichever trips first.

    A true Hammer requires a prior local downtrend (else it's a Hanging
    Man -- a weak/contested shape, not a reliable bull signal); a true
    Shooting Star requires a prior local uptrend (else it's an Inverted
    Hammer), when require_trend_context is on (default, matching the
    indicator).

    Ported from the "Retest Entry Signals" section of the attached Pine
    script. Its "Block Reversals Against A Same-Day Confirmed Breakout"
    toggle is NOT implemented here -- it depends on a "confirmed breakout
    already fired today" latch that generate_signal() doesn't track (this
    port re-evaluates every poll cycle rather than firing once per day).
    """
    orb = opening_range(bars, market_open, orb_minutes)
    if orb is None:
        return None
    orb_high, orb_low = orb

    range_end = bars[0].timestamp.replace(
        hour=market_open.hour, minute=market_open.minute, second=0, microsecond=0
    ) + dt.timedelta(minutes=orb_minutes)
    post_range_bars = [b for b in bars if b.timestamp >= range_end]
    if not post_range_bars:
        return None

    latest = post_range_bars[-1]
    prev = post_range_bars[-2] if len(post_range_bars) >= 2 else None

    broke_up = any(b.close > orb_high for b in post_range_bars)
    broke_down = any(b.close < orb_low for b in post_range_bars)
    retest_up_inside = broke_up and latest.low <= orb_high and latest.close >= orb_high
    retest_down_inside = broke_down and latest.high >= orb_low and latest.close <= orb_low
    if not retest_up_inside and not retest_down_inside:
        return None

    bull_pin, bull_engulf, bear_pin, bear_engulf = _candle_patterns(latest, prev)

    lookback_idx = len(post_range_bars) - 1 - trend_lookback
    local_downtrend = local_uptrend = False
    if lookback_idx >= 0:
        local_downtrend = latest.close < post_range_bars[lookback_idx].close
        local_uptrend = latest.close > post_range_bars[lookback_idx].close

    true_hammer = bull_pin and (not require_trend_context or local_downtrend)
    true_shooting_star = bear_pin and (not require_trend_context or local_uptrend)
    bull_pattern = true_hammer or bull_engulf
    bear_pattern = true_shooting_star or bear_engulf

    cloud_bias = ema_cloud_bias(bars) if use_cloud_filter else None
    cloud_bull_ok = not use_cloud_filter or cloud_bias == OptionType.CALL
    cloud_bear_ok = not use_cloud_filter or cloud_bias == OptionType.PUT
    ftfc_long_ok = not use_ftfc_filter or ftfc_allows(bars, OptionType.CALL, ftfc_mode)
    ftfc_short_ok = not use_ftfc_filter or ftfc_allows(bars, OptionType.PUT, ftfc_mode)

    is_long = is_short = False
    kind = fired_zone = ""
    if use_continuation and retest_up_inside and bull_pattern and cloud_bull_ok and ftfc_long_ok:
        is_long, kind, fired_zone = True, "continuation", "high"
    elif use_continuation and retest_down_inside and bear_pattern and cloud_bear_ok and ftfc_short_ok:
        is_short, kind, fired_zone = True, "continuation", "low"
    elif use_reversal and retest_down_inside and bull_pattern and cloud_bull_ok and ftfc_long_ok:
        is_long, kind, fired_zone = True, "reversal", "low"
    elif use_reversal and retest_up_inside and bear_pattern and cloud_bear_ok and ftfc_short_ok:
        is_short, kind, fired_zone = True, "reversal", "high"

    if not is_long and not is_short:
        return None

    entry = latest.close
    atr_value = atr(bars) or 0.0
    if is_long:
        stop = latest.low - atr_value * sl_atr_mult
        target = entry + (entry - stop) * rr_ratio
        option_type = OptionType.CALL
    else:
        stop = latest.high + atr_value * sl_atr_mult
        target = entry - (stop - entry) * rr_ratio
        option_type = OptionType.PUT

    level = orb_high if fired_zone == "high" else orb_low
    return TradeSignal(
        option_type=option_type,
        reason=f"retest {kind}: {'bullish' if is_long else 'bearish'} pattern retesting ORB {fired_zone} {level:.2f}",
        underlying_price=entry,
        orb_high=orb_high,
        orb_low=orb_low,
        underlying_stop_price=stop,
        underlying_target_price=target,
    )


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
    ftfc_mode: str = "off",
    use_dynamic_profit_target: bool = False,
    dynamic_pt_multiplier: float = 2.0,
) -> TradeSignal | None:
    """Evaluate the latest bar against the opening range, plus optional
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
    - FTFC (Full Timeframe Continuity): see ftfc_allows() -- ftfc_mode="off"
      (default) imposes no restriction; "daily" or "full" requires the
      current daily (and, for "full", 60m/30m) candle(s) to also be
      pointing the breakout's direction.

    All trade signal frequency for signal quality; each can be disabled
    independently for testing/comparison.

    When use_dynamic_profit_target is on, the returned signal's
    underlying_target_price is set to dynamic_pt_multiplier x the breakout
    candle's own high-low range, projected from entry in the breakout
    direction (2x by default, matching the Pine script's Profit Target
    Box) -- checked in ADDITION to the usual premium-based
    PROFIT_TARGET_PCT exit, not instead of it (see agent.py's _check_exit).
    When off (default), underlying_target_price is left None and only the
    premium-based target applies, unchanged from before this existed.
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

    def dynamic_target(direction: int) -> float | None:
        if not use_dynamic_profit_target:
            return None
        candle_range = latest.high - latest.low
        return latest.close + direction * candle_range * dynamic_pt_multiplier

    if latest.close > call_trigger:
        if use_vwap_filter and current_vwap is not None and latest.close <= current_vwap:
            return None
        if not volume_confirms():
            return None
        if use_ema_cloud_filter and cloud_bias != OptionType.CALL:
            return None
        if not ftfc_allows(bars, OptionType.CALL, ftfc_mode):
            return None
        return TradeSignal(
            option_type=OptionType.CALL,
            reason=f"close {latest.close:.2f} broke above ORB high {orb_high:.2f}",
            underlying_price=latest.close,
            orb_high=orb_high,
            orb_low=orb_low,
            vwap=current_vwap,
            underlying_target_price=dynamic_target(1),
        )

    if latest.close < put_trigger:
        if use_vwap_filter and current_vwap is not None and latest.close >= current_vwap:
            return None
        if not volume_confirms():
            return None
        if use_ema_cloud_filter and cloud_bias != OptionType.PUT:
            return None
        if not ftfc_allows(bars, OptionType.PUT, ftfc_mode):
            return None
        return TradeSignal(
            option_type=OptionType.PUT,
            reason=f"close {latest.close:.2f} broke below ORB low {orb_low:.2f}",
            underlying_price=latest.close,
            orb_high=orb_high,
            orb_low=orb_low,
            vwap=current_vwap,
            underlying_target_price=dynamic_target(-1),
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
