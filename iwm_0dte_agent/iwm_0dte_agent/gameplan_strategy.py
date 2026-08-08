"""Port of the "Gameplan: Hold / Rejection Zones" TradingView indicator.

Two zones -- a "hold" zone (support) and a "rejection" zone (resistance) --
are supplied manually each morning (see Notifier.request_zones). A CALL
signal fires the first time price touches the hold zone and then closes
back above it (optionally requiring a bullish candle); a PUT signal fires
symmetrically off the rejection zone. Each fires at most once per day,
mirroring the original indicator's `not holdConfirmedToday` guards.

`support_broken` / `resistance_broken` (close breaks all the way through a
zone) are tracked and surfaced as alerts but do NOT generate a trade signal
on their own -- the original indicator treats them as an invalidation/status
marker ("Support Broken" / "Resistance Broken"), not an entry trigger like
"Hold Confirmed" / "Rejection Confirmed" are.

Only the "Manual" zone-source mode from the indicator is ported -- the
other four (Premarket Range, Prior Day Range, Classic Pivots, VWAP+ATR)
compute zones automatically and were deliberately left out; ask if you want
one of those added.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .models import Bar, OptionType, TradeSignal


@dataclass(frozen=True)
class GameplanZones:
    hold_low: float
    hold_high: float
    reject_low: float
    reject_high: float

    def valid(self) -> bool:
        return self.hold_high > 0 and self.reject_high > 0


@dataclass(frozen=True)
class GameplanEvaluation:
    signal: TradeSignal | None
    support_broken: bool
    resistance_broken: bool


class GameplanState:
    """Per-day touch/confirmation tracking, mirroring the indicator's `var`
    booleans that reset each new session (`resetDaily` in the Pine script).
    """

    def __init__(self) -> None:
        self._day: dt.date | None = None
        self.touched_hold = False
        self.touched_reject = False
        self.hold_confirmed_today = False
        self.reject_confirmed_today = False
        self.support_broken_today = False
        self.resistance_broken_today = False

    def _roll_day_if_needed(self, today: dt.date) -> None:
        if today != self._day:
            self._day = today
            self.touched_hold = False
            self.touched_reject = False
            self.hold_confirmed_today = False
            self.reject_confirmed_today = False
            self.support_broken_today = False
            self.resistance_broken_today = False

    def evaluate(
        self,
        bar: Bar,
        zones: GameplanZones,
        require_bull_close: bool = True,
        require_bear_close: bool = True,
    ) -> GameplanEvaluation:
        self._roll_day_if_needed(bar.timestamp.date())

        if not zones.valid():
            return GameplanEvaluation(signal=None, support_broken=False, resistance_broken=False)

        if zones.hold_low <= bar.low <= zones.hold_high:
            self.touched_hold = True
        if zones.reject_low <= bar.high <= zones.reject_high:
            self.touched_reject = True

        bull_candle = bar.close > bar.open
        bear_candle = bar.close < bar.open
        signal: TradeSignal | None = None

        hold_confirmed = (
            self.touched_hold
            and bar.close > zones.hold_high
            and (not require_bull_close or bull_candle)
            and not self.hold_confirmed_today
        )
        if hold_confirmed:
            self.hold_confirmed_today = True
            signal = TradeSignal(
                option_type=OptionType.CALL,
                reason=f"hold zone [{zones.hold_low:g}-{zones.hold_high:g}] confirmed, close {bar.close:.2f}",
                underlying_price=bar.close,
            )

        # Zones shouldn't overlap, but if they're misconfigured such that both
        # confirm on the same bar, the later (PUT) check below wins -- only
        # one signal can be acted on per bar either way.
        reject_confirmed = (
            self.touched_reject
            and bar.close < zones.reject_low
            and (not require_bear_close or bear_candle)
            and not self.reject_confirmed_today
        )
        if reject_confirmed:
            self.reject_confirmed_today = True
            signal = TradeSignal(
                option_type=OptionType.PUT,
                reason=f"rejection zone [{zones.reject_low:g}-{zones.reject_high:g}] confirmed, close {bar.close:.2f}",
                underlying_price=bar.close,
            )

        support_broken = bar.close < zones.hold_low and not self.support_broken_today
        if support_broken:
            self.support_broken_today = True

        resistance_broken = bar.close > zones.reject_high and not self.resistance_broken_today
        if resistance_broken:
            self.resistance_broken_today = True

        return GameplanEvaluation(signal=signal, support_broken=support_broken, resistance_broken=resistance_broken)


def parse_zone_message(text: str) -> GameplanZones | None:
    """Parses "hold_low hold_high reject_low reject_high" (space- or
    comma-separated) as sent via Telegram or the terminal prompt. Returns
    None on anything that doesn't cleanly parse to exactly four numbers.
    """
    parts = text.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        hold_low, hold_high, reject_low, reject_high = (float(p) for p in parts)
    except ValueError:
        return None
    return GameplanZones(hold_low=hold_low, hold_high=hold_high, reject_low=reject_low, reject_high=reject_high)
