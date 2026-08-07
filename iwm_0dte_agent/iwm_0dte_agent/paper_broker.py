"""Simulated broker used for --dry-run (the default mode).

Pulls real intraday price data for the underlying via yfinance so the
strategy sees realistic price action, but never talks to Robinhood and never
places a real order. The option chain and fills are synthetic (Black-Scholes
with a flat IV), clearly good enough to exercise the strategy/risk logic and
demo the agent, but not to be mistaken for a live quote.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from typing import Sequence

from .broker import Broker
from .config import Config
from .models import Bar, OptionContract, OptionType, OrderResult
from .pricing import bs_price

logger = logging.getLogger(__name__)


class PaperBroker(Broker):
    def __init__(self, config: Config, starting_buying_power: float = 25_000.0):
        self._config = config
        self._buying_power = starting_buying_power

    def login(self) -> None:
        logger.info("[paper] no login required in dry-run mode")

    def get_buying_power(self) -> float:
        return self._buying_power

    def get_underlying_price(self, symbol: str) -> float:
        bars = self._download(symbol)
        if not bars:
            raise RuntimeError(f"No price data available for {symbol}")
        return bars[-1].close

    def get_intraday_bars(self, symbol: str, since: dt.datetime) -> list[Bar]:
        return [b for b in self._download(symbol) if b.timestamp >= since]

    def _download(self, symbol: str) -> list[Bar]:
        import yfinance as yf

        data = yf.download(
            symbol, period="1d", interval="5m", progress=False, auto_adjust=False
        )
        bars: list[Bar] = []
        for ts, row in data.iterrows():
            bars.append(
                Bar(
                    timestamp=ts.to_pydatetime(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]),
                )
            )
        return bars

    def _years_to_expiry(self) -> float:
        now = dt.datetime.now()
        close = now.replace(
            hour=self._config.market_close.hour,
            minute=self._config.market_close.minute,
            second=0,
            microsecond=0,
        )
        seconds_left = max((close - now).total_seconds(), 60.0)
        return seconds_left / (365 * 24 * 3600)

    def get_0dte_chain(self, symbol: str) -> Sequence[OptionContract]:
        spot = self.get_underlying_price(symbol)
        today = dt.date.today().isoformat()
        tte = self._years_to_expiry()
        strikes = [round(spot) + offset for offset in range(-5, 6)]
        contracts = []
        for strike in strikes:
            for option_type in (OptionType.CALL, OptionType.PUT):
                mid = bs_price(spot, strike, tte, option_type)
                spread = max(0.02, mid * 0.05)
                bid, ask = round(mid - spread / 2, 2), round(mid + spread / 2, 2)
                contracts.append(
                    OptionContract(
                        symbol=symbol,
                        strike=float(strike),
                        option_type=option_type,
                        expiration=today,
                        bid=max(bid, 0.01),
                        ask=ask,
                        mid=round((bid + ask) / 2, 2),
                        contract_id=f"paper-{symbol}-{today}-{strike}-{option_type.value}",
                    )
                )
        return contracts

    def get_option_quote(self, contract_id: str) -> OptionContract:
        _, symbol, expiration, strike, option_type = contract_id.split("-")
        spot = self.get_underlying_price(symbol)
        tte = self._years_to_expiry()
        opt_type = OptionType(option_type)
        mid = bs_price(spot, float(strike), tte, opt_type)
        spread = max(0.02, mid * 0.05)
        bid, ask = round(mid - spread / 2, 2), round(mid + spread / 2, 2)
        return OptionContract(
            symbol=symbol,
            strike=float(strike),
            option_type=opt_type,
            expiration=expiration,
            bid=max(bid, 0.01),
            ask=ask,
            mid=round((bid + ask) / 2, 2),
            contract_id=contract_id,
        )

    def submit_order(
        self,
        contract: OptionContract,
        quantity: int,
        limit_price: float,
        side: str,
    ) -> OrderResult:
        fill_price = round(limit_price * random.uniform(0.99, 1.01), 2)
        notional = fill_price * quantity * 100
        if side == "buy":
            self._buying_power -= notional
        else:
            self._buying_power += notional
        logger.info(
            "[paper] filled %s %d x %s %s %.2f @ %.2f (buying power now %.2f)",
            side, quantity, contract.symbol, contract.option_type.value,
            contract.strike, fill_price, self._buying_power,
        )
        return OrderResult(
            submitted=True,
            broker_order_id=f"paper-fill-{dt.datetime.now().timestamp():.0f}",
            detail=f"simulated fill at {fill_price}",
        )
