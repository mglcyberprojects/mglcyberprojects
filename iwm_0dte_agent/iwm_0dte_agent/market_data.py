"""Shared yfinance download + DataFrame-to-Bar conversion, used by both
PaperBroker and the backtester so there's one place that knows how to turn
a yfinance response into our Bar model.

Newer yfinance versions return a DataFrame with MultiIndex columns (e.g.
("Open", "IWM") instead of just "Open") even for a single symbol, which
makes `row["Open"]` come back as a pandas Series instead of a scalar and
breaks a plain `float(row["Open"])`. `_bars_from_dataframe` flattens that
away defensively so callers don't have to know or care which shape their
installed yfinance version returns.

yfinance's intraday index also frequently comes back timezone-aware
(localized to the exchange's own timezone, e.g. America/New_York) rather
than naive. Every other timestamp in this codebase (market_open/
entry_cutoff/etc., and `since` in get_intraday_bars callers) is naive local
time -- comparing an aware bar timestamp against that raises "can't compare
offset-naive and offset-aware datetimes" the same way it did in
RobinhoodBroker.get_intraday_bars (see broker.py) before that was fixed.
Normalized here the same way: convert to naive local time if aware, leave
alone if the installed yfinance version already returns naive timestamps.
"""

from __future__ import annotations

from .models import Bar


def _bars_from_dataframe(data) -> list[Bar]:
    import pandas as pd

    if isinstance(data.columns, pd.MultiIndex):
        data = data.copy()
        data.columns = data.columns.get_level_values(0)

    bars: list[Bar] = []
    for ts, row in data.iterrows():
        timestamp = ts.to_pydatetime()
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone().replace(tzinfo=None)
        bars.append(
            Bar(
                timestamp=timestamp,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
            )
        )
    return bars


def download_bars(symbol: str, period: str, interval: str) -> list[Bar]:
    import yfinance as yf

    data = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if data.empty:
        return []
    return _bars_from_dataframe(data)
