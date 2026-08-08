"""Shared yfinance download + DataFrame-to-Bar conversion, used by both
PaperBroker and the backtester so there's one place that knows how to turn
a yfinance response into our Bar model.

Newer yfinance versions return a DataFrame with MultiIndex columns (e.g.
("Open", "IWM") instead of just "Open") even for a single symbol, which
makes `row["Open"]` come back as a pandas Series instead of a scalar and
breaks a plain `float(row["Open"])`. `_bars_from_dataframe` flattens that
away defensively so callers don't have to know or care which shape their
installed yfinance version returns.
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


def download_bars(symbol: str, period: str, interval: str) -> list[Bar]:
    import yfinance as yf

    data = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if data.empty:
        return []
    return _bars_from_dataframe(data)
