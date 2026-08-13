"""Main loop: watch every ticker in config.symbols, generate 0DTE ORB+EMA
cloud signals per symbol, propose trades, and only ever act on them after an
explicit human confirmation -- via Telegram if configured, otherwise the
terminal.

Usage:
    python -m iwm_0dte_agent.agent                 # paper trading (default)
    python -m iwm_0dte_agent.agent --live           # real Robinhood orders
    python -m iwm_0dte_agent.agent --once           # single iteration, for testing

Nothing here places a real order without both (a) the --live flag and (b) an
explicit approval from the notifier's confirm() for that specific trade.
There is no autopilot mode.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time as time_module

from .broker import Broker, RobinhoodBroker
from .config import CONFIG, Config
from .gameplan_strategy import GameplanState, GameplanZones
from .mcp_broker import MCPBroker
from .models import AgentStatus, Bar, OpenPosition, PositionStatus, ProposedOrder, TradeSignal
from .notifier import Notifier, build_notifier
from .paper_broker import PaperBroker
from .risk import RiskManager
from .strategy import atm_contract, generate_signal, select_strike_by_dollar_offset
from .trade_log import TradeLog

logger = logging.getLogger(__name__)


def _build_broker(live: bool, config: Config) -> Broker:
    if not live:
        return PaperBroker(config)
    return MCPBroker(config) if config.use_robinhood_mcp else RobinhoodBroker(config)


def list_mcp_tools(config: Config = CONFIG) -> None:
    """Connect to Robinhood's MCP server and print every discovered tool,
    without touching robin_stocks or running the trading loop. Useful for
    checking, e.g., whether options tools have shown up yet.
    """
    broker = MCPBroker(config)
    try:
        broker.connect()
        # close() tears down the session (and its discovered tool list) --
        # grab what we need while it's still alive, not after.
        tool_names = sorted(broker.list_discovered_tools())
    finally:
        broker.close()
    print(f"\nDiscovered {len(tool_names)} MCP tools at {config.robinhood_mcp_url}:")
    for name in tool_names:
        print(f"  - {name}")


def _today_open(config: Config) -> dt.datetime:
    now = dt.datetime.now()
    return now.replace(
        hour=config.market_open.hour, minute=config.market_open.minute,
        second=0, microsecond=0,
    )


def _alert_deduped(
    key: str, message: str, notifier: Notifier, alerted_reasons: set[tuple[dt.date, str]],
) -> None:
    """Send `message` at most once per calendar day per `key`.

    Without this, any persistent condition -- a blocked-entry reason, a
    stuck loop error, a signal that keeps re-firing with no usable contract
    or affordable quantity -- would re-alert on every poll cycle (every
    `poll_seconds`, the default is once a minute) for the rest of the
    session. `key` should describe the *category* of the condition, not
    include values that change every cycle (like a live price) or every
    alert in that category would look "new" and defeat the dedup.
    """
    full_key = (dt.date.today(), key)
    if full_key in alerted_reasons:
        return
    alerted_reasons.add(full_key)
    notifier.alert(message)


def _maybe_alert_blocked_entry(
    why: str, notifier: Notifier, alerted_reasons: set[tuple[dt.date, str]],
) -> None:
    _alert_deduped(why, f"No new entries: {why}", notifier, alerted_reasons)


def _maybe_alert_loop_error(
    exc: Exception, notifier: Notifier, alerted_reasons: set[tuple[dt.date, str]],
) -> None:
    # Keyed with an "error: " prefix so it can't collide with a
    # blocked-entry reason that happens to read the same as an exception's repr.
    _alert_deduped(f"error: {exc!r}", f"Error in agent loop: {exc!r}", notifier, alerted_reasons)


def _generate_signal(
    config: Config, bars: list[Bar], gameplan_state: GameplanState | None, gameplan_zones: GameplanZones | None,
    notifier: Notifier,
) -> TradeSignal | None:
    if config.strategy == "gameplan":
        if gameplan_zones is None or not bars or gameplan_state is None:
            return None
        evaluation = gameplan_state.evaluate(
            bars[-1], gameplan_zones,
            require_bull_close=config.gameplan_require_bull_close,
            require_bear_close=config.gameplan_require_bear_close,
        )
        if evaluation.support_broken:
            notifier.alert(f"Gameplan: support broken (close below hold zone low {gameplan_zones.hold_low:g})")
        if evaluation.resistance_broken:
            notifier.alert(f"Gameplan: resistance broken (close above rejection zone high {gameplan_zones.reject_high:g})")
        return evaluation.signal

    return generate_signal(
        bars, config.market_open, config.orb_minutes,
        use_vwap_filter=config.vwap_filter,
        use_volume_filter=config.volume_filter,
        volume_multiplier=config.volume_multiplier,
        volume_lookback_bars=config.volume_lookback_bars,
        breakout_buffer_pct=config.breakout_buffer_pct,
        use_ema_cloud_filter=config.ema_cloud_filter,
    )


def _try_open_position(
    symbol: str, broker: Broker, risk: RiskManager, trade_log: TradeLog, config: Config, live: bool,
    notifier: Notifier, alerted_reasons: set[tuple[dt.date, str]],
    gameplan_state: GameplanState | None = None, gameplan_zones: GameplanZones | None = None,
) -> OpenPosition | None:
    buying_power = broker.get_buying_power()
    can_open, why = risk.can_open_new_trade(buying_power, dt.datetime.now().time())
    if not can_open:
        logger.debug("Not opening a new trade: %s", why)
        _maybe_alert_blocked_entry(why, notifier, alerted_reasons)
        return None

    bars = broker.get_intraday_bars(symbol, since=_today_open(config))
    signal = _generate_signal(config, bars, gameplan_state, gameplan_zones, notifier)
    if signal is None:
        return None

    chain = broker.get_0dte_chain(symbol)
    contract = select_strike_by_dollar_offset(
        chain, signal.option_type, signal.underlying_price, config.strike_dollar_offset
    )
    if contract is None or contract.ask <= 0:
        logger.warning("No usable %s contract found near %.2f for %s", signal.option_type.value, signal.underlying_price, symbol)
        # Diagnostic detail for tuning STRIKE_DOLLAR_OFFSET/RISK_PCT_PER_TRADE
        # from trade_log.csv after the fact -- this branch otherwise wrote
        # nothing to the log at all, so there was no record of *why* a
        # signal that kept re-firing never turned into a trade. Logged only
        # (no Telegram alert) -- a signal that keeps missing this by a little
        # is routine, not something worth a ping for every day it happens;
        # check trade_log.csv if you want to know.
        atm = atm_contract(chain, signal.option_type, signal.underlying_price)
        atm_ask = atm.ask if atm is not None else None
        trade_log.write(
            "skipped_no_contract", symbol=symbol, option_type=signal.option_type.value,
            strike=0, expiration="", quantity=0, price=atm_ask or 0, reason=signal.reason,
            detail=f"atm_ask={atm_ask} strike_dollar_offset={config.strike_dollar_offset}",
        )
        return None

    choices = risk.quantity_choices(buying_power, contract.ask)
    if not choices:
        logger.info("No affordable contract quantity, skipping signal: %s", signal.reason)
        # Logged only, same reasoning as the no-usable-contract branch above.
        trade_log.write(
            "skipped_no_quantity", symbol=contract.symbol, option_type=contract.option_type.value,
            strike=contract.strike, expiration=contract.expiration, quantity=0, price=contract.ask,
            reason=signal.reason,
            detail=f"buying_power={buying_power:.2f} cost_per_contract={contract.ask * 100:.2f}",
        )
        return None

    proposed = ProposedOrder(
        contract=contract,
        quantity=choices[0],
        quantity_choices=choices,
        limit_price=contract.ask,
        stop_loss_price=round(contract.ask * (1 - config.stop_loss_pct), 2),
        profit_target_price=round(contract.ask * (1 + config.profit_target_pct), 2),
        reason=signal.reason,
    )
    trade_log.write(
        "proposed_open", symbol=contract.symbol, option_type=contract.option_type.value,
        strike=contract.strike, expiration=contract.expiration, quantity=0,
        price=contract.ask, reason=f"{signal.reason} (choices: {choices})",
    )

    quantity = notifier.confirm(proposed, live)
    if quantity <= 0:
        trade_log.write(
            "declined_open", symbol=contract.symbol, option_type=contract.option_type.value,
            strike=contract.strike, expiration=contract.expiration, quantity=0,
            price=contract.ask, reason=signal.reason,
        )
        notifier.alert(f"Declined: {contract.option_type.value.upper()} ${contract.strike:g} entry")
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
        notifier.alert(f"Order FAILED: {contract.option_type.value.upper()} ${contract.strike:g} -- {result.detail}")
        return None

    notifier.alert(
        f"Filled: BUY {quantity}x {contract.option_type.value.upper()} "
        f"{contract.symbol} ${contract.strike:g} @ ${contract.ask:.2f}"
    )
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
    position: OpenPosition, broker: Broker, risk: RiskManager, trade_log: TradeLog, live: bool,
    config: Config, notifier: Notifier,
) -> bool:
    reason = _check_exit(position, broker, config, risk)
    if reason is None:
        return False

    quote = broker.get_option_quote(position.contract.contract_id)
    proposed = ProposedOrder(
        contract=quote, quantity=position.quantity, limit_price=quote.bid,
        stop_loss_price=position.stop_loss_price, profit_target_price=position.profit_target_price,
        reason=reason, entry_price=position.entry_price,
    )
    trade_log.write(
        "proposed_close", symbol=quote.symbol, option_type=quote.option_type.value,
        strike=quote.strike, expiration=quote.expiration, quantity=position.quantity,
        price=quote.bid, reason=reason,
    )
    if not notifier.confirm(proposed, live):
        trade_log.write(
            "declined_close", symbol=quote.symbol, option_type=quote.option_type.value,
            strike=quote.strike, expiration=quote.expiration, quantity=position.quantity,
            price=quote.bid, reason=reason,
        )
        notifier.alert(f"Declined close ({reason}) -- position still open, will re-check next cycle")
        return False

    result = broker.submit_order(position.contract, position.quantity, quote.bid, side="sell")
    pnl = proposed.pnl_dollars
    pnl_pct = proposed.pnl_pct
    trade_log.write(
        "filled_close" if result.submitted else "failed_close",
        symbol=quote.symbol, option_type=quote.option_type.value, strike=quote.strike,
        expiration=quote.expiration, quantity=position.quantity, price=quote.bid,
        reason=reason, detail=f"pnl={pnl:.2f} {result.detail}",
    )
    if result.submitted:
        risk.record_trade_closed(pnl)
        notifier.alert(
            f"Closed ({reason}): SELL {position.quantity}x {quote.option_type.value.upper()} "
            f"{quote.symbol} ${quote.strike:g} @ ${quote.bid:.2f} -- P&L ${pnl:+.2f} ({pnl_pct:+.1f}%)"
        )
    else:
        notifier.alert(f"Close order FAILED ({reason}) -- {result.detail}")
    return result.submitted


def _build_agent_status(
    positions: dict[str, OpenPosition], broker: Broker, risk: RiskManager, config: Config,
) -> AgentStatus:
    position_statuses: list[PositionStatus] = []
    for position in positions.values():
        quote = broker.get_option_quote(position.contract.contract_id)
        position_statuses.append(PositionStatus(
            contract=position.contract, quantity=position.quantity, entry_price=position.entry_price,
            current_bid=quote.bid, stop_loss_price=position.stop_loss_price,
            profit_target_price=position.profit_target_price,
        ))
    return AgentStatus(
        positions=position_statuses, buying_power=broker.get_buying_power(),
        trades_today=risk.trades_today, max_trades_per_day=config.max_trades_per_day,
        realized_pnl_today=risk.realized_pnl_today,
    )


def _handle_status_requests(
    notifier: Notifier, broker: Broker, risk: RiskManager, positions: dict[str, OpenPosition], config: Config,
) -> None:
    """On-demand /status command + Refresh button -- checked every
    status_poll_seconds (default 5s), independent of poll_seconds (which
    paces the trading logic), so replies stay snappy without evaluating
    signals any more often. Builds one live status snapshot (across every
    symbol with an open position) and reuses it for every pending request
    this cycle, rather than re-fetching per request, since they'd all show
    the same moment-in-time numbers anyway.
    """
    pending = notifier.poll_status_requests()
    if not pending:
        return
    status = _build_agent_status(positions, broker, risk, config)
    for req in pending:
        if req.kind == "new":
            notifier.post_status(status)
        else:
            notifier.update_status(req.message_id, status)


def _check_status_safely(
    notifier: Notifier, broker: Broker, risk: RiskManager, positions: dict[str, OpenPosition], config: Config,
    alerted_reasons: set[tuple[dt.date, str]],
) -> None:
    # Its own try/except, deliberately separate from the trading-logic
    # try/except in run()'s main loop: a broker/strategy error there (e.g. a
    # signal that can't fetch a quote) must not also block /status and the
    # Positions button from responding every time it recurs.
    try:
        _handle_status_requests(notifier, broker, risk, positions, config)
    except Exception as exc:
        logger.exception("Error handling status requests")
        _maybe_alert_loop_error(exc, notifier, alerted_reasons)


def run(live: bool, once: bool, config: Config = CONFIG) -> None:
    # Bail out before touching Telegram/the broker at all if the market's
    # already closed for the day (e.g. run_agent_live.bat's restart loop
    # cycling overnight) -- otherwise every ~30s restart would re-send the
    # LIVE confirmation prompt (for --live) or an Agent started/Market
    # closed alert pair (either mode), forever, until the next calendar
    # day. --once is exempt since it's for deliberate manual testing, e.g.
    # checking connectivity after hours.
    if not once and dt.datetime.now().time() >= config.market_close:
        logger.info("Market already closed for today (%s) -- not starting a session.", config.market_close)
        return

    # Built before the live-start gate below (not after, as it used to be)
    # so the gate itself can go through notifier.confirm_live_start() --
    # TerminalNotifier still requires typing LIVE at a keyboard;
    # TelegramNotifier requires replying LIVE from your phone, so an
    # unattended session (e.g. the Windows Scheduled Task) can still start
    # --live without a human at the machine, while still requiring an
    # explicit, deliberate confirmation rather than a single button tap.
    notifier = build_notifier(config)

    if live:
        if config.use_robinhood_mcp:
            broker_line = "This connects via Robinhood's official Agentic Trading MCP server (OAuth)."
        else:
            broker_line = (
                "Robinhood has no official trading API; this uses an unofficial client "
                "(robin_stocks) that is against their Terms of Service."
            )
        warning = (
            "LIVE MODE: this will place REAL orders on Robinhood using your real "
            f"account and money. {broker_line} 0DTE options can lose their full "
            "value within hours."
        )
        if not notifier.confirm_live_start(warning):
            logger.info("LIVE start not confirmed, aborting.")
            return

    broker = _build_broker(live, config)

    try:
        broker.login()
    except Exception as exc:
        notifier.alert(f"Agent failed to start: login error -- {exc}")
        raise

    risk = RiskManager(config=config)
    trade_log = TradeLog(config.trade_log_path)
    alerted_reasons: set[tuple[dt.date, str]] = set()

    # One open position per symbol, up to len(trading_symbols) concurrently
    # -- see _try_open_position/_try_close_position below, both symbol-scoped.
    # MAX_TRADES_PER_DAY/MAX_DAILY_LOSS_PCT are still global, enforced by the
    # single shared `risk` instance's can_open_new_trade() regardless of
    # which symbol is asking.
    positions: dict[str, OpenPosition] = {}
    mode = "LIVE" if live else "paper"

    # Gameplan is a single set of manually-input hold/rejection zones for
    # ONE ticker's structure -- it doesn't make sense applied to every
    # tracked symbol at once the way the ORB+EMA-cloud strategy does, so
    # only the first configured symbol trades under it.
    trading_symbols = config.symbols if config.strategy != "gameplan" else config.symbols[:1]

    logger.info("Agent started (%s mode, %s strategy) for %s", mode, config.strategy, ", ".join(trading_symbols))
    notifier.alert(f"Agent started ({mode} mode, {config.strategy} strategy) for {', '.join(trading_symbols)}")
    notifier.show_positions_shortcut()

    gameplan_state: GameplanState | None = None
    gameplan_zones: GameplanZones | None = None
    if config.strategy == "gameplan":
        gameplan_state = GameplanState()
        gameplan_zones = notifier.request_zones(config.gameplan_zone_request_timeout_seconds)
        if gameplan_zones is None:
            notifier.alert("No gameplan zones set -- the agent will keep running but won't open any positions today.")

    while True:
        now = dt.datetime.now().time()
        if now >= config.market_close:
            logger.info("Market closed, stopping.")
            notifier.alert("Market closed, agent stopping for the day.")
            break

        for symbol in trading_symbols:
            try:
                position = positions.get(symbol)
                if position is not None:
                    if _try_close_position(position, broker, risk, trade_log, live, config, notifier):
                        del positions[symbol]
                else:
                    new_position = _try_open_position(
                        symbol, broker, risk, trade_log, config, live, notifier, alerted_reasons,
                        gameplan_state, gameplan_zones,
                    )
                    if new_position is not None:
                        positions[symbol] = new_position
            except Exception as exc:
                logger.exception("Error in agent loop iteration (%s)", symbol)
                _maybe_alert_loop_error(exc, notifier, alerted_reasons)

        _check_status_safely(notifier, broker, risk, positions, config, alerted_reasons)

        if once:
            break

        # Sleep in status_poll_seconds increments (default 5s) instead of
        # one big poll_seconds sleep, re-checking for /status and Positions
        # taps after each -- a status check is a cheap non-blocking Telegram
        # call, not a broker call, so this doesn't make the trading logic
        # above (which still only runs once per poll_seconds) run any more often.
        remaining = config.poll_seconds
        while remaining > 0:
            nap = max(1, min(config.status_poll_seconds, remaining))
            time_module.sleep(nap)
            remaining -= nap
            _check_status_safely(notifier, broker, risk, positions, config, alerted_reasons)


def main() -> None:
    parser = argparse.ArgumentParser(description="IWM 0DTE opening-range-breakout signal agent")
    parser.add_argument("--live", action="store_true", help="Place real orders on Robinhood (default: paper trading)")
    parser.add_argument("--once", action="store_true", help="Run a single iteration and exit (useful for testing)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--list-mcp-tools", action="store_true",
        help="Connect to Robinhood's MCP server, print the tools it currently exposes, and exit "
             "(no robin_stocks login, no trading).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list_mcp_tools:
        list_mcp_tools()
        return

    run(live=args.live, once=args.once)


if __name__ == "__main__":
    main()
