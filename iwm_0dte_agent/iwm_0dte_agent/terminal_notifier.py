"""Default notifier: prints alerts and asks for confirmation on stdin.

Used whenever Telegram isn't configured. This is the original safety
boundary of the project -- nothing is submitted to a broker without a typed
`y` here.
"""

from __future__ import annotations

from .models import ProposedOrder


class TerminalNotifier:
    def alert(self, text: str) -> None:
        print(f"\n[ALERT] {text}")

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
        print(f"  Reason:          {order.reason}")
        print("=" * 60)
        answer = input("Submit this order? [y/N]: ").strip().lower()
        return answer in ("y", "yes")
