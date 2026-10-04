from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.models import InstrumentRow, TradeRow
from trading_agent.domain.trading import Book, Trade


def _dec(value: float | None, places: int) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, places)))


async def replace_book(session: AsyncSession, book: Book, trades: Sequence[Trade]) -> int:
    """Replace all trades of a simulated book (it is recomputed from scratch each run)."""
    await session.execute(delete(TradeRow).where(TradeRow.book == book))
    session.add_all(
        TradeRow(
            book=book,
            strategy=t.strategy,
            instrument_id=t.instrument_id,
            signal_date=t.signal_date,
            entry_date=t.entry_date,
            entry_price=_dec(t.entry_price, 4),
            quantity=t.quantity,
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
            quantity=t.quantity,
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
