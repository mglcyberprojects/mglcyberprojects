"""Append-only CSV log of every proposed trade and its outcome (declined,
filled, or closed). Kept as plain CSV so it's easy to eyeball after a
session without extra tooling.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
from dataclasses import dataclass

_FIELDS = [
    "timestamp", "event", "symbol", "option_type", "strike", "expiration",
    "quantity", "price", "reason", "detail",
]


@dataclass
class TradeLog:
    path: str

    def _ensure_header(self) -> None:
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_FIELDS).writeheader()

    def write(self, event: str, **fields) -> None:
        self._ensure_header()
        row = {"timestamp": dt.datetime.now().isoformat(timespec="seconds"), "event": event}
        row.update(fields)
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDS).writerow(row)
