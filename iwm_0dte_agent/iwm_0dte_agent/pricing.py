"""Minimal Black-Scholes pricer, plus the synthetic option chain/quote
builders shared by the paper broker and the backtester.

This exists purely to give the simulator plausible-looking option prices when
no live chain is available. It is not accurate enough for anything but demo
purposes -- it assumes a flat implied volatility and ignores dividends,
early exercise, and the bid/ask skew real 0DTE chains show near expiry.
"""

from __future__ import annotations

import math
from statistics import NormalDist

from .models import OptionContract, OptionType

_NORM = NormalDist()


def bs_price(
    spot: float,
    strike: float,
    years_to_expiry: float,
    option_type: OptionType,
    iv: float = 0.20,
    rate: float = 0.0,
) -> float:
    years_to_expiry = max(years_to_expiry, 1e-6)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * years_to_expiry) / (
        iv * math.sqrt(years_to_expiry)
    )
    d2 = d1 - iv * math.sqrt(years_to_expiry)
    if option_type == OptionType.CALL:
        price = spot * _NORM.cdf(d1) - strike * math.exp(-rate * years_to_expiry) * _NORM.cdf(d2)
    else:
        price = strike * math.exp(-rate * years_to_expiry) * _NORM.cdf(-d2) - spot * _NORM.cdf(-d1)
    return max(price, 0.01)


def synthetic_quote(
    symbol: str, strike: float, option_type: OptionType, expiration: str,
    spot: float, years_to_expiry: float,
) -> OptionContract:
    mid = bs_price(spot, strike, years_to_expiry, option_type)
    spread = max(0.02, mid * 0.05)
    bid, ask = round(mid - spread / 2, 2), round(mid + spread / 2, 2)
    return OptionContract(
        symbol=symbol,
        strike=float(strike),
        option_type=option_type,
        expiration=expiration,
        bid=max(bid, 0.01),
        ask=ask,
        mid=round((bid + ask) / 2, 2),
        contract_id=f"paper-{symbol}-{expiration}-{strike}-{option_type.value}",
    )


def synthetic_chain(
    symbol: str, spot: float, expiration: str, years_to_expiry: float, strike_width: int = 5,
) -> list[OptionContract]:
    strikes = [round(spot) + offset for offset in range(-strike_width, strike_width + 1)]
    return [
        synthetic_quote(symbol, strike, option_type, expiration, spot, years_to_expiry)
        for strike in strikes
        for option_type in (OptionType.CALL, OptionType.PUT)
    ]
