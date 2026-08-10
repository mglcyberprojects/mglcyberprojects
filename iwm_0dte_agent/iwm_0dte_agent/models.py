"""Shared data types used across broker, strategy, and risk modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OptionContract:
    """A single 0DTE option contract quote, broker-agnostic."""

    symbol: str
    strike: float
    option_type: OptionType
    expiration: str  # ISO date, should equal today for 0DTE
    bid: float
    ask: float
    mid: float
    contract_id: str  # broker-specific identifier, opaque to strategy/risk code


@dataclass(frozen=True)
class TradeSignal:
    """Emitted by any strategy (ORB, gameplan, ...). orb_high/orb_low/vwap
    are ORB-specific context and left None for strategies that don't have
    them -- only option_type/reason/underlying_price are used downstream."""

    option_type: OptionType
    reason: str
    underlying_price: float
    orb_high: float | None = None
    orb_low: float | None = None
    vwap: float | None = None


@dataclass(frozen=True)
class ProposedOrder:
    contract: OptionContract
    quantity: int
    limit_price: float
    stop_loss_price: float
    profit_target_price: float
    reason: str
    # Set only when this proposal closes an existing position (the price it
    # was originally opened at), so a confirm() UI can show % gain/loss.
    # None for entry proposals, where there's nothing yet to compare against.
    entry_price: float | None = None
    # Set only on entry proposals: the affordable quantities (e.g. 1/3/5
    # contracts) a confirm() UI should let the human pick between, instead
    # of a single fixed `quantity`. None for close proposals, which always
    # close the existing position's full size -- there's nothing to choose.
    quantity_choices: list[int] | None = None

    @property
    def pnl_pct(self) -> float | None:
        if self.entry_price is None:
            return None
        return (self.limit_price - self.entry_price) / self.entry_price * 100

    @property
    def pnl_dollars(self) -> float | None:
        if self.entry_price is None:
            return None
        return (self.limit_price - self.entry_price) * self.quantity * 100


@dataclass
class OpenPosition:
    contract: OptionContract
    quantity: int
    entry_price: float
    stop_loss_price: float
    profit_target_price: float
    opened_at: datetime


@dataclass(frozen=True)
class OrderResult:
    submitted: bool
    broker_order_id: str | None
    detail: str


@dataclass(frozen=True)
class PositionStatus:
    """Live snapshot of an open position for an on-demand status request --
    current_bid is fetched fresh each time, unlike OpenPosition's static
    entry_price/thresholds."""

    contract: OptionContract
    quantity: int
    entry_price: float
    current_bid: float
    stop_loss_price: float
    profit_target_price: float

    @property
    def pnl_pct(self) -> float:
        return (self.current_bid - self.entry_price) / self.entry_price * 100

    @property
    def pnl_dollars(self) -> float:
        return (self.current_bid - self.entry_price) * self.quantity * 100


@dataclass(frozen=True)
class AgentStatus:
    """Everything an on-demand status/refresh reply needs to render --
    broker- and notifier-agnostic, built by agent.py and rendered into
    channel-specific text by whichever Notifier is in use.

    positions is a list (today always length 0 or 1, since the agent only
    ever holds one position at a time by design -- see README.md) rather
    than a single optional field, so /positions renders as a list and is
    ready if that one-at-a-time design ever changes, without another
    render-layer rewrite."""

    positions: list[PositionStatus]
    buying_power: float
    trades_today: int
    max_trades_per_day: int
    realized_pnl_today: float
