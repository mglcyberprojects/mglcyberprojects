# IWM 0DTE Options Signal Agent

A signal-generation agent for **$IWM (Russell 2000 ETF) same-day-expiration
("0DTE") options**, connected to Robinhood. It watches price action, applies
an opening-range-breakout strategy, and proposes trades — but it **never
places an order without an explicit human confirmation**, either typed at
the terminal or approved/declined from Telegram. There is no autopilot mode.

**Going live for the first time?** See `live_trading_walkthrough.txt` for
the exact setup checklist and process flow -- what to verify before your
first `--live` session, and what to expect during one.

## Read this before running it

- **0DTE options are extremely high risk.** They can lose their entire value
  within hours, and often expire worthless. This project is not investment
  advice, and past behavior of any strategy here is no guarantee of future
  results.
- **`--live` mode places real options orders through Robinhood's official
  Agentic Trading MCP server** (`agent.robinhood.com/mcp/trading`,
  OAuth-based), not the unofficial `robin_stocks` client — as of an August
  2026 connection, that server exposes a full options surface
  (`get_option_chains`, `get_option_quotes`, `review_option_order`,
  `place_option_order`, ...) and this agent's account data, underlying price
  bars, 0DTE chain lookup, quotes, and order submission all go through it.
  With the default `USE_ROBINHOOD_MCP=true`, [`robin_stocks`](https://github.com/jmfernandes/robin_stocks)
  (unofficial, against Robinhood's Terms of Service) is never imported,
  logged into, or called at all — that risk only applies if you explicitly
  set `USE_ROBINHOOD_MCP=false`. This is new and has only been validated
  against one real account's schema so far; see "Robinhood's official MCP
  server" below before trusting it with real money.
- **Default mode is paper trading.** Running the agent with no flags never
  touches Robinhood or real money — it simulates fills against real IWM
  price data pulled via `yfinance`, with a synthetic (Black-Scholes) option
  chain. Use this to evaluate the strategy before ever considering `--live`.
- Every proposed trade — entry or exit — shows its contract, cost, stop
  loss, and profit target, and requires an explicit approval before
  `broker.submit_order` is ever called. Entries let you pick the quantity
  yourself (1/3/5 contracts, whichever you can afford); exits always close
  the full existing position, so there's nothing to pick. With Telegram
  configured, that approval is inline buttons in your authorized chat;
  otherwise it's typed at the terminal (a quantity for entries, `y`/`N` for
  exits). `--live` mode additionally requires typing `LIVE` at startup
  before the session begins.
- **Telegram approval means whoever controls that Telegram account can
  place real orders in --live mode.** Treat the bot token and your chat id
  as credentials: keep `.env` out of git (already covered by `.gitignore`),
  don't add the bot to group chats, and don't share the token. An unanswered
  confirmation request is treated as a decline after
  `TELEGRAM_CONFIRM_TIMEOUT_SECONDS` (default 5 minutes) — it never falls
  through to "approved" on timeout.

## Strategy

`STRATEGY` in `.env` picks which one generates entry signals -- `orb`
(default) or `gameplan`. Both feed into the same risk management, position
sizing, confirmation gate, and hard-exit handling described below; only the
"when do I enter" logic differs.

### Opening Range Breakout (`STRATEGY=orb`, default)

With an optional VWAP trend filter, plus two more optional confirming
filters (off by default — see below):

1. The first `ORB_MINUTES` (default 15) of the session establishes a
   high/low range.
2. A close above the range high — and above session VWAP, if
   `VWAP_FILTER` (default on) is on — signals a **call**. A close below
   the range low (and below VWAP) signals a **put**.
3. Only one position is held at a time, capped at `MAX_TRADES_PER_DAY`
   entries per day.
4. Every position carries a stop loss and profit target expressed as a
   percentage of the premium paid, and is force-exited at `HARD_EXIT` time
   regardless of P&L, to avoid holding 0DTE contracts into the final
   minutes before expiration.

**Two additional confirming filters exist, both off by default**
(`VOLUME_FILTER=false`, `BREAKOUT_BUFFER_PCT=0`) — a real backtest
comparison didn't show a clear benefit over the base ORB+VWAP strategy, so
the agent behaves the same as it did before these existed unless you
explicitly turn one on to experiment:
- **Volume** (`VOLUME_FILTER`): the breakout bar's volume must beat
  `VOLUME_MULTIPLIER` (default 1.5) times the average volume of the last
  `VOLUME_LOOKBACK_BARS` (default 6) bars, counting only bars *after* the
  opening range closed (not the range-forming bars themselves, which are
  structurally the highest-volume bars of the day and would otherwise
  inflate the baseline right when it matters most).
- **Breakout buffer** (`BREAKOUT_BUFFER_PCT`, e.g. 0.001 = 0.1%): the
  close must clear the level by this fraction, not just tick through it
  by any amount.

Both apply in paper, `--live`, and `backtest.py` identically since all
three reuse this exact function — `python -m iwm_0dte_agent.backtest` is
the way to compare signal frequency and win rate with a filter on vs. off
before trusting a change with real money.

See `iwm_0dte_agent/strategy.py` for the exact logic — it's pure functions
with no I/O, so it's easy to read and to unit test.

### Gameplan: Hold / Rejection Zones (`STRATEGY=gameplan`)

A port of the "Gameplan: Hold / Rejection Zones" TradingView indicator
(`iwm_0dte_agent/gameplan_strategy.py`), for a discretionary "I drew these
levels on the chart this morning" style of trading rather than a computed
opening range:

1. You define a **hold zone** (support) and a **rejection zone**
   (resistance) as price ranges, e.g. hold `228.50–229.20`, reject
   `231.00–232.50`.
2. A **CALL** signal fires the first time price dips into the hold zone and
   then closes back above it (a bullish candle, close > open, is required
   by default — `GAMEPLAN_REQUIRE_BULL_CLOSE`). A **PUT** signal fires
   symmetrically off the rejection zone. Each fires **at most once per
   day**.
3. If price closes all the way through a zone (below the hold zone's low,
   or above the rejection zone's high), that's flagged as "support broken"
   / "resistance broken" and sent as an alert — but, matching the original
   indicator, it does **not** open a position on its own; it's an
   invalidation marker, not an entry trigger.
4. Only the indicator's "Manual" zone-source mode is ported — you supply
   the zones yourself each morning (see below). The other four automatic
   modes in the original script (Premarket Range, Prior Day Range, Classic
   Pivots, VWAP+ATR) were left out; ask if you want one added.

**Getting the zones to the agent:** at startup, if `STRATEGY=gameplan`, the
agent asks for today's zones — over Telegram if configured (an inline
prompt asking you to reply with four numbers), otherwise a terminal prompt.
Reply with:
```
228.50 229.20 231.00 232.50
```
(hold\_low hold\_high reject\_low reject\_high, space- or comma-separated).
No reply within `GAMEPLAN_ZONE_REQUEST_TIMEOUT_SECONDS` (default 30
minutes) and the agent keeps running but won't open any positions that day
— it doesn't fall back to guessing zones on its own.

## Risk management

Configured in `.env` (see `.env.example`):

| Setting | Meaning |
|---|---|
| `RISK_PCT_PER_TRADE` | Fraction of buying power risked per trade, sized against the stop loss — drives `backtest.py`'s unattended sizing only, see below |
| `STOP_LOSS_PCT` / `PROFIT_TARGET_PCT` | Exit thresholds as % of entry premium |
| `MAX_TRADES_PER_DAY` | Hard cap on new entries per session |
| `MAX_DAILY_LOSS_PCT` | Stops new entries once realized losses for the day hit this % of buying power |
| `MAX_CONTRACTS_PER_TRADE` | Absolute ceiling on position size regardless of sizing math |
| `ENTRY_CUTOFF` / `HARD_EXIT` | No new entries after cutoff; all positions force-closed at hard exit |

### Picking a quantity live/paper vs. in a backtest

Live and paper sessions don't size positions automatically — when a signal
fires, the agent computes which of **1, 3, or 5 contracts** you can actually
afford (`quantity * ask * 100 <= buying power`, also capped at
`MAX_CONTRACTS_PER_TRADE`) and offers you those as a choice at approval time:
buttons in Telegram, or a typed number at the terminal. If none of 1/3/5 fit
your buying power, the signal is skipped with an alert instead of prompting.
`RISK_PCT_PER_TRADE` plays no part in this — it's not a live/paper knob.

`backtest.py`, which has no human in the loop, still needs a fully automated
sizing formula — that's what `RISK_PCT_PER_TRADE` (and `CHEAP_OTM_MODE`
below) drive: they pick a single quantity per simulated trade so a multi-day
backtest can run unattended.

### Small-account smoke testing (e.g. $50) — backtest only

A single ATM 0DTE IWM contract can easily cost more than a small test
account's entire buying power, and the `RISK_PCT_PER_TRADE` sizing formula
above will just size every trade down to 0 contracts once buying power gets
that small. `CHEAP_OTM_MODE=true` swaps both of those out for account sizes
where there's no meaningful "risk %" to size against, in `backtest.py`:

| Setting | Meaning |
|---|---|
| `CHEAP_OTM_MODE` | When true, replaces normal strike/sizing logic below, in `backtest.py` |
| `OTM_MIN_DISCOUNT_PCT` | Walks strikes out-of-the-money until premium is at least this much cheaper than the ATM contract's ask (default `0.70` = 70% cheaper) |

With it on: the agent picks the first strike, walking away from ATM, whose
ask is at least `OTM_MIN_DISCOUNT_PCT` below the ATM ask (rather than a
fixed number of strikes out, since how far that takes you moves with the
day's volatility) — then buys exactly 1 contract if `ask * 100` fits your
buying power, otherwise skips the signal. Stop loss / profit target %,
approval-gating, and everything else about the agent is unchanged; this
only changes *which* strike gets picked and *how many* contracts.

`CHEAP_OTM_MODE` still affects strike selection in live/paper mode too — it's
only the sizing half (1 contract only) that's backtest-only, since live/paper
always offers the 1/3/5 choice described above regardless of this flag. Turn
it off (`CHEAP_OTM_MODE=false`, the default) once you're backtesting against
a real account and want `RISK_PCT_PER_TRADE`-based position sizing back for
those simulated runs.

## Setup

```bash
cd iwm_0dte_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit strategy/risk parameters as desired
```

`ROBINHOOD_USERNAME`/`PASSWORD`/`TOTP_SECRET` are **not** needed for the
default `--live` path — see below. Only fill them in if you plan to set
`USE_ROBINHOOD_MCP=false`.

## Robinhood's official MCP server

`--live` mode defaults to `USE_ROBINHOOD_MCP=true`, which connects to
Robinhood's official Agentic Trading MCP server via OAuth — you authorize in
your own browser, the agent never sees your Robinhood password at all. As of
an August 2026 connection, this covers account balance, underlying quotes,
**and the full options path**: `get_option_chains` → `get_option_instruments`
→ `get_option_quotes` for reading the 0DTE chain, and `review_option_order`
→ `place_option_order` for submitting a trade (declining automatically if
Robinhood's own pre-trade `order_checks` flags anything). There's no MCP
tool for historical price bars, only a live quote — so rather than falling
back to `robin_stocks` for those, `get_intraday_bars` builds its own 5-minute
OHLC bars in process from the same quote polls the agent already makes every
cycle (see `bar_builder.py`). With `USE_ROBINHOOD_MCP=true`, the default,
`robin_stocks` is never imported, never logged into, and never called —
`ROBINHOOD_USERNAME`/`PASSWORD`/`TOTP_SECRET` aren't needed. The one
consequence: the opening range is only as good as how long the agent has
been running, so start it at or before market open.

Set `USE_ROBINHOOD_MCP=false` to skip MCP entirely and use `robin_stocks`
for everything instead, the way this project worked before the MCP server
existed — a reasonable fallback if the MCP path ever misbehaves on your
account, but note this brings back `robin_stocks`' automation-against-ToS
risk (see `broker.py`), which the default path above avoids entirely.

**This is new and has only been schema-validated against one real
account**, via `mcp_probe.py describe` (reads each tool's declared JSON
schema without ever invoking it) rather than trial-and-error against real
orders. Everything downstream of `submit_order` still sits behind this
project's existing human-confirmation gate — nothing places automatically —
but treat the first several `--live` runs as something to watch closely,
not something to walk away from.

**Check what's live on your account** without running the trading loop or
touching robin_stocks at all:

```bash
python -m iwm_0dte_agent --list-mcp-tools
```

This connects via OAuth and prints every tool the server currently
exposes. `login()` also logs a warning if it spots any option-related tool
name this client doesn't already use (e.g. multi-leg-specific tools,
`cancel_option_order`, `exercise_option`) — that's your sign Robinhood
added something new worth wiring in too.

To inspect a specific tool's real request/response shape before changing
how it's used (rather than guessing), use `mcp_probe.py`:

```bash
# Safe on anything, including order-placement tools -- reads the schema
# the server declares, never actually calls the tool. Takes any number of
# tool names and an optional --out FILE to dump several schemas at once:
python -m iwm_0dte_agent.mcp_probe describe get_option_chains get_option_instruments --out option_schemas.txt

# Actually invokes the tool and prints the raw response. Fine for
# read-only tools; requires a second confirmation for anything that can
# place/cancel/modify an order or exercise a position:
python -m iwm_0dte_agent.mcp_probe call get_option_chains '{"underlying_symbol": "IWM"}'
```

If `--list-mcp-tools`, `mcp_probe.py`, or `--live` fails during the MCP step:

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
4. Run the agent. You should get an "Agent started" message. Proposed entries
   arrive with buttons for each affordable quantity (**1x / 3x / 5x contracts**,
   whichever fit your buying power) plus **Decline**; proposed closes arrive
   with **Approve** / **Decline** since a close always closes the full
   existing position — there's nothing to choose there.

What gets sent: agent start/stop, every proposed trade (entries with
quantity-choice buttons, closes with Approve/Decline), fills, declines, order
failures, risk limits blocking new entries (deduplicated to once per reason
per day), hard-exit-forced closes, and unhandled errors in the agent loop.

Only replies from the configured `TELEGRAM_CHAT_ID` are ever accepted as an
approval — button presses from any other chat are logged and ignored (same
rule for the `/status` command and Refresh button below).

**On-demand status:** send `/status` or `/positions` to the chat any time to
get a snapshot — every open position (contract, entry price, live bid,
unrealized P&L, stop loss/profit target) or "No open positions", plus
buying power, trades today, and today's realized P&L. Rendered as a list:
today the agent only ever holds one position at a time (see above), so it's
usually one entry or none, but the reply is ready to show more if that
changes. The reply has a **🔄 Refresh** button that re-fetches everything
and edits the same message in place, rather than sending a new one each
time.

At startup the agent also sends a persistent **📊 Positions** button that
replaces your chat's normal keyboard — it stays there under the message box
across every message from then on, so checking positions doesn't require
remembering or retyping `/status`. Tapping it just sends "📊 Positions" as
an ordinary message, recognized the same way as typing the command.

This is checked once per agent loop iteration, not instantly — a `/status`
command or Refresh tap is picked up on the next cycle, so expect up to
`POLL_SECONDS` (60s by default) of latency, not a live push.

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

## Backtesting

```bash
python -m iwm_0dte_agent.backtest --days 7
```

Replays the same strategy/risk logic against recent real IWM price history
and prints a per-day trade report plus a `backtest_results.csv`. Option
prices are still the same synthetic Black-Scholes model paper trading uses
(no free source of historical 0DTE chains exists), so treat this as a check
on strategy *behavior*, not a prediction of real P&L. See
`backtest_instructions.txt` for the full walkthrough.

To backtest the gameplan strategy, pass a single fixed zone set applied
across every day in the window (backtesting can't ask you for a fresh
discretionary read each morning the way live/paper trading does):
```bash
python -m iwm_0dte_agent.backtest --strategy gameplan --days 7 \
    --hold-low 228.50 --hold-high 229.20 --reject-low 231.00 --reject-high 232.50
```

The simulated account starts with $25,000 buying power by default,
regardless of `CHEAP_OTM_MODE` in `.env` — that setting's strike selection
and 1-contract-if-affordable sizing *are* honored in the backtest, but at
$25,000 a contract is basically always affordable, so the "does this fit my
account" part of `CHEAP_OTM_MODE` never actually gets exercised. Pass
`--buying-power` to test against a size that matches your real account:
```bash
python -m iwm_0dte_agent.backtest --days 7 --buying-power 50
```
Worth noting when comparing runs: at small buying-power sizes, cheap
far-OTM premiums swing wildly in percentage terms on tiny absolute dollar
amounts, which can swamp the read on whether some other change (a filter, a
parameter tweak) actually helped. If you're isolating a strategy/signal
change specifically, compare at the $25,000 default first; use
`--buying-power` to separately judge how `CHEAP_OTM_MODE` sizing behaves at
your real account size.

## Running unattended on Windows

`deploy/run_agent.bat` + `deploy/setup_autostart.ps1` register the agent as
a Windows Scheduled Task that starts automatically when you log in and
restarts itself if it crashes — the closest Windows equivalent to a Linux
`systemd` service, no extra tools to install.

**Telegram must be configured first (see above).** With no console window
in front of you, a terminal `y/N` confirmation prompt would just hang
forever waiting for input that can never arrive. `run_agent.bat` runs in
paper mode only (no `--live`) for the same reason: `--live` mode has its
own one-time `Type LIVE to confirm` startup prompt that also needs a real
interactive terminal, so don't point this at `--live` yet — ask if you want
help routing that confirmation through Telegram too before you do.

Set it up:

```powershell
cd iwm_0dte_agent\deploy
.\setup_autostart.ps1
```

That registers a task named `IWM0DTEAgent` triggered at your next logon. To
start it immediately without logging out:

```powershell
schtasks /Run /TN IWM0DTEAgent
```

You'll get a console window titled "IWM 0DTE Agent" (safe to minimize, not
to close) plus a running log at `iwm_0dte_agent\logs\agent.log`. Day-to-day
visibility is meant to come from Telegram, not that window — the log is a
backstop for debugging startup failures.

Stop / remove it:

```powershell
taskkill /FI "WINDOWTITLE eq IWM 0DTE Agent" /T /F   # stop the running agent
schtasks /Delete /TN IWM0DTEAgent /F                 # remove the scheduled task entirely
```

Note this only runs while you're logged in to Windows (not across a full
logoff/lock-screen-only is fine, a sign-out is not) — good enough for a
machine you leave logged in during market hours. If you want it to survive
being fully logged off too, that needs the Scheduled Task's "run whether
user is logged on or not" option, which requires storing your Windows
password in Task Scheduler — ask if you want that set up.

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
  broker.py             # Broker interface + robin_stocks implementation (only used if USE_ROBINHOOD_MCP=false)
  mcp_broker.py         # Robinhood's official MCP server: account data, price bars, full 0DTE options path -- default --live broker
  bar_builder.py        # In-process OHLC bar accumulation from MCP quote polls (no MCP historical-bars tool exists)
  mcp_probe.py          # CLI to inspect a Robinhood MCP tool's schema/response before wiring it into MCPBroker
  market_data.py        # yfinance download + DataFrame-to-Bar conversion, shared by paper broker + backtester
  paper_broker.py       # simulated broker for --dry-run (default)
  pricing.py            # Black-Scholes pricer + synthetic chain/quote builders (paper broker + backtester)
  strategy.py           # ORB + VWAP signal generation (pure functions)
  gameplan_strategy.py  # hold/rejection zone signal generation, ported from a TradingView indicator
  risk.py               # position sizing and daily risk limits
  notifier.py           # Notifier protocol + factory (Telegram if configured, else terminal)
  telegram_bot.py       # Telegram alerts + inline-button approve/decline
  terminal_notifier.py  # terminal fallback: print alerts, y/N confirmation prompt
  trade_log.py          # CSV audit log of every proposed/filled/declined trade
  backtest.py           # replays strategy/risk logic against historical IWM bars
  agent.py              # main loop + CLI entrypoint
tests/                  # unit tests for strategy/risk/pricing/notifier/mcp_broker/backtest
deploy/
  run_agent.bat         # Windows restart-loop wrapper (paper mode)
  setup_autostart.ps1   # registers run_agent.bat as a logon-triggered Scheduled Task
```
