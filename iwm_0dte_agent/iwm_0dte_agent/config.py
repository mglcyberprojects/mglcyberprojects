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


@dataclass(frozen=True)
class Config:
    symbol: str = "IWM"

    # Robinhood credentials (only needed when --live is passed)
    rh_username: str = field(default_factory=lambda: os.environ.get("ROBINHOOD_USERNAME", ""))
    rh_password: str = field(default_factory=lambda: os.environ.get("ROBINHOOD_PASSWORD", ""))
    rh_totp_secret: str = field(default_factory=lambda: os.environ.get("ROBINHOOD_TOTP_SECRET", ""))

    # Opening-range breakout strategy
    orb_minutes: int = field(default_factory=lambda: _env_int("ORB_MINUTES", 15))
    vwap_filter: bool = field(default_factory=lambda: os.environ.get("VWAP_FILTER", "true").lower() == "true")
    strike_offset: int = field(default_factory=lambda: _env_int("STRIKE_OFFSET", 0))  # 0 = ATM

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
    trade_log_path: str = field(default_factory=lambda: os.environ.get("TRADE_LOG_PATH", "trade_log.csv"))


CONFIG = Config()
