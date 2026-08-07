"""Main loop: watch IWM, generate 0DTE ORB signals, propose trades, and only
ever act on them after an explicit human confirmation.

Usage:
    python -m iwm_0dte_agent.agent                 # paper trading (default)
    python -m iwm_0dte_agent.agent --live           # real Robinhood orders
    python -m iwm_0dte_agent.agent --once           # single iteration, for testing

Nothing here places a real order without both (a) the --live flag and (b) a
"y" typed at the confirm() prompt for that specific trade. There is no
autopilot mode.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time as time_module

from .broker import Broker, RobinhoodBroker
from .config import CONFIG, Config
from .confirm import confirm
from .models import OpenPosition, ProposedOrder
from .paper_broker import PaperBroker
from .risk import RiskManager
from .strategy import generate_signal, select_strike
from .trade_log import TradeLog

logger = logging.getLogger(__name__)


def _build_broker(live: bool, config: Config) -> Broker:
    return RobinhoodBroker(config) if live else PaperBroker(config)


def _today_open(config: Config) -> dt.datetime:
    now = dt.datetime.now()
    return now.replace(
        hour=config.market_open.hour, minute=config.market_open.minute,
        second=0, microsecond=0,
    )


def _try_open_position(
    broker: Broker, risk: RiskManager, trade_log: TradeLog, config: Config, live: bool,
) -> OpenPosition | None:
    buying_power = broker.get_buying_power()
    can_open, why = risk.can_open_new_trade(buying_power, dt.datetime.now().time())
    if not can_open:
        logger.debug("Not opening a new trade: %s", why)
        return None

    bars = broker.get_intraday_bars(config.symbol, since=_today_open(config))
    signal = generate_signal(bars, config.market_open, config.orb_minutes, config.vwap_filter)
    if signal is None:
        return None

    chain = broker.get_0dte_chain(config.symbol)
    contract = select_strike(chain, signal.option_type, signal.underlying_price, config.strike_offset)
    if contract is None or contract.ask <= 0:
        logger.warning("No usable %s contract found near %.2f", signal.option_type.value, signal.underlying_price)
        return None

    quantity = risk.position_size(buying_power, contract.ask)
    if quantity <= 0:
        logger.info("Position size computed to 0 contracts, skipping signal: %s", signal.reason)
        return None

    proposed = ProposedOrder(
        contract=contract,
        quantity=quantity,
        limit_price=contract.ask,
        stop_loss_price=round(contract.ask * (1 - config.stop_loss_pct), 2),
        profit_target_price=round(contract.ask * (1 + config.profit_target_pct), 2),
        reason=signal.reason,
    )
    trade_log.write(
        "proposed_open", symbol=contract.symbol, option_type=contract.option_type.value,
        strike=contract.strike, expiration=contract.expiration, quantity=quantity,
        price=contract.ask, reason=signal.reason,
    )

    if not confirm(proposed, live):
        trade_log.write(
            "declined_open", symbol=contract.symbol, option_type=contract.option_type.value,
            strike=contract.strike, expiration=contract.expiration, quantity=quantity,
            price=contract.ask, reason=signal.reason,
        )
        return None

    result = broker.submit_order(contract, quantity, contract.ask, side="buy")
    trade_log.write(
        "filled_open" if result.submitted else "failed_open",
        symbol=contract.symbol, option_type=contract.option_type.value,
        strike=contract.strike, expiration=contract.expiration, quantity=quantity,
        price=contract.ask, detail=result.detail,
    )
    if not result.submitted:
        logger.error("Order failed: %s", result.detail)
        return None

    risk.record_trade_opened()
    return OpenPosition(
        contract=contract, quantity=quantity, entry_price=contract.ask,
        stop_loss_price=proposed.stop_loss_price,
        profit_target_price=proposed.profit_target_price,
        opened_at=dt.datetime.now(),
    )


def _check_exit(position: OpenPosition, broker: Broker, config: Config, risk: RiskManager) -> str | None:
    now = dt.datetime.now().time()
    if risk.is_hard_exit_time(now):
        return f"hard exit time reached ({config.hard_exit})"
    quote = broker.get_option_quote(position.contract.contract_id)
    if quote.bid <= position.stop_loss_price:
        return f"stop loss hit (bid {quote.bid:.2f} <= {position.stop_loss_price:.2f})"
    if quote.bid >= position.profit_target_price:
        return f"profit target hit (bid {quote.bid:.2f} >= {position.profit_target_price:.2f})"
    return None


def _try_close_position(
    position: OpenPosition, broker: Broker, risk: RiskManager, trade_log: TradeLog, live: bool, config: Config,
) -> bool:
    reason = _check_exit(position, broker, config, risk)
    if reason is None:
        return False

    quote = broker.get_option_quote(position.contract.contract_id)
    proposed = ProposedOrder(
        contract=quote, quantity=position.quantity, limit_price=quote.bid,
        stop_loss_price=position.stop_loss_price, profit_target_price=position.profit_target_price,
        reason=reason,
    )
    trade_log.write(
        "proposed_close", symbol=quote.symbol, option_type=quote.option_type.value,
        strike=quote.strike, expiration=quote.expiration, quantity=position.quantity,
        price=quote.bid, reason=reason,
    )
    if not confirm(proposed, live):
        trade_log.write(
            "declined_close", symbol=quote.symbol, option_type=quote.option_type.value,
            strike=quote.strike, expiration=quote.expiration, quantity=position.quantity,
            price=quote.bid, reason=reason,
        )
        return False

    result = broker.submit_order(position.contract, position.quantity, quote.bid, side="sell")
    pnl = (quote.bid - position.entry_price) * position.quantity * 100
    trade_log.write(
        "filled_close" if result.submitted else "failed_close",
        symbol=quote.symbol, option_type=quote.option_type.value, strike=quote.strike,
        expiration=quote.expiration, quantity=position.quantity, price=quote.bid,
        reason=reason, detail=f"pnl={pnl:.2f} {result.detail}",
    )
    if result.submitted:
        risk.record_trade_closed(pnl)
    return result.submitted


def run(live: bool, once: bool, config: Config = CONFIG) -> None:
    if live:
        print(
            "\n*** LIVE MODE: this will place REAL orders on Robinhood using your "
            "real account and money. Robinhood has no official trading API; this "
            "uses an unofficial client (robin_stocks) that is against their Terms "
            "of Service. 0DTE options can lose their full value within hours. ***\n"
        )
        typed = input("Type LIVE to confirm you understand and want to proceed: ").strip()
        if typed != "LIVE":
            print("Aborting.")
            return

    broker = _build_broker(live, config)
    broker.login()
    risk = RiskManager(config=config)
    trade_log = TradeLog(config.trade_log_path)

    position: OpenPosition | None = None
    logger.info("Agent started (%s mode) for %s", "LIVE" if live else "paper", config.symbol)

    while True:
        now = dt.datetime.now().time()
        if now >= config.market_close:
            logger.info("Market closed, stopping.")
            break

        try:
            if position is not None:
                if _try_close_position(position, broker, risk, trade_log, live, config):
                    position = None
            else:
                position = _try_open_position(broker, risk, trade_log, config, live)
        except Exception:
            logger.exception("Error in agent loop iteration")

        if once:
            break
        time_module.sleep(config.poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="IWM 0DTE opening-range-breakout signal agent")
    parser.add_argument("--live", action="store_true", help="Place real orders on Robinhood (default: paper trading)")
    parser.add_argument("--once", action="store_true", help="Run a single iteration and exit (useful for testing)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(live=args.live, once=args.once)


if __name__ == "__main__":
    main()
