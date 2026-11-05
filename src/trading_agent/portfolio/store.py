"""The book's round trips, its daily equity and the stored reports (tax)."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core.numbers import optional_decimal as _dec
from trading_agent.core.tables import EquityDailyRow, InstrumentRow, ReportRow, TradeRow
from trading_agent.portfolio.types import Book, Trade


async def replace_book(session: AsyncSession, book: Book, trades: Sequence[Trade]) -> int:
    """Replace all trades of a book (it is rebuilt from its brackets and fills each evening)."""
    await session.execute(delete(TradeRow).where(TradeRow.book == book))
    session.add_all(
        TradeRow(
            book=book,
            strategy=t.strategy,
            instrument_id=t.instrument_id,
            signal_date=t.signal_date,
            entry_date=t.entry_date,
            entry_price=_dec(t.entry_price, 4),
            quantity=_dec(t.quantity, 4),
            stop=_dec(t.stop, 4),
            target=_dec(t.target, 4),
            risk_eur=_dec(t.risk_eur, 2),
            fees=_dec(t.fees, 2),
            fees_eur=_dec(t.fees_eur, 2),
            exit_date=t.exit_date,
            exit_price=_dec(t.exit_price, 4),
            exit_reason=t.exit_reason,
            pnl_net_eur=_dec(t.pnl_net_eur, 2),
            r_multiple=_dec(t.r_multiple, 3),
            holding_sessions=t.holding_sessions,
        )
        for t in trades
    )
    await session.flush()
    return len(trades)


async def book_trades(session: AsyncSession, book: Book) -> list[TradeRow]:
    stmt = select(TradeRow).where(TradeRow.book == book).order_by(TradeRow.entry_date)
    return list(await session.scalars(stmt))


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


async def trades(session: AsyncSession, book: Book) -> list[Trade]:
    """The book's round trips as domain objects, oldest entry first."""
    stmt = (
        select(TradeRow, InstrumentRow)
        .join(InstrumentRow, InstrumentRow.id == TradeRow.instrument_id)
        .where(TradeRow.book == book)
        .order_by(TradeRow.entry_date, TradeRow.id)
    )
    return [
        Trade(
            book=book,
            strategy=t.strategy,
            instrument_id=t.instrument_id,
            yahoo_symbol=i.yahoo_symbol,
            market=i.market,  # pyright: ignore[reportArgumentType]
            sector=i.sector,
            signal_date=t.signal_date,
            entry_date=t.entry_date,
            entry_price=float(t.entry_price),
            quantity=float(t.quantity),
            stop=float(t.stop),
            target=float(t.target),
            risk_eur=float(t.risk_eur),
            fees=float(t.fees),
            fees_eur=float(t.fees_eur),
            exit_date=t.exit_date,
            exit_price=_float(t.exit_price),
            exit_reason=t.exit_reason,
            pnl_net_eur=_float(t.pnl_net_eur),
            r_multiple=_float(t.r_multiple),
            holding_sessions=t.holding_sessions,
        )
        for t, i in await session.execute(stmt)
    ]


async def upsert_equity(
    session: AsyncSession,
    book: str,
    sleeve: str,
    day: date,
    *,
    equity_eur: Decimal,
    cash_eur: Decimal,
    invested_eur: Decimal,
) -> None:
    values = {"equity_eur": equity_eur, "cash_eur": cash_eur, "invested_eur": invested_eur}
    stmt = insert(EquityDailyRow).values(book=book, sleeve=sleeve, date=day, **values)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["book", "sleeve", "date"], set_=values)
    )


async def equity_history(
    session: AsyncSession, book: str, sleeve: str
) -> list[tuple[date, Decimal]]:
    stmt = (
        select(EquityDailyRow.date, EquityDailyRow.equity_eur)
        .where(EquityDailyRow.book == book, EquityDailyRow.sleeve == sleeve)
        .order_by(EquityDailyRow.date)
    )
    return [(d, e) for d, e in await session.execute(stmt)]


async def save_report(session: AsyncSession, kind: str, period_end: date, body: str) -> None:
    stmt = insert(ReportRow).values(kind=kind, period_end=period_end, body=body)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["kind", "period_end"], set_={"body": body, "created_at": func.now()}
        )
    )


async def reports(session: AsyncSession, kind: str) -> list[tuple[date, str]]:
    """(period end, body), newest first."""
    stmt = (
        select(ReportRow.period_end, ReportRow.body)
        .where(ReportRow.kind == kind)
        .order_by(ReportRow.period_end.desc())
    )
    return [(d, b) for d, b in await session.execute(stmt)]
