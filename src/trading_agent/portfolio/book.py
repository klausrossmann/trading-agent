"""Book accounting from brackets and fills (IMPLEMENTATION.md 9.6). Pure; amounts in EUR.

One account per budget sleeve: settled and unsettled cash, positions marked to market,
cash reserved for pending entries, round-trip trades, and the risk engine's PortfolioState.
"""

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from trading_agent.domain.market import Instrument, Market
from trading_agent.domain.orders import Bracket, OrderKind
from trading_agent.domain.risk import Holding, PortfolioState
from trading_agent.domain.trading import Book, Trade

CENT = Decimal("0.01")
PENDING = ("approved", "submitted", "working")


@dataclass(frozen=True)
class BookFill:
    bracket_id: UUID
    kind: OrderKind
    day: date  # trade date at the exchange
    quantity: int
    price: Decimal
    fee: Decimal  # instrument currency: the broker's commission or the fees.yaml estimate
    eur_rate: Decimal  # instrument currency per EUR on that day
    settles: date

    @property
    def cash_eur(self) -> Decimal:
        """Cash effect: negative for a buy, positive for a sale, fees included."""
        gross = self.quantity * self.price
        return (gross - self.fee if self.kind != "entry" else -gross - self.fee) / self.eur_rate


@dataclass(frozen=True)
class Account:
    cash_eur: Decimal  # settled
    unsettled_eur: Decimal
    reserved_eur: Decimal  # limit value of pending entries
    invested_eur: Decimal  # open positions marked to market
    holdings: tuple[Holding, ...]  # one per bracket with a position or a pending entry

    @property
    def equity_eur(self) -> Decimal:
        return self.cash_eur + self.unsettled_eur + self.invested_eur

    @property
    def available_eur(self) -> Decimal:
        return self.cash_eur - self.reserved_eur


def account(
    budget_eur: Decimal,
    brackets: Sequence[Bracket],
    fills: Sequence[BookFill],
    instruments: Mapping[int, Instrument],
    marks: Mapping[int, Decimal],
    rates: Mapping[str, Decimal],
    today: date,
) -> Account:
    """`brackets` and `fills` of one sleeve; `marks` are last prices, `rates` today's FX."""
    cash, unsettled = budget_eur, Decimal(0)
    for f in fills:
        if f.kind == "entry" or f.settles <= today:
            cash += f.cash_eur
        else:
            unsettled += f.cash_eur
    reserved = invested = Decimal(0)
    holdings: list[Holding] = []
    for b in brackets:
        inst = instruments[b.instrument_id]
        rate = rates[inst.currency]
        held = b.open_qty * marks.get(b.instrument_id, b.entry_price or b.entry) / rate
        pending = (b.quantity - b.filled_qty) * b.entry / rate if b.state in PENDING else Decimal(0)
        invested += held
        reserved += pending
        if held or pending:
            value = (held + pending).quantize(CENT)
            holdings.append(
                Holding(instrument_id=b.instrument_id, sector=inst.sector, value_eur=value)
            )
    return Account(
        cash_eur=cash.quantize(CENT),
        unsettled_eur=unsettled.quantize(CENT),
        reserved_eur=reserved.quantize(CENT),
        invested_eur=invested.quantize(CENT),
        holdings=tuple(holdings),
    )


def portfolio_state(
    acct: Account,
    budget_eur: Decimal,
    history: Sequence[tuple[date, Decimal]],
    today: date,
    orders_today: int,
) -> PortfolioState:
    """Day and week P&L against the last close before today / before Monday; drawdown
    against the highest equity seen (the budget at the start)."""
    equity = acct.equity_eur
    week_start = today - timedelta(days=today.weekday())
    before_today = [e for d, e in history if d < today]
    before_week = [e for d, e in history if d < week_start]
    peak = max([budget_eur, equity, *(e for _, e in history)])
    return PortfolioState(
        holdings=acct.holdings,
        settled_cash_eur=acct.available_eur,
        pnl_today_eur=equity - (before_today[-1] if before_today else budget_eur),
        pnl_week_eur=equity - (before_week[-1] if before_week else budget_eur),
        drawdown_eur=peak - equity,
        orders_today=orders_today,
    )


def trades(
    book: Book,
    brackets: Sequence[Bracket],
    fills: Sequence[BookFill],
    instruments: Mapping[int, Instrument],
    signals: Mapping[UUID, tuple[date, str]],
    sessions_between: Callable[[Market, date, date], int],
) -> list[Trade]:
    """One round trip per bracket that filled; `signals` maps bracket id to (as_of, strategy)."""
    by_bracket: dict[UUID, list[BookFill]] = defaultdict(list)
    for f in fills:
        by_bracket[f.bracket_id].append(f)
    out: list[Trade] = []
    for b in brackets:
        entries = [f for f in by_bracket[b.id] if f.kind == "entry"]
        if b.filled_qty == 0 or not entries or b.entry_price is None:
            continue
        exits = [f for f in by_bracket[b.id] if f.kind != "entry"]
        inst = instruments[b.instrument_id]
        signal_date, strategy = signals[b.id]
        first = entries[0]
        risk_eur = b.filled_qty * (b.entry - b.initial_stop) / first.eur_rate
        fees = sum((f.fee for f in by_bracket[b.id]), Decimal(0))
        fees_eur = sum((f.fee / f.eur_rate for f in by_bracket[b.id]), Decimal(0))
        closed = b.state == "closed" and bool(exits)
        pnl = sum((f.cash_eur for f in by_bracket[b.id]), Decimal(0)) if closed else None
        out.append(
            Trade(
                book=book,
                strategy=strategy,
                instrument_id=b.instrument_id,
                yahoo_symbol=inst.yahoo_symbol,
                market=inst.market,
                sector=inst.sector,
                signal_date=signal_date,
                entry_date=first.day,
                entry_price=float(b.entry_price),
                quantity=b.filled_qty,
                stop=float(b.initial_stop),
                target=float(b.target),
                risk_eur=float(risk_eur),
                fees=float(fees),
                fees_eur=float(fees_eur),
                exit_date=exits[-1].day if closed else None,
                exit_price=float(b.exit_price) if closed and b.exit_price is not None else None,
                exit_reason=b.exit_reason if closed else None,
                pnl_net_eur=float(pnl) if pnl is not None else None,
                r_multiple=float(pnl / risk_eur) if pnl is not None and risk_eur > 0 else None,
                holding_sessions=sessions_between(inst.market, first.day, exits[-1].day)
                if closed
                else None,
            )
        )
    return out
