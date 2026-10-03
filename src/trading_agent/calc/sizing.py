"""Position sizing (IMPLEMENTATION.md 9.2). Pure; the risk engine calls it with live values."""

from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

ZERO = Decimal(0)

Binding = Literal["risk", "position", "cash"]


class SizingLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    budget_eur: Decimal  # the agent's capital cap, also in paper
    max_risk_pct: Decimal  # of budget, per trade: (entry - stop) * qty
    max_position_pct: Decimal  # of budget, per position: entry * qty
    min_position_eur: Decimal
    cash_reserve_pct: Decimal  # of budget, never spent


class Sizing(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: int
    binding: Binding | None  # the cap that limited the quantity
    reason: str | None  # why the quantity is 0
    risk: Decimal  # quantity * (entry - stop), instrument currency
    value: Decimal  # quantity * entry, instrument currency


def _rejected(reason: str) -> Sizing:
    return Sizing(quantity=0, binding=None, reason=reason, risk=ZERO, value=ZERO)


def position_size(
    *,
    entry: Decimal,
    stop: Decimal,
    settled_cash: Decimal,
    eur_rate: Decimal,
    limits: SizingLimits,
) -> Sizing:
    """Whole shares for a long trade.

    q = floor(min(risk_pct * B / (E - S), position_pct * B / E, (cash - reserve) / E))

    `settled_cash` is in the instrument currency; `eur_rate` converts EUR into it (units per
    1 EUR, 1 for EUR instruments). Returns quantity 0 with a reason instead of raising.
    """
    if entry <= 0 or stop <= 0 or eur_rate <= 0:
        return _rejected("non-positive price or FX rate")
    if stop >= entry:
        return _rejected("stop must be below entry for a long trade")

    budget = limits.budget_eur * eur_rate
    caps: dict[Binding, Decimal] = {
        "risk": limits.max_risk_pct / 100 * budget / (entry - stop),
        "position": limits.max_position_pct / 100 * budget / entry,
        "cash": (settled_cash - limits.cash_reserve_pct / 100 * budget) / entry,
    }
    binding = min(caps, key=lambda k: caps[k])
    quantity = max(int(caps[binding].to_integral_value(rounding=ROUND_FLOOR)), 0)
    if quantity == 0:
        return _rejected(f"no capacity ({binding} limit allows less than one share)")
    value = entry * quantity
    if value < limits.min_position_eur * eur_rate:
        return _rejected(f"position {value:.2f} below the minimum size ({binding} limit binds)")
    return Sizing(
        quantity=quantity,
        binding=binding,
        reason=None,
        risk=(entry - stop) * quantity,
        value=value,
    )
