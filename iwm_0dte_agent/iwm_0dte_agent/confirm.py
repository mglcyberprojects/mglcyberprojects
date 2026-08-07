"""Human-in-the-loop confirmation gate.

This is the safety boundary of the whole project: nothing in `agent.py`
calls `broker.submit_order` without going through `confirm()` first, and
this default implementation always asks on the terminal. It is intentionally
the *only* place a "yes" can come from -- there is no config flag that
bypasses it.
"""

from __future__ import annotations

from .models import ProposedOrder


def confirm(order: ProposedOrder, live: bool) -> bool:
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
