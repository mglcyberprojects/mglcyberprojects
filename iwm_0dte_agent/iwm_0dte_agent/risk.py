"""Position sizing and daily risk limits.

Kept as a small stateful class with pure-ish methods so it's easy to unit
test: feed it synthetic account/PnL numbers and assert on the decisions it
makes, no broker or clock dependency required.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import Config


@dataclass
class RiskManager:
    config: Config
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    _day: dt.date = field(default_factory=dt.date.today)

    def _roll_day_if_needed(self) -> None:
        today = dt.date.today()
        if today != self._day:
            self._day = today
            self.trades_today = 0
            self.realized_pnl_today = 0.0

    def record_trade_closed(self, pnl: float) -> None:
        self._roll_day_if_needed()
        self.realized_pnl_today += pnl

    def record_trade_opened(self) -> None:
        self._roll_day_if_needed()
        self.trades_today += 1

    def daily_loss_limit_hit(self, buying_power: float) -> bool:
        self._roll_day_if_needed()
        if buying_power <= 0:
            return True
        loss_pct = -self.realized_pnl_today / buying_power
        return loss_pct >= self.config.max_daily_loss_pct

    def can_open_new_trade(self, buying_power: float, now: dt.time) -> tuple[bool, str]:
        self._roll_day_if_needed()
        if self.trades_today >= self.config.max_trades_per_day:
            return False, f"max trades/day reached ({self.trades_today})"
        if self.daily_loss_limit_hit(buying_power):
            return False, f"daily loss limit reached ({self.realized_pnl_today:.2f})"
        if now >= self.config.entry_cutoff:
            return False, f"past entry cutoff ({self.config.entry_cutoff})"
        return True, "ok"

    def is_hard_exit_time(self, now: dt.time) -> bool:
        return now >= self.config.hard_exit

    def position_size(self, buying_power: float, premium_per_contract: float) -> int:
        """Contracts sized so max loss (at stop_loss_pct) <= risk_pct_per_trade of buying power."""
        if premium_per_contract <= 0:
            return 0
        risk_dollars = buying_power * self.config.risk_pct_per_trade
        max_loss_per_contract = premium_per_contract * 100 * self.config.stop_loss_pct
        if max_loss_per_contract <= 0:
            return 0
        contracts = int(risk_dollars // max_loss_per_contract)
        return max(0, min(contracts, self.config.max_contracts_per_trade))
