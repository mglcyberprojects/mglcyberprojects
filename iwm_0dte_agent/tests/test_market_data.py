import datetime as dt

import pandas as pd
import pytest

from iwm_0dte_agent.market_data import _bars_from_dataframe, download_bars


def _index():
    return pd.DatetimeIndex(
        [dt.datetime(2024, 1, 2, 9, 30), dt.datetime(2024, 1, 2, 9, 35)]
    )


def _tz_aware_index():
    # yfinance's intraday index frequently comes back localized to the
    # exchange's own timezone rather than naive.
    return _index().tz_localize("America/New_York")


def test_bars_from_dataframe_flat_columns():
    df = pd.DataFrame(
        {
            "Open": [100.0, 100.5],
            "High": [101.0, 101.2],
            "Low": [99.5, 100.0],
            "Close": [100.5, 100.8],
            "Volume": [1000, 1200],
        },
        index=_index(),
    )

    bars = _bars_from_dataframe(df)

    assert len(bars) == 2
    assert bars[0].open == 100.0
    assert bars[0].close == 100.5
    assert bars[1].volume == 1200


def test_bars_from_dataframe_multiindex_columns():
    # Reproduces the shape newer yfinance versions return even for a single
    # symbol: columns like ("Open", "IWM") instead of just "Open". Before
    # market_data.py existed, `float(row["Open"])` blew up here with
    # "TypeError: float() argument must be a string or a real number, not
    # 'Series'" -- this test is the regression guard for that crash.
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["IWM"]])
    df = pd.DataFrame(
        [[100.0, 101.0, 99.5, 100.5, 1000], [100.5, 101.2, 100.0, 100.8, 1200]],
        index=_index(),
        columns=columns,
    )

    bars = _bars_from_dataframe(df)

    assert len(bars) == 2
    assert bars[0].open == 100.0
    assert bars[0].close == 100.5
    assert bars[1].volume == 1200


def test_download_bars_returns_empty_list_for_empty_response(monkeypatch):
    import yfinance as yf

    monkeypatch.setattr(yf, "download", lambda *a, **k: pd.DataFrame())

    assert download_bars("IWM", period="1d", interval="5m") == []


def test_download_bars_parses_multiindex_response(monkeypatch):
    import yfinance as yf

    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["IWM"]])
    fake_df = pd.DataFrame(
        [[100.0, 101.0, 99.5, 100.5, 1000]],
        index=_index()[:1],
        columns=columns,
    )
    monkeypatch.setattr(yf, "download", lambda *a, **k: fake_df)

    bars = download_bars("IWM", period="1d", interval="5m")

    assert len(bars) == 1
    assert bars[0].close == 100.5


def test_bars_from_dataframe_normalizes_timezone_aware_index():
    # Reproduces a real crash: yfinance's intraday index frequently comes
    # back timezone-aware (localized to the exchange's timezone), while
    # every other timestamp in this codebase -- including `since` in
    # PaperBroker.get_intraday_bars -- is naive local time. Comparing them
    # directly (`b.timestamp >= since`) raised "can't compare offset-naive
    # and offset-aware datetimes" in paper trading, the same class of bug
    # already fixed once in RobinhoodBroker.get_intraday_bars (broker.py).
    df = pd.DataFrame(
        {
            "Open": [100.0, 100.5],
            "High": [101.0, 101.2],
            "Low": [99.5, 100.0],
            "Close": [100.5, 100.8],
            "Volume": [1000, 1200],
        },
        index=_tz_aware_index(),
    )

    bars = _bars_from_dataframe(df)  # must not raise

    assert len(bars) == 2
    assert bars[0].timestamp.tzinfo is None  # normalized to naive local
    # The actual comparison PaperBroker.get_intraday_bars performs -- must
    # not raise TypeError now that both sides are naive.
    since = dt.datetime(2024, 1, 2, 0, 0)
    assert all(b.timestamp >= since for b in bars)
