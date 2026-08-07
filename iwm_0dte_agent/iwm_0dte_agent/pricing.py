"""Minimal Black-Scholes pricer used only by the paper broker.

This exists purely to give the simulator plausible-looking option prices when
no live chain is available. It is not accurate enough for anything but demo
purposes -- it assumes a flat implied volatility and ignores dividends,
early exercise, and the bid/ask skew real 0DTE chains show near expiry.
"""

from __future__ import annotations

import math
from statistics import NormalDist

from .models import OptionType

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
