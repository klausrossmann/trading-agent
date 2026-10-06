"""Tax helper: sales in EUR at trade-day rates, average cost, partial exits, the share pot."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_agent.domain.market import Instrument
from trading_agent.domain.orders import Bracket
from trading_agent.portfolio.book import BookFill
from trading_agent.portfolio.tax import TAX_RATE, render, sales

D = Decimal
AAPL = Instrument(
    id=1,
    symbol="AAPL",
    yahoo_symbol="AAPL",
    name="Apple",
    market="US",
    exchange="SMART",
    currency="USD",
    kind="stock",
)
SAP = AAPL.model_copy(update={"id": 2, "yahoo_symbol": "SAP.DE", "market": "EU", "currency": "EUR"})


def bracket(inst: int) -> Bracket:
    return Bracket(
        id=uuid4(),
        book="agent_live",
        instrument_id=inst,
        state="closed",
        quantity=D(4),
        entry=D(100),
        stop=D(96),
        initial_stop=D(96),
        target=D(108),
        expires=datetime(2027, 1, 1, tzinfo=UTC),
        filled_qty=D(4),
    )


def fill(b: Bracket, kind: str, qty: int, price: str, day: date, rate: str) -> BookFill:
    return BookFill(
        bracket_id=b.id,
        kind=kind,  # pyright: ignore[reportArgumentType]
        day=day,
        quantity=D(qty),
        price=D(price),
        fee=D(1),
        eur_rate=D(rate),
        settles=day,
    )


def test_sales_use_trade_day_rates_and_average_cost() -> None:
    us, eu, unfilled = bracket(1), bracket(2), bracket(1)
    fills = [
        fill(us, "entry", 2, "100", date(2026, 12, 1), "1.10"),
        fill(us, "entry", 2, "102", date(2026, 12, 2), "1.10"),
        fill(us, "target", 2, "110", date(2026, 12, 30), "1.05"),  # last year
        fill(us, "stop", 2, "95", date(2027, 1, 5), "1.00"),
        fill(eu, "entry", 4, "50", date(2027, 2, 1), "1"),
        fill(eu, "target", 4, "56", date(2027, 2, 10), "1"),
    ]
    rows = sales([us, eu, unfilled], fills, {1: AAPL, 2: SAP}, 2027)
    assert [(s.day, s.symbol, s.quantity, s.bought) for s in rows] == [
        (date(2027, 1, 5), "AAPL", 2, date(2026, 12, 1)),
        (date(2027, 2, 10), "SAP.DE", 4, date(2027, 2, 1)),
    ]
    aapl, sap = rows
    assert aapl.cost_eur == D("184.55")  # half of (202 + 206) / 1.10 incl. 2 x 1 fee
    assert aapl.proceeds_eur == D("189.00")  # (190 - 1) / 1.00
    assert aapl.fees_eur == D("1.91")  # half the buy fees in EUR plus the sell fee
    assert aapl.gain_eur == D("4.45")
    assert (sap.cost_eur, sap.proceeds_eur, sap.gain_eur) == (D(201), D(223), D(22))


def test_render() -> None:
    us = bracket(1)
    fills = [
        fill(us, "entry", 4, "100", date(2027, 3, 1), "1"),
        fill(us, "stop", 4, "95", date(2027, 3, 2), "1"),
    ]
    text = render(2027, "agent_live", sales([us], fills, {1: AAPL}, 2027))
    assert text.startswith("# Tax helper 2027 (agent_live)")
    assert "- Losses from share sales: 22.00" in text  # 401 cost, 379 proceeds
    assert "- Net: -22.00" in text
    assert "before any allowance (Sparer-Pauschbetrag) and church tax: 0.00" in text
    assert "| 2027-03-02 | AAPL | 4 | 2027-03-01 | 401.00 | 379.00 | 2.00 | -22.00 |" in text
    assert pytest.approx(D("0.26375")) == TAX_RATE
