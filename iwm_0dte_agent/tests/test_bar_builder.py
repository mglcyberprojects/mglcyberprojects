import datetime as dt

from iwm_0dte_agent.bar_builder import BarBuilder


def test_first_quote_in_a_bucket_opens_the_bar():
    builder = BarBuilder(bucket_minutes=5)
    at = dt.datetime(2024, 1, 2, 9, 31, 12)

    builder.add_quote(100.0, at)

    bars = builder.bars_since(dt.datetime(2024, 1, 2, 0, 0))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timestamp == dt.datetime(2024, 1, 2, 9, 30)  # floored to the bucket start
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 100.0, 100.0, 100.0)


def test_later_quotes_in_the_same_bucket_update_high_low_close_but_not_open():
    builder = BarBuilder(bucket_minutes=5)
    builder.add_quote(100.0, dt.datetime(2024, 1, 2, 9, 31, 0))
    builder.add_quote(102.0, dt.datetime(2024, 1, 2, 9, 32, 0))
    builder.add_quote(98.0, dt.datetime(2024, 1, 2, 9, 33, 0))
    builder.add_quote(99.5, dt.datetime(2024, 1, 2, 9, 34, 0))

    bars = builder.bars_since(dt.datetime(2024, 1, 2, 0, 0))

    assert len(bars) == 1
    bar = bars[0]
    assert bar.open == 100.0
    assert bar.high == 102.0
    assert bar.low == 98.0
    assert bar.close == 99.5


def test_quotes_in_different_buckets_produce_separate_bars_in_order():
    builder = BarBuilder(bucket_minutes=5)
    builder.add_quote(100.0, dt.datetime(2024, 1, 2, 9, 41, 0))
    builder.add_quote(101.0, dt.datetime(2024, 1, 2, 9, 31, 0))  # out of order arrival

    bars = builder.bars_since(dt.datetime(2024, 1, 2, 0, 0))

    assert [b.timestamp for b in bars] == [
        dt.datetime(2024, 1, 2, 9, 30),
        dt.datetime(2024, 1, 2, 9, 40),
    ]


def test_bars_since_filters_out_earlier_bars():
    builder = BarBuilder(bucket_minutes=5)
    builder.add_quote(100.0, dt.datetime(2024, 1, 2, 9, 31, 0))
    builder.add_quote(105.0, dt.datetime(2024, 1, 2, 10, 1, 0))

    bars = builder.bars_since(dt.datetime(2024, 1, 2, 10, 0, 0))

    assert len(bars) == 1
    assert bars[0].close == 105.0


def test_bars_since_empty_when_nothing_recorded():
    builder = BarBuilder()
    assert builder.bars_since(dt.datetime(2024, 1, 2, 0, 0)) == []
