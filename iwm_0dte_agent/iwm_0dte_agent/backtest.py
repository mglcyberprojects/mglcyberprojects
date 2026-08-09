"""Backtests the ORB+VWAP strategy against recent historical IWM price data.

Reuses the exact same strategy.py / risk.py / pricing.py code the live and
paper-trading agent uses, so a backtest exercises the same signal and risk
logic as a real run -- there's no separate "backtest strategy" to drift out
of sync with production.

IMPORTANT CAVEAT: 0DTE option prices here are the same synthetic
Black-Scholes model paper trading uses (flat 20% IV), not real historical
option quotes. Free data sources (including yfinance) don't provide
historical 0DTE option chains, so this can't reproduce real historical
bid/ask spreads or the IV skew/crush that shows up around news and near
expiry. Treat results as a check on strategy *behavior* (does it enter when
expected, does it respect stops/targets/the hard exit) -- not a prediction
of real fill prices or realistic P&L.

Usage:
    python -m iwm_0dte_agent.backtest --days 7
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from dataclasses import dataclass, field, replace

from .config import CONFIG, Config
from .gameplan_strategy import GameplanState, GameplanZones
from .market_data import download_bars
from .models import Bar, OptionType, TradeSignal
from .pricing import synthetic_chain
from .risk import RiskManager
from .strategy import generate_signal, select_strike

STARTING_BUYING_POWER = 25_000.0


@dataclass
class SimTrade:
    date: dt.date
    option_type: OptionType
    strike: float
    entry_time: dt.datetime
    entry_price: float
    exit_time: dt.datetime
    exit_price: float
    exit_reason: str
    quantity: int
    pnl: float


@dataclass
class DayResult:
    date: dt.date
    trades: list[SimTrade] = field(default_factory=list)
    skipped_reason: str | None = None


def fetch_historical_bars(symbol: str, days: int, interval: str = "5m") -> dict[dt.date, list[Bar]]:
    by_day: dict[dt.date, list[Bar]] = {}
    for bar in download_bars(symbol, period=f"{days}d", interval=interval):
        by_day.setdefault(bar.timestamp.date(), []).append(bar)
    return by_day


def _years_to_expiry(ts: dt.datetime, market_close: dt.time) -> float:
    close_dt = ts.replace(hour=market_close.hour, minute=market_close.minute, second=0, microsecond=0)
    seconds_left = max((close_dt - ts).total_seconds(), 60.0)
    return seconds_left / (365 * 24 * 3600)


def _quote_for(config: Config, day: dt.date, ts: dt.datetime, spot: float, strike: float, option_type: OptionType):
    tte = _years_to_expiry(ts, config.market_close)
    chain = synthetic_chain(config.symbol, spot, day.isoformat(), tte)
    return next((c for c in chain if c.strike == strike and c.option_type == option_type), None)


def _generate_signal(
    config: Config, bars: list[Bar], i: int,
    gameplan_state: GameplanState | None, gameplan_zones: GameplanZones | None,
) -> TradeSignal | None:
    if config.strategy == "gameplan":
        if gameplan_zones is None or gameplan_state is None:
            return None
        # Broken-zone (support/resistance) events aren't surfaced in backtest
        # output -- there's no notifier here, only actual trades get reported.
        evaluation = gameplan_state.evaluate(
            bars[i], gameplan_zones,
            require_bull_close=config.gameplan_require_bull_close,
            require_bear_close=config.gameplan_require_bear_close,
        )
        return evaluation.signal
    return generate_signal(
        bars[: i + 1], config.market_open, config.orb_minutes,
        use_vwap_filter=config.vwap_filter,
        use_volume_filter=config.volume_filter,
        volume_multiplier=config.volume_multiplier,
        volume_lookback_bars=config.volume_lookback_bars,
        breakout_buffer_pct=config.breakout_buffer_pct,
    )


def simulate_day(
    bars: list[Bar], config: Config = CONFIG, gameplan_zones: GameplanZones | None = None,
) -> DayResult:
    """Replays one trading day's bars through the live strategy/risk logic.

    Mirrors agent.py's _try_open_position/_try_close_position, but walking
    forward bar-by-bar through history instead of polling a real clock.
    `gameplan_zones` is only used when config.strategy == "gameplan"; a
    fresh GameplanState is created per day, matching its daily reset.

    Always sizes against STARTING_BUYING_POWER ($25,000) using normal ATM
    select_strike()/position_size() -- config.cheap_otm_mode is deliberately
    NOT honored here, so this measures strategy/signal quality in isolation
    from account-size effects. CHEAP_OTM_MODE's cheap-far-OTM premiums swing
    wildly in percentage terms on tiny absolute dollar amounts, which was
    swamping any read on whether the confluence filters actually help.
    """
    if not bars:
        return DayResult(date=dt.date.today(), skipped_reason="no data")

    day = bars[0].timestamp.date()
    risk = RiskManager(config=config)
    buying_power = STARTING_BUYING_POWER
    result = DayResult(date=day)
    position: dict | None = None
    gameplan_state = GameplanState() if config.strategy == "gameplan" else None

    for i, bar in enumerate(bars):
        now_time = bar.timestamp.time()

        if position is not None:
            exit_reason = None
            if now_time >= config.hard_exit:
                exit_reason = f"hard exit time reached ({config.hard_exit})"

            quote = _quote_for(config, day, bar.timestamp, bar.close, position["strike"], position["option_type"])
            if quote is None:
                continue
            if exit_reason is None:
                if quote.bid <= position["stop_loss"]:
                    exit_reason = f"stop loss hit (bid {quote.bid:.2f} <= {position['stop_loss']:.2f})"
                elif quote.bid >= position["profit_target"]:
                    exit_reason = f"profit target hit (bid {quote.bid:.2f} >= {position['profit_target']:.2f})"

            if exit_reason:
                pnl = (quote.bid - position["entry_price"]) * position["quantity"] * 100
                result.trades.append(SimTrade(
                    date=day, option_type=position["option_type"], strike=position["strike"],
                    entry_time=position["entry_time"], entry_price=position["entry_price"],
                    exit_time=bar.timestamp, exit_price=quote.bid,
                    exit_reason=exit_reason, quantity=position["quantity"], pnl=pnl,
                ))
                risk.record_trade_closed(pnl)
                buying_power += pnl
                position = None
            continue

        can_open, _why = risk.can_open_new_trade(buying_power, now_time)
        if not can_open:
            continue

        signal = _generate_signal(config, bars, i, gameplan_state, gameplan_zones)
        if signal is None:
            continue

        tte = _years_to_expiry(bar.timestamp, config.market_close)
        chain = synthetic_chain(config.symbol, signal.underlying_price, day.isoformat(), tte)
        contract = select_strike(chain, signal.option_type, signal.underlying_price, config.strike_offset)
        if contract is None or contract.ask <= 0:
            continue
        quantity = risk.position_size(buying_power, contract.ask)
        if quantity <= 0:
            continue

        position = {
            "option_type": signal.option_type, "strike": contract.strike,
            "entry_price": contract.ask, "entry_time": bar.timestamp, "quantity": quantity,
            "stop_loss": round(contract.ask * (1 - config.stop_loss_pct), 2),
            "profit_target": round(contract.ask * (1 + config.profit_target_pct), 2),
        }
        risk.record_trade_opened()

    if position is not None:
        # Historical data ran out before the hard exit time -- close at the
        # last available bar rather than leaving a phantom open position.
        last_bar = bars[-1]
        quote = _quote_for(config, day, last_bar.timestamp, last_bar.close, position["strike"], position["option_type"])
        exit_price = quote.bid if quote else position["entry_price"]
        pnl = (exit_price - position["entry_price"]) * position["quantity"] * 100
        result.trades.append(SimTrade(
            date=day, option_type=position["option_type"], strike=position["strike"],
            entry_time=position["entry_time"], entry_price=position["entry_price"],
            exit_time=last_bar.timestamp, exit_price=exit_price,
            exit_reason="ran out of historical data before hard exit",
            quantity=position["quantity"], pnl=pnl,
        ))

    return result


def _print_report(results: list[DayResult]) -> None:
    print(f"\n{'Date':<12}{'Trades':<8}{'P&L':>10}")
    print("-" * 30)
    total_pnl = 0.0
    total_trades = 0
    wins = 0
    for r in results:
        day_pnl = sum(t.pnl for t in r.trades)
        total_pnl += day_pnl
        total_trades += len(r.trades)
        wins += sum(1 for t in r.trades if t.pnl > 0)
        note = f"  ({r.skipped_reason})" if r.skipped_reason else ""
        print(f"{r.date.isoformat():<12}{len(r.trades):<8}{day_pnl:>10.2f}{note}")
        for t in r.trades:
            print(
                f"    {t.entry_time.strftime('%H:%M')} BUY {t.option_type.value.upper()} ${t.strike:g} "
                f"@ {t.entry_price:.2f}  ->  {t.exit_time.strftime('%H:%M')} @ {t.exit_price:.2f}  "
                f"pnl={t.pnl:+.2f}  ({t.exit_reason})"
            )
    print("-" * 30)
    win_rate = (wins / total_trades * 100) if total_trades else 0.0
    print(f"Total trades: {total_trades}   Win rate: {win_rate:.1f}%   Total P&L: {total_pnl:+.2f}\n")


def _write_csv(results: list[DayResult], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "option_type", "strike", "entry_time", "entry_price",
            "exit_time", "exit_price", "exit_reason", "quantity", "pnl",
        ])
        for r in results:
            for t in r.trades:
                writer.writerow([
                    t.date, t.option_type.value, t.strike, t.entry_time.isoformat(), t.entry_price,
                    t.exit_time.isoformat(), t.exit_price, t.exit_reason, t.quantity, f"{t.pnl:.2f}",
                ])


def run_backtest(
    days: int, interval: str = "5m", config: Config = CONFIG, out_csv: str = "backtest_results.csv",
    gameplan_zones: GameplanZones | None = None,
) -> list[DayResult]:
    by_day = fetch_historical_bars(config.symbol, days, interval)
    if not by_day:
        print(
            f"\nNo historical data came back for {config.symbol} over the last {days} day(s) "
            f"at interval={interval}. This means the download failed or returned nothing -- "
            f"not that no trades happened. Check your network connection and try again before "
            f"trusting a '0 trades' result.\n"
        )
        return []
    results = [simulate_day(bars, config, gameplan_zones) for _day, bars in sorted(by_day.items())]
    _print_report(results)
    _write_csv(results, out_csv)
    print(f"Full trade log written to {out_csv}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the IWM 0DTE ORB+VWAP or gameplan strategy against recent history")
    parser.add_argument("--days", type=int, default=7, help="Calendar days of history to pull (default 7, covers last trading week)")
    parser.add_argument("--interval", default="5m", choices=["1m", "2m", "5m", "15m", "30m"],
                         help="Bar size. 1m only available for roughly the last 7 trading days.")
    parser.add_argument("--out", default="backtest_results.csv", help="CSV output path")
    parser.add_argument("--strategy", default=None, choices=["orb", "gameplan"],
                         help="Override STRATEGY from .env for this run.")
    parser.add_argument("--hold-low", type=float, help="Gameplan strategy: hold zone low (required with --strategy gameplan)")
    parser.add_argument("--hold-high", type=float, help="Gameplan strategy: hold zone high")
    parser.add_argument("--reject-low", type=float, help="Gameplan strategy: rejection zone low")
    parser.add_argument("--reject-high", type=float, help="Gameplan strategy: rejection zone high")
    args = parser.parse_args()

    config = CONFIG
    if args.strategy is not None:
        config = replace(config, strategy=args.strategy)

    gameplan_zones = None
    if config.strategy == "gameplan":
        zone_args = (args.hold_low, args.hold_high, args.reject_low, args.reject_high)
        if any(z is None for z in zone_args):
            parser.error(
                "--strategy gameplan requires all four zone bounds: "
                "--hold-low --hold-high --reject-low --reject-high\n"
                "Note: this applies ONE fixed zone set across every day in the backtest window -- "
                "your real day-to-day discretionary zones would differ. This tests the "
                "touch/confirm/exit logic against a plausible zone, not your actual daily calls."
            )
        gameplan_zones = GameplanZones(
            hold_low=args.hold_low, hold_high=args.hold_high,
            reject_low=args.reject_low, reject_high=args.reject_high,
        )

    run_backtest(days=args.days, interval=args.interval, config=config, out_csv=args.out, gameplan_zones=gameplan_zones)


if __name__ == "__main__":
    main()
