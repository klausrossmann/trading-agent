from decimal import Decimal
from pathlib import Path

import pytest

from trading_agent.calc.fees import FeeSchedule, order_fees, round_trip_fees
from trading_agent.settings import load_fees

ROOT = Path(__file__).resolve().parents[3]
D = Decimal


@pytest.fixture(scope="module")
def fees() -> FeeSchedule:
    return load_fees(ROOT / "config")


def _plan(fees: FeeSchedule, market: str, plan: str) -> FeeSchedule:
    markets = dict(fees.markets)
    markets[market] = markets[market].model_copy(update={"plan": plan})  # pyright: ignore[reportArgumentType]
    return fees.model_copy(update={"markets": markets})


def test_us_tiered_small_order(fees: FeeSchedule) -> None:
    # buy 40 @ 52.10: 40 x 0.0035 = 0.14 -> min 0.35; third party 40 x 0.0032 = 0.128 -> 0.48
    assert order_fees(fees, "US", "buy", 40, D("52.10")) == D("0.48")
    # sell 40 @ 57: 0.35 + 0.128 + SEC 0.00278 % x 2280 = 0.0634 + TAF 40 x 0.000195 = 0.0078
    assert order_fees(fees, "US", "sell", 40, D("57.00")) == D("0.55")
    assert round_trip_fees(fees, "US", 40, D("52.10"), D("57.00")) == D("1.03")


def test_us_fixed_cap_below_minimum_applies(fees: FeeSchedule) -> None:
    # IBKR's own example: 10 shares @ 0.20 -> 0.05, minimum 1.00, capped at 1 % x 2.00 = 0.02
    assert order_fees(_plan(fees, "US", "fixed"), "US", "buy", 10, D("0.20")) == D("0.02")


def test_eu_tiered(fees: FeeSchedule) -> None:
    # 6 @ 50 = 300: 0.05 % = 0.15 -> min 1.25; third party 0.005 % = 0.015 -> min 0.60
    assert order_fees(fees, "EU", "buy", 6, D("50")) == D("1.85")
    assert round_trip_fees(fees, "EU", 6, D("50"), D("50")) == D("3.70")  # CONCEPT 6.3: 3-4
    # 1000 @ 100 = 100,000: 0.05 % = 50 -> max 29; third party 0.005 % = 5.00
    assert order_fees(fees, "EU", "sell", 1000, D("100")) == D("34.00")


def test_eu_fixed(fees: FeeSchedule) -> None:
    fixed = _plan(fees, "EU", "fixed")
    assert round_trip_fees(fixed, "EU", 6, D("50"), D("50")) == D("6.00")  # CONCEPT 6.3: 6
    assert order_fees(fixed, "EU", "buy", 1000, D("100")) == D("50.00")  # no maximum


def test_zero_quantity_costs_nothing(fees: FeeSchedule) -> None:
    assert order_fees(fees, "US", "buy", 0, D("10")) == 0
