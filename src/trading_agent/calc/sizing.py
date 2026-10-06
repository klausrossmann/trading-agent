"""Position sizing (IMPLEMENTATION.md 9.2). Pure; the risk engine calls it with live values."""

from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trading_agent.domain.numbers import QUANTITY_STEP

ZERO = Decimal(0)
ONE = Decimal(1)

Binding = Literal["risk", "position", "cash"]
Rejection = Literal["invalid", "no_capacity", "below_minimum"]


class SizingLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    budget_eur: Decimal  # the agent's capital cap, also in paper
    # Risk and position caps run from min (at min_confidence) to max (at max_confidence).
    min_risk_pct: Decimal  # of budget, per trade: (entry - stop) * qty
    max_risk_pct: Decimal
    min_position_pct: Decimal  # of budget, per position: entry * qty
    max_position_pct: Decimal
    min_confidence: Decimal
    max_confidence: Decimal
    min_position_eur: Decimal
    cash_reserve_pct: Decimal  # of budget, never spent


class Sizing(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: Decimal  # shares, rounded down to QUANTITY_STEP
    binding: Binding | None  # the cap that limited the quantity
    rejection: Rejection | None = None
    reason: str | None  # why the quantity is 0
    risk: Decimal  # quantity * (entry - stop), instrument currency
    value: Decimal  # quantity * entry, instrument currency
    conviction: Decimal = ONE  # 0 = smallest position the limits allow, 1 = largest


def _rejected(rejection: Rejection, reason: str) -> Sizing:
    return Sizing(
        quantity=ZERO, binding=None, rejection=rejection, reason=reason, risk=ZERO, value=ZERO
    )


def conviction(confidence: Decimal | None, limits: SizingLimits) -> Decimal:
    """0 at `min_confidence` rising linearly to 1 at `max_confidence`; None means full size."""
    if confidence is None:
        return ONE
    span = limits.max_confidence - limits.min_confidence
    if span <= 0:
        return ONE if confidence >= limits.max_confidence else ZERO
    return min(max((confidence - limits.min_confidence) / span, ZERO), ONE)


def position_size(
    *,
    entry: Decimal,
    stop: Decimal,
    settled_cash: Decimal,
    eur_rate: Decimal,
    limits: SizingLimits,
    confidence: Decimal | None = None,
) -> Sizing:
    """Fractional shares for a long trade.

    q = min(risk_pct * B / (E - S), position_pct * B / E, (cash - reserve) / E), rounded down

    risk_pct and position_pct grow with the conviction c in [0, 1] derived from `confidence`:
    pct = min_pct + c * (max_pct - min_pct).

    `settled_cash` is in the instrument currency; `eur_rate` converts EUR into it (units per
    1 EUR, 1 for EUR instruments). Returns quantity 0 with a reason instead of raising.
    """
    if entry <= 0 or stop <= 0 or eur_rate <= 0:
        return _rejected("invalid", "non-positive price or FX rate")
    if stop >= entry:
        return _rejected("invalid", "stop must be below entry for a long trade")

    c = conviction(confidence, limits)
    risk_pct = limits.min_risk_pct + c * (limits.max_risk_pct - limits.min_risk_pct)
    position_pct = limits.min_position_pct + c * (limits.max_position_pct - limits.min_position_pct)
    budget = limits.budget_eur * eur_rate
    caps: dict[Binding, Decimal] = {
        "risk": risk_pct / 100 * budget / (entry - stop),
        "position": position_pct / 100 * budget / entry,
        "cash": (settled_cash - limits.cash_reserve_pct / 100 * budget) / entry,
    }
    binding = min(caps, key=lambda k: caps[k])
    quantity = max(caps[binding].quantize(QUANTITY_STEP, rounding=ROUND_FLOOR), ZERO)
    if quantity == 0:
        return _rejected("no_capacity", f"no capacity ({binding} limit leaves no room)")
    value = entry * quantity
    if value < limits.min_position_eur * eur_rate:
        return _rejected(
            "below_minimum", f"position {value:.2f} below the minimum size ({binding} limit binds)"
        )
    return Sizing(
        quantity=quantity,
        binding=binding,
        reason=None,
        risk=(entry - stop) * quantity,
        value=value,
        conviction=c,
    )
