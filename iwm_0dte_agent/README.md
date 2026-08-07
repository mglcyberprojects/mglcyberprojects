# IWM 0DTE Options Signal Agent

A signal-generation agent for **$IWM (Russell 2000 ETF) same-day-expiration
("0DTE") options**, connected to Robinhood. It watches price action, applies
an opening-range-breakout strategy, and proposes trades — but it **never
places an order without an explicit human confirmation**, either typed at
the terminal or approved/declined from Telegram. There is no autopilot mode.

## Read this before running it

- **0DTE options are extremely high risk.** They can lose their entire value
  within hours, and often expire worthless. This project is not investment
  advice, and past behavior of any strategy here is no guarantee of future
  results.
- **Robinhood options trading still goes through an unofficial client.**
  Robinhood launched an official, OAuth-based "Agentic Trading" MCP server
  in May 2026 (`agent.robinhood.com/mcp/trading`), and `--live` mode uses it
  by default for account balance and IWM's underlying price. But as of this
  writing, that server does not yet expose options order placement — so
  0DTE option chain lookups and every actual order this agent places still
  go through [`robin_stocks`](https://github.com/jmfernandes/robin_stocks),
  an unofficial, reverse-engineered client. Using it to automate trading is
  against Robinhood's Terms of Service and can get an account flagged,
  restricted, or closed. That risk is yours if you enable `--live`. See
  "Robinhood's official MCP server" below for details and how to check
  whether that's changed.
- **Default mode is paper trading.** Running the agent with no flags never
  touches Robinhood or real money — it simulates fills against real IWM
  price data pulled via `yfinance`, with a synthetic (Black-Scholes) option
  chain. Use this to evaluate the strategy before ever considering `--live`.
- Every proposed trade — entry or exit — shows its contract, size, cost,
  stop loss, and profit target, and requires an explicit approval before
  `broker.submit_order` is ever called. With Telegram configured, that
  approval is an inline button reply from your authorized chat; otherwise
  it's a typed `y` at the terminal. `--live` mode additionally requires
  typing `LIVE` at startup before the session begins.
- **Telegram approval means whoever controls that Telegram account can
  place real orders in --live mode.** Treat the bot token and your chat id
  as credentials: keep `.env` out of git (already covered by `.gitignore`),
  don't add the bot to group chats, and don't share the token. An unanswered
  confirmation request is treated as a decline after
  `TELEGRAM_CONFIRM_TIMEOUT_SECONDS` (default 5 minutes) — it never falls
  through to "approved" on timeout.

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

## Robinhood's official MCP server

`--live` mode defaults to `USE_ROBINHOOD_MCP=true`, which connects to
Robinhood's official Agentic Trading MCP server for account balance and
IWM's underlying price via OAuth — you authorize in your own browser, the
agent never sees your Robinhood password for that part. **0DTE options
(chain lookup and every order this agent actually places) still go through
`robin_stocks` regardless of this setting**, because Robinhood's MCP server
does not yet expose options tools. `ROBINHOOD_USERNAME` / `PASSWORD` /
`TOTP_SECRET` are therefore still required in `.env` even with MCP enabled.
Set `USE_ROBINHOOD_MCP=false` to skip MCP entirely and use `robin_stocks`
for everything, the way this project worked before the MCP server existed.

**Check what's live on your account** without running the trading loop or
touching robin_stocks at all:

```bash
python -m iwm_0dte_agent --list-mcp-tools
```

This connects via OAuth (opens your browser to Robinhood's login) and
prints every tool the server currently exposes. If you see anything with
"option" in the name, the agent will also log a warning about it on
`--live` startup — that's your sign options support may have landed, and
worth asking to have wired into `mcp_broker.py`'s `MCPBroker` so order
placement moves off `robin_stocks` too.

**This integration has not been exercised against the live server.** It's
written against the real `mcp` Python SDK's client API (verified by
inspecting the installed package) and Robinhood's publicly documented OAuth
+ MCP setup, but the sandbox this was built in has no network access to
`robinhood.com`, so the OAuth handshake and exact tool response shapes are
unconfirmed. If `--list-mcp-tools` or `--live` fails during the MCP step:

- The OAuth flow opens `http://127.0.0.1:8765/callback` (configurable via
  `ROBINHOOD_MCP_OAUTH_PORT`) to catch the redirect — make sure nothing else
  is bound to that port, and that you're running this on the same machine
  as the browser (the MCP docs say agentic accounts are set up on desktop).
- A tool-name or response-shape error will name the exact tool and payload
  it choked on (`MCPError`) — that's the fastest way to tell me what
  Robinhood's server actually returned so the parsing can be corrected.
- As a fallback, set `USE_ROBINHOOD_MCP=false` and the agent behaves exactly
  as it did before this integration existed.

Tokens are cached in `.robinhood_mcp_tokens.json` (gitignored) after the
first successful authorization, so you shouldn't need to reauthorize every
run.

## Telegram alerts + approval (optional)

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are both set in `.env`, the
agent sends every alert and confirmation request to that chat instead of the
terminal. If either is blank, it falls back to terminal-only with no remote
alerts — the agent always has a working confirmation channel either way.

1. **Create a bot:** message [@BotFather](https://t.me/BotFather) on
   Telegram, send `/newbot`, and follow the prompts. It replies with a token
   that looks like `123456789:AAExampleTokenDoNotUse`. Put that in
   `TELEGRAM_BOT_TOKEN`.
2. **Start a chat with your new bot** (search for it by the username you
   gave BotFather) and send it any message, e.g. `/start` — Telegram bots
   can't message you first.
3. **Find your chat id:** open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser right
   after step 2 and look for `"chat":{"id":...}` in the response. Put that
   number in `TELEGRAM_CHAT_ID`.
4. Run the agent. You should get an "Agent started" message. Proposed trades
   arrive with **Approve** / **Decline** buttons.

What gets sent: agent start/stop, every proposed trade (with Approve/Decline
buttons), fills, declines, order failures, risk limits blocking new entries
(deduplicated to once per reason per day), hard-exit-forced closes, and
unhandled errors in the agent loop.

Only replies from the configured `TELEGRAM_CHAT_ID` are ever accepted as an
approval — button presses from any other chat are logged and ignored.

## Running

```bash
# Paper trading (default, safe, no Robinhood connection):
python -m iwm_0dte_agent

# Single iteration, useful for testing outside market hours:
python -m iwm_0dte_agent --once --verbose

# Live trading on Robinhood (real money, real orders, reads .env credentials):
python -m iwm_0dte_agent --live

# Check what Robinhood's official MCP server currently exposes, no trading:
python -m iwm_0dte_agent --list-mcp-tools
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
  config.py             # env-driven configuration
  models.py             # shared dataclasses (Bar, OptionContract, TradeSignal, ...)
  broker.py             # Broker interface + live Robinhood implementation (robin_stocks)
  mcp_broker.py         # Robinhood's official MCP server for account/equity data, robin_stocks fallback for options
  paper_broker.py       # simulated broker for --dry-run (default)
  pricing.py            # Black-Scholes pricer used only by the paper broker
  strategy.py           # ORB + VWAP signal generation (pure functions)
  risk.py               # position sizing and daily risk limits
  notifier.py           # Notifier protocol + factory (Telegram if configured, else terminal)
  telegram_bot.py       # Telegram alerts + inline-button approve/decline
  terminal_notifier.py  # terminal fallback: print alerts, y/N confirmation prompt
  trade_log.py          # CSV audit log of every proposed/filled/declined trade
  agent.py              # main loop + CLI entrypoint
tests/                  # unit tests for strategy/risk/pricing/notifier/mcp_broker
```
