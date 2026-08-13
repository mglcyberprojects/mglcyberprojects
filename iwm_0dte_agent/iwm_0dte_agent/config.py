"""Central configuration for the IWM 0DTE agent.

All knobs are read from environment variables (with sane defaults) so the
strategy/risk parameters can be tuned without touching code. Copy
`.env.example` to `.env` and adjust as needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_time(name: str, default: str) -> time:
    raw = os.environ.get(name, default)
    hour, minute = (int(part) for part in raw.split(":"))
    return time(hour=hour, minute=minute)


def _env_symbols(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class Config:
    # Every ticker the agent watches and can independently hold a position
    # in -- one position per symbol, up to len(symbols) concurrently (see
    # agent.py's run() loop). A tuple, not a list, so Config stays hashable
    # /truly immutable like its other fields (frozen=True doesn't stop a
    # mutable list field from being mutated in place).
    symbols: tuple[str, ...] = field(
        default_factory=lambda: _env_symbols("SYMBOLS", "SPY,IWM,NVDA,MSTR,HOOD,COIN,PLTR,UBER")
    )

    # Robinhood credentials for the unofficial robin_stocks client. NOT used
    # by the default --live path (USE_ROBINHOOD_MCP=true talks only to
    # Robinhood's official OAuth-based MCP server) -- only needed if you set
    # USE_ROBINHOOD_MCP=false, which uses robin_stocks for everything.
    rh_username: str = field(default_factory=lambda: os.environ.get("ROBINHOOD_USERNAME", ""))
    rh_password: str = field(default_factory=lambda: os.environ.get("ROBINHOOD_PASSWORD", ""))
    rh_totp_secret: str = field(default_factory=lambda: os.environ.get("ROBINHOOD_TOTP_SECRET", ""))

    # Which strategy generates entry signals: "orb" (opening-range breakout,
    # default) or "gameplan" (manual hold/rejection zones, see
    # gameplan_strategy.py).
    strategy: str = field(default_factory=lambda: os.environ.get("STRATEGY", "orb").lower())

    # Opening-range breakout strategy. Default 5-minute range (with 1-minute
    # bars for both range-building and breakout entries -- see
    # market_data.py/paper_broker.py's fetch interval) to match the
    # ORB+EMA-cloud "A+ setup" strategy this was tuned around.
    orb_minutes: int = field(default_factory=lambda: _env_int("ORB_MINUTES", 5))
    vwap_filter: bool = field(default_factory=lambda: os.environ.get("VWAP_FILTER", "true").lower() == "true")

    # How far from the current price (in dollars, not strikes) to select an
    # entry contract: a CALL targets underlying_price + strike_dollar_offset,
    # a PUT targets underlying_price - strike_dollar_offset, then whichever
    # strike the chain actually has closest to that target is used. See
    # select_strike_by_dollar_offset() in strategy.py.
    strike_dollar_offset: float = field(default_factory=lambda: _env_float("STRIKE_DOLLAR_OFFSET", 1.0))

    # Additional ORB confirming filters -- each cuts signal frequency for
    # (hopefully) higher quality; see strategy.generate_signal()'s docstring.
    # Volume/breakout-buffer default OFF: a real backtest comparison didn't
    # show a clear benefit, so the agent behaves the same as it did before
    # these existed unless you explicitly turn one on to experiment.
    # EMA cloud confluence defaults ON -- it's the primary "A+ setup"
    # criterion this strategy is built around, not an experimental extra.
    volume_filter: bool = field(default_factory=lambda: os.environ.get("VOLUME_FILTER", "false").lower() == "true")
    volume_multiplier: float = field(default_factory=lambda: _env_float("VOLUME_MULTIPLIER", 1.5))
    volume_lookback_bars: int = field(default_factory=lambda: _env_int("VOLUME_LOOKBACK_BARS", 6))
    breakout_buffer_pct: float = field(default_factory=lambda: _env_float("BREAKOUT_BUFFER_PCT", 0.0))
    ema_cloud_filter: bool = field(
        default_factory=lambda: os.environ.get("EMA_CLOUD_FILTER", "true").lower() == "true"
    )

    # Gameplan (hold/rejection zone) strategy
    gameplan_require_bull_close: bool = field(
        default_factory=lambda: os.environ.get("GAMEPLAN_REQUIRE_BULL_CLOSE", "true").lower() == "true"
    )
    gameplan_require_bear_close: bool = field(
        default_factory=lambda: os.environ.get("GAMEPLAN_REQUIRE_BEAR_CLOSE", "true").lower() == "true"
    )
    gameplan_zone_request_timeout_seconds: int = field(
        default_factory=lambda: _env_int("GAMEPLAN_ZONE_REQUEST_TIMEOUT_SECONDS", 1800)
    )

    # Risk management
    risk_pct_per_trade: float = field(default_factory=lambda: _env_float("RISK_PCT_PER_TRADE", 0.01))
    stop_loss_pct: float = field(default_factory=lambda: _env_float("STOP_LOSS_PCT", 0.50))
    profit_target_pct: float = field(default_factory=lambda: _env_float("PROFIT_TARGET_PCT", 1.00))
    max_trades_per_day: int = field(default_factory=lambda: _env_int("MAX_TRADES_PER_DAY", 2))
    max_daily_loss_pct: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS_PCT", 0.03))
    max_contracts_per_trade: int = field(default_factory=lambda: _env_int("MAX_CONTRACTS_PER_TRADE", 5))

    # Session timing (US/Eastern, "HH:MM")
    market_open: time = field(default_factory=lambda: _env_time("MARKET_OPEN", "09:30"))
    entry_cutoff: time = field(default_factory=lambda: _env_time("ENTRY_CUTOFF", "14:30"))
    hard_exit: time = field(default_factory=lambda: _env_time("HARD_EXIT", "15:45"))
    market_close: time = field(default_factory=lambda: _env_time("MARKET_CLOSE", "16:00"))

    poll_seconds: int = field(default_factory=lambda: _env_int("POLL_SECONDS", 60))
    # How often /status and the Positions button are checked for, separate
    # from poll_seconds (which paces the trading logic: broker calls, signal
    # evaluation). A status check is just a cheap non-blocking Telegram
    # getUpdates call, not a broker call, so it can run far more often than
    # the trading logic without adding real API load -- keeps status replies
    # snappy even when poll_seconds is set high.
    status_poll_seconds: int = field(default_factory=lambda: _env_int("STATUS_POLL_SECONDS", 5))
    trade_log_path: str = field(default_factory=lambda: os.environ.get("TRADE_LOG_PATH", "trade_log.csv"))

    # Telegram alerts + approve/decline confirmation. Optional -- if either
    # is unset the agent falls back to the terminal confirmation prompt.
    telegram_bot_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))
    telegram_confirm_timeout_seconds: int = field(
        default_factory=lambda: _env_int("TELEGRAM_CONFIRM_TIMEOUT_SECONDS", 300)
    )

    # Robinhood's official Agentic Trading MCP server. Used (when enabled)
    # for account/equity data in --live mode via OAuth -- see mcp_broker.py.
    # As of this writing, options order placement is not yet exposed on
    # this server, so option chain lookups and order submission still go
    # through the robin_stocks fallback regardless of this setting.
    use_robinhood_mcp: bool = field(
        default_factory=lambda: os.environ.get("USE_ROBINHOOD_MCP", "true").lower() == "true"
    )
    robinhood_mcp_url: str = field(
        default_factory=lambda: os.environ.get("ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading")
    )
    robinhood_mcp_token_cache_path: str = field(
        default_factory=lambda: os.environ.get("ROBINHOOD_MCP_TOKEN_CACHE_PATH", ".robinhood_mcp_tokens.json")
    )
    robinhood_mcp_oauth_port: int = field(
        default_factory=lambda: _env_int("ROBINHOOD_MCP_OAUTH_PORT", 8765)
    )


CONFIG = Config()
