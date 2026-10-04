"""Yearly tax helper (CONCEPT.md 12): realized gains and losses of share sales in EUR.

Not tax advice. Each sale is converted at the ECB rate of its trade day and set against the
average cost of the shares bought (also at their trade days' rates), fees included on both
sides. Shares form their own loss pot (§ 20 Abs. 6 Satz 4 EStG).
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from trading_agent.domain.market import Instrument
from trading_agent.domain.orders import Bracket
from trading_agent.portfolio.book import BookFill

CENT = Decimal("0.01")
TAX_RATE = Decimal("0.25") * Decimal("1.055")  # Abgeltungsteuer plus Solidaritätszuschlag


@dataclass(frozen=True)
class Sale:
    day: date
    symbol: str
    quantity: int
    bought: date
    cost_eur: Decimal  # average purchase cost incl. buy fees for the shares sold
    proceeds_eur: Decimal  # after sell fees
    fees_eur: Decimal

    @property
    def gain_eur(self) -> Decimal:
        return self.proceeds_eur - self.cost_eur


def sales(
    brackets: Sequence[Bracket],
    fills: Sequence[BookFill],
    instruments: Mapping[int, Instrument],
    year: int,
) -> list[Sale]:
    """One row per sell fill in `year`, in date order."""
    by_bracket: dict[UUID, list[BookFill]] = defaultdict(list)
    for f in fills:
        by_bracket[f.bracket_id].append(f)
    out: list[Sale] = []
    for b in brackets:
        entries = [f for f in by_bracket[b.id] if f.kind == "entry"]
        bought = sum(f.quantity for f in entries)
        if not bought:
            continue
        cost = sum(((f.quantity * f.price + f.fee) / f.eur_rate for f in entries), Decimal(0))
        buy_fees = sum((f.fee / f.eur_rate for f in entries), Decimal(0))
        for f in by_bracket[b.id]:
            if f.kind == "entry" or f.day.year != year:
                continue
            share = Decimal(f.quantity) / bought
            out.append(
                Sale(
                    day=f.day,
                    symbol=instruments[b.instrument_id].yahoo_symbol,
                    quantity=f.quantity,
                    bought=entries[0].day,
                    cost_eur=(cost * share).quantize(CENT),
                    proceeds_eur=((f.quantity * f.price - f.fee) / f.eur_rate).quantize(CENT),
                    fees_eur=(buy_fees * share + f.fee / f.eur_rate).quantize(CENT),
                )
            )
    return sorted(out, key=lambda s: (s.day, s.symbol))


def render(year: int, book: str, items: Sequence[Sale]) -> str:
    gains = sum((s.gain_eur for s in items if s.gain_eur > 0), Decimal(0))
    losses = sum((-s.gain_eur for s in items if s.gain_eur < 0), Decimal(0))
    net = gains - losses
    lines = [
        f"# Tax helper {year} ({book})",
        "",
        "Not tax advice. Amounts in EUR at the ECB reference rate of each trade day, fees "
        "included. Check the line numbers in the year's Anlage KAP (foreign capital income, "
        "since IBKR withholds no German tax).",
        "",
        "## Shares (own loss pot)",
        "",
        f"- Sales: {len(items)}",
        f"- Gains from share sales: {gains:.2f}",
        f"- Losses from share sales: {losses:.2f}",
        f"- Net: {net:.2f}",
        f"- Tax at 25 % plus solidarity surcharge, before any allowance (Sparer-Pauschbetrag) "
        f"and church tax: {max(net, Decimal(0)) * TAX_RATE:.2f}",
        "",
        "Losses only offset share gains (this year or carried forward), not other income.",
        "",
        "## Sales",
        "",
        "| Sold | Symbol | Qty | Bought | Cost | Proceeds | Fees | Gain |",
        "|---|---|---|---|---|---|---|---|",
        *[
            f"| {s.day} | {s.symbol} | {s.quantity} | {s.bought} | {s.cost_eur:.2f} | "
            f"{s.proceeds_eur:.2f} | {s.fees_eur:.2f} | {s.gain_eur:.2f} |"
            for s in items
        ],
        "",
        "## Not covered",
        "",
        "- Dividends and withholding tax, and gains or losses on the USD cash balance: take "
        "them from the IBKR Flex Query / activity statement.",
        "- Positions still open at the end of the year (not realized).",
    ]
    return "\n".join(lines) + "\n"
