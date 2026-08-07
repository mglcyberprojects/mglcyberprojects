# IWM 0DTE Options Signal Agent

A signal-generation agent for **$IWM (Russell 2000 ETF) same-day-expiration
("0DTE") options**, connected to Robinhood. It watches price action, applies
an opening-range-breakout strategy, and proposes trades — but it **never
places an order without an explicit human confirmation typed at the
terminal**. There is no autopilot mode.

## Read this before running it

- **0DTE options are extremely high risk.** They can lose their entire value
  within hours, and often expire worthless. This project is not investment
  advice, and past behavior of any strategy here is no guarantee of future
  results.
- **Robinhood has no official public trading API.** `--live` mode talks to
  Robinhood through [`robin_stocks`](https://github.com/jmfernandes/robin_stocks),
  an unofficial, reverse-engineered client. Using it to automate trading is
  against Robinhood's Terms of Service and can get an account flagged,
  restricted, or closed. That risk is yours if you enable `--live`.
- **Default mode is paper trading.** Running the agent with no flags never
  touches Robinhood or real money — it simulates fills against real IWM
  price data pulled via `yfinance`, with a synthetic (Black-Scholes) option
  chain. Use this to evaluate the strategy before ever considering `--live`.
- Every proposed trade — entry or exit — is printed to the terminal with its
  contract, size, cost, stop loss, and profit target, and requires a typed
  `y` to proceed. `--live` mode additionally requires typing `LIVE` at
  startup before the session begins.

## Strategy

Opening Range Breakout (ORB) with an optional VWAP trend filter:

1. The first `ORB_MINUTES` (default 15) of the session establishes a
   high/low range.
2. A close above the range high — and above session VWAP, if the filter is
   on — signals a **call**. A close below the range low (and below VWAP)
   signals a **put**.
3. Only one position is held at a time, capped at `MAX_TRADES_PER_DAY`
   entries per day.
4. Every position carries a stop loss and profit target expressed as a
   percentage of the premium paid, and is force-exited at `HARD_EXIT` time
   regardless of P&L, to avoid holding 0DTE contracts into the final
   minutes before expiration.

See `iwm_0dte_agent/strategy.py` for the exact logic — it's pure functions
with no I/O, so it's easy to read and to unit test.

## Risk management

Configured in `.env` (see `.env.example`):

| Setting | Meaning |
|---|---|
| `RISK_PCT_PER_TRADE` | Fraction of buying power risked per trade, sized against the stop loss |
| `STOP_LOSS_PCT` / `PROFIT_TARGET_PCT` | Exit thresholds as % of entry premium |
| `MAX_TRADES_PER_DAY` | Hard cap on new entries per session |
| `MAX_DAILY_LOSS_PCT` | Stops new entries once realized losses for the day hit this % of buying power |
| `MAX_CONTRACTS_PER_TRADE` | Absolute ceiling on position size regardless of sizing math |
| `ENTRY_CUTOFF` / `HARD_EXIT` | No new entries after cutoff; all positions force-closed at hard exit |

## Setup

```bash
cd iwm_0dte_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit strategy/risk parameters as desired
```

For `--live` mode only, also fill in `ROBINHOOD_USERNAME`,
`ROBINHOOD_PASSWORD`, and `ROBINHOOD_TOTP_SECRET` (the base32 secret from
setting up an authenticator app for 2FA — not SMS codes) in `.env`.

## Running

```bash
# Paper trading (default, safe, no Robinhood connection):
python -m iwm_0dte_agent

# Single iteration, useful for testing outside market hours:
python -m iwm_0dte_agent --once --verbose

# Live trading on Robinhood (real money, real orders, reads .env credentials):
python -m iwm_0dte_agent --live
```

All proposed and executed trades are appended to `trade_log.csv`
(configurable via `TRADE_LOG_PATH`).

## Tests

```bash
pip install -r requirements.txt
pytest
```

Tests cover the strategy signal logic, position sizing/risk limits, and the
synthetic option pricer — all pure functions, no network or broker calls.

## Project layout

```
iwm_0dte_agent/
  config.py         # env-driven configuration
  models.py          # shared dataclasses (Bar, OptionContract, TradeSignal, ...)
  broker.py           # Broker interface + live Robinhood implementation (robin_stocks)
  paper_broker.py     # simulated broker for --dry-run (default)
  pricing.py           # Black-Scholes pricer used only by the paper broker
  strategy.py          # ORB + VWAP signal generation (pure functions)
  risk.py               # position sizing and daily risk limits
  confirm.py             # human confirmation gate — the safety boundary
  trade_log.py           # CSV audit log of every proposed/filled/declined trade
  agent.py                # main loop + CLI entrypoint
tests/                    # unit tests for strategy/risk/pricing
```
