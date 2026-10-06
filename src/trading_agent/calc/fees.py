"""IBKR commission model (Tiered/Fixed) per market, for fee checks and simulated fills."""

from decimal import ROUND_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.domain.market import Currency, Market

ZERO = Decimal(0)
CENT = Decimal("0.01")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CommissionRule(_Frozen):
    per_share: Decimal = ZERO
    pct_of_value: Decimal = ZERO  # in percent: 0.05 means 0.05 %
    min: Decimal = ZERO
    max: Decimal | None = None
    max_pct_of_value: Decimal | None = None
    third_party_per_share: Decimal = ZERO  # exchange + clearing estimate
    third_party_pct_of_value: Decimal = ZERO
    third_party_min: Decimal = ZERO


class SellRegulatory(_Frozen):
    """Fees charged on sales only (US: SEC Section 31 fee, FINRA TAF)."""

    pct_of_value: Decimal = ZERO
    per_share: Decimal = ZERO
    per_share_max: Decimal | None = None


class MarketFees(_Frozen):
    currency: Currency
    plan: Literal["tiered", "fixed"]
    tiered: CommissionRule
    fixed: CommissionRule
    sell_regulatory: SellRegulatory = Field(default_factory=SellRegulatory)


class FeeSchedule(_Frozen):
    markets: dict[Market, MarketFees]


def _commission(rule: CommissionRule, quantity: Decimal, value: Decimal) -> Decimal:
    fee = max(rule.per_share * quantity + rule.pct_of_value / 100 * value, rule.min)
    caps: list[Decimal] = []
    if rule.max is not None:
        caps.append(rule.max)
    if rule.max_pct_of_value is not None:
        caps.append(rule.max_pct_of_value / 100 * value)
    # IBKR: if the cap is below the minimum, the cap applies.
    return min([fee, *caps])


def _third_party(rule: CommissionRule, quantity: Decimal, value: Decimal) -> Decimal:
    variable = rule.third_party_per_share * quantity + rule.third_party_pct_of_value / 100 * value
    return max(variable, rule.third_party_min) if variable > 0 else ZERO


def order_fees(
    schedule: FeeSchedule,
    market: Market,
    side: Literal["buy", "sell"],
    quantity: Decimal,
    price: Decimal,
) -> Decimal:
    """Estimated fees for one order in the instrument currency, rounded up to the cent."""
    if quantity <= 0:
        return ZERO
    fees = schedule.markets[market]
    rule = fees.tiered if fees.plan == "tiered" else fees.fixed
    value = price * quantity
    total = _commission(rule, quantity, value) + _third_party(rule, quantity, value)
    if side == "sell":
        reg = fees.sell_regulatory
        per_share = reg.per_share * quantity
        if reg.per_share_max is not None:
            per_share = min(per_share, reg.per_share_max)
        total += reg.pct_of_value / 100 * value + per_share
    return total.quantize(CENT, rounding=ROUND_UP)


def round_trip_fees(
    schedule: FeeSchedule, market: Market, quantity: Decimal, entry: Decimal, exit_price: Decimal
) -> Decimal:
    return order_fees(schedule, market, "buy", quantity, entry) + order_fees(
        schedule, market, "sell", quantity, exit_price
    )
