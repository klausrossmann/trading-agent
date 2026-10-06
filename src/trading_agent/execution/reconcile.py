"""Reconciliation, read side (IMPLEMENTATION.md 10.3): broker positions vs. the agent's book.

Pure. From M8 a mismatch halts the agent; until then it only raises an alert.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from trading_agent.domain.broker import BrokerPosition

TOLERANCE = 0.00005  # half of QUANTITY_STEP: floats from the broker vs the stored decimals


@dataclass(frozen=True)
class Mismatch:
    symbol: str
    expected: float
    actual: float


@dataclass(frozen=True)
class ReconcileReport:
    unknown: list[BrokerPosition]  # held at the broker, not in the book
    missing: list[Mismatch]  # in the book, not (fully) at the broker
    quantity: list[Mismatch]  # held at both, different size

    @property
    def clean(self) -> bool:
        return not (self.unknown or self.missing or self.quantity)


def reconcile(
    broker: Sequence[BrokerPosition], expected: Mapping[int, tuple[str, float]]
) -> ReconcileReport:
    """`expected`: conid -> (symbol, quantity) of the book's open positions."""
    held = {p.conid: p for p in broker}
    unknown = [p for conid, p in held.items() if conid not in expected]
    missing: list[Mismatch] = []
    quantity: list[Mismatch] = []
    for conid, (symbol, qty) in expected.items():
        p = held.get(conid)
        if p is None:
            missing.append(Mismatch(symbol, qty, 0.0))
        elif abs(p.quantity - qty) > TOLERANCE:
            quantity.append(Mismatch(symbol, qty, p.quantity))
    return ReconcileReport(unknown, missing, quantity)
