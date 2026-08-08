"""Default notifier: prints alerts and asks for confirmation on stdin.

Used whenever Telegram isn't configured. This is the original safety
boundary of the project -- nothing is submitted to a broker without a typed
`y` here.
"""

from __future__ import annotations

from .gameplan_strategy import GameplanZones, parse_zone_message
from .models import AgentStatus, ProposedOrder
from .notifier import StatusRequest


def _print_status(status: AgentStatus) -> None:
    print("\n" + "-" * 60)
    print("STATUS")
    print("-" * 60)
    if status.position is not None:
        p = status.position
        print(f"  {p.contract.option_type.value.upper()} {p.contract.symbol} "
              f"${p.contract.strike:g} exp {p.contract.expiration}")
        print(f"  Qty {p.quantity} @ entry ${p.entry_price:.2f}, current bid ${p.current_bid:.2f}")
        print(f"  Unrealized P&L: {p.pnl_pct:+.1f}% (${p.pnl_dollars:+.2f})")
        print(f"  Stop loss ${p.stop_loss_price:.2f} / profit target ${p.profit_target_price:.2f}")
    else:
        print("  No open position.")
    print(f"  Buying power: ${status.buying_power:,.2f}")
    print(f"  Trades today: {status.trades_today}/{status.max_trades_per_day}  "
          f"Realized P&L today: ${status.realized_pnl_today:+.2f}")
    print("-" * 60)


class TerminalNotifier:
    def alert(self, text: str) -> None:
        print(f"\n[ALERT] {text}")

    def poll_status_requests(self) -> list[StatusRequest]:
        # Terminal mode is already attended and prints everything live to
        # stdout as it happens -- there's no on-demand /status command to
        # poll for the way Telegram mode has, so this is always empty.
        return []

    def post_status(self, status: AgentStatus) -> None:
        _print_status(status)

    def update_status(self, message_id: int, status: AgentStatus) -> None:
        _print_status(status)

    def request_zones(self, timeout_seconds: int) -> GameplanZones | None:
        # Terminal mode is already interactive/attended, so this ignores
        # timeout_seconds and just blocks on input() -- there's no console
        # you'd be leaving unattended the way Telegram mode needs a timeout for.
        print("\nGameplan strategy: enter today's zones.")
        print("Format: hold_low hold_high reject_low reject_high  (e.g. 228.50 229.20 231.00 232.50)")
        raw = input("Zones (blank to skip trading today): ").strip()
        if not raw:
            return None
        zones = parse_zone_message(raw)
        if zones is None:
            print("Could not parse that -- expected exactly 4 numbers. Skipping trading today.")
        return zones

    def confirm(self, order: ProposedOrder, live: bool) -> bool:
        mode = "LIVE (real money)" if live else "PAPER (simulated)"
        print("\n" + "=" * 60)
        print(f"PROPOSED TRADE -- {mode}")
        print("=" * 60)
        print(f"  {order.contract.option_type.value.upper()} {order.contract.symbol} "
              f"${order.contract.strike:g} exp {order.contract.expiration}")
        print(f"  Quantity:        {order.quantity} contract(s)")
        print(f"  Limit price:     ${order.limit_price:.2f}  "
              f"(bid ${order.contract.bid:.2f} / ask ${order.contract.ask:.2f})")
        print(f"  Est. cost:       ${order.limit_price * order.quantity * 100:.2f}")
        print(f"  Stop loss:       ${order.stop_loss_price:.2f}")
        print(f"  Profit target:   ${order.profit_target_price:.2f}")
        if order.entry_price is not None:
            print(f"  P&L:             {order.pnl_pct:+.1f}% (${order.pnl_dollars:+.2f})")
        print(f"  Reason:          {order.reason}")
        print("=" * 60)
        answer = input("Submit this order? [y/N]: ").strip().lower()
        return answer in ("y", "yes")
