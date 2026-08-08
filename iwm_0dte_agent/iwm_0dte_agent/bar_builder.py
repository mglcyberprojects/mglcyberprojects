"""In-process OHLC bar accumulation from repeated point-in-time quotes.

Robinhood's official MCP server has no historical-bars tool -- only a live
quote (`get_equity_quotes`), unlike `robin_stocks`, which can hand back a
whole day of ready-made 5-minute candles in one call. To avoid needing
`robin_stocks` at all in `--live` mode, this turns the quotes the agent is
already polling every cycle into the same shape `strategy.py` expects, one
sample at a time, bucketed into fixed-width bars.

Because bars only exist from whenever the agent started polling, the
opening range (ORB) is only accurate if the agent is running at or before
market open -- there's no way to backfill a bar for a period before this
process was alive.
"""

from __future__ import annotations

import datetime as dt

from .models import Bar


class BarBuilder:
    def __init__(self, bucket_minutes: int = 5):
        self._bucket_minutes = bucket_minutes
        self._bars: dict[dt.datetime, Bar] = {}

    def _bucket_start(self, at: dt.datetime) -> dt.datetime:
        floored_minute = at.minute - (at.minute % self._bucket_minutes)
        return at.replace(minute=floored_minute, second=0, microsecond=0)

    def add_quote(self, price: float, at: dt.datetime) -> None:
        bucket = self._bucket_start(at)
        existing = self._bars.get(bucket)
        if existing is None:
            self._bars[bucket] = Bar(
                timestamp=bucket, open=price, high=price, low=price, close=price, volume=0.0,
            )
            return
        self._bars[bucket] = Bar(
            timestamp=bucket,
            open=existing.open,
            high=max(existing.high, price),
            low=min(existing.low, price),
            close=price,
            volume=0.0,
        )

    def bars_since(self, since: dt.datetime) -> list[Bar]:
        return sorted((b for ts, b in self._bars.items() if ts >= since), key=lambda b: b.timestamp)
