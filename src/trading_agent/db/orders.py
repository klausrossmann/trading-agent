"""Brackets, their broker orders and fills."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.market import to_instrument
from trading_agent.db.models import AuditLog, BracketRow, FillRow, InstrumentRow, OrderRow
from trading_agent.domain.orders import (
    TERMINAL,
    Bracket,
    BrokerFill,
    BrokerOrder,
    OrderKind,
    OrderSpec,
)

_BRACKET = tuple(Bracket.model_fields)
_SPEC = ("kind", "action", "order_type", "quantity", "limit_price", "stop_price", "tif")
_SPEC_MORE = ("good_till", "parent_ref", "oca_group")
_STATUS = ("status", "filled", "avg_fill_price", "broker_order_id", "perm_id")


def _bracket(row: BracketRow) -> Bracket:
    return Bracket.model_validate({k: getattr(row, k) for k in _BRACKET})


async def insert_bracket(
    session: AsyncSession,
    b: Bracket,
    specs: Sequence[OrderSpec],
    decision: dict[str, Any],
    at: datetime,
) -> bool:
    """False if the bracket exists already (a repeated submit)."""
    stmt = (
        insert(BracketRow)
        .values(**b.model_dump(), decision=decision)
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(BracketRow.id)
    )
    if (await session.execute(stmt)).scalar_one_or_none() is None:
        return False
    await add_orders(session, specs, at)
    session.add(AuditLog(actor="executor", event="bracket.approved", payload={"id": str(b.id)}))
    return True


async def add_orders(session: AsyncSession, specs: Sequence[OrderSpec], at: datetime) -> None:
    """`at` is when the order goes to the broker; the simulator only fills later sessions."""
    for spec in specs:
        stmt = insert(OrderRow).values(
            order_ref=spec.order_ref,
            bracket_id=spec.bracket_id,
            created_at=at,
            status="pending",
            **{k: getattr(spec, k) for k in (*_SPEC, *_SPEC_MORE)},
        )
        await session.execute(stmt.on_conflict_do_nothing(index_elements=["order_ref"]))


async def bracket(session: AsyncSession, bracket_id: UUID, *, lock: bool = False) -> Bracket | None:
    stmt = select(BracketRow).where(BracketRow.id == bracket_id)
    row = (await session.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    return None if row is None else _bracket(row)


async def open_brackets(session: AsyncSession, book: str) -> list[Bracket]:
    stmt = (
        select(BracketRow)
        .where(BracketRow.book == book, BracketRow.state.not_in(TERMINAL))
        .order_by(BracketRow.created_at)
    )
    return [_bracket(r) for r in await session.scalars(stmt)]


async def all_brackets(session: AsyncSession, book: str) -> list[tuple[Bracket, datetime]]:
    """Every bracket of the book with its creation time, oldest first."""
    stmt = select(BracketRow).where(BracketRow.book == book).order_by(BracketRow.created_at)
    return [(_bracket(r), r.created_at) for r in await session.scalars(stmt)]


async def book_fills(session: AsyncSession, book: str) -> list[tuple[UUID, OrderKind, BrokerFill]]:
    """(bracket id, order kind, fill) for every fill of the book, oldest first."""
    stmt = (
        select(FillRow, OrderRow.bracket_id, OrderRow.kind)
        .join(OrderRow, OrderRow.order_ref == FillRow.order_ref)
        .join(BracketRow, BracketRow.id == OrderRow.bracket_id)
        .where(BracketRow.book == book)
        .order_by(FillRow.ts, FillRow.exec_id)
    )
    return [
        (
            bracket_id,
            cast(OrderKind, kind),
            BrokerFill(
                exec_id=f.exec_id,
                order_ref=f.order_ref,
                ts=f.ts,
                quantity=f.quantity,
                price=f.price,
                commission=f.commission,
            ),
        )
        for f, bracket_id, kind in await session.execute(stmt)
    ]


async def save_bracket(session: AsyncSession, b: Bracket, event: str) -> None:
    row = await session.get(BracketRow, b.id, with_for_update=True)
    if row is None:
        raise LookupError(f"bracket {b.id} not stored")
    for k, v in b.model_dump().items():
        setattr(row, k, v)
    row.updated_at = func.now()  # pyright: ignore[reportAttributeAccessIssue]
    session.add(
        AuditLog(
            actor="executor",
            event=f"bracket.{event}",
            payload={"id": str(b.id), "state": b.state},
        )
    )


async def orders(
    session: AsyncSession, bracket_ids: Sequence[UUID] | None = None
) -> list[tuple[OrderSpec, BrokerOrder, datetime]]:
    """(spec, last known status, created_at), oldest first; without ids: open brackets only."""
    stmt = (
        select(OrderRow, InstrumentRow)
        .join(BracketRow, BracketRow.id == OrderRow.bracket_id)
        .join(InstrumentRow, InstrumentRow.id == BracketRow.instrument_id)
        .order_by(OrderRow.created_at, OrderRow.order_ref)
    )
    if bracket_ids is None:
        stmt = stmt.where(BracketRow.state.not_in(TERMINAL))
    else:
        stmt = stmt.where(OrderRow.bracket_id.in_(bracket_ids))
    out: list[tuple[OrderSpec, BrokerOrder, datetime]] = []
    for row, inst in await session.execute(stmt):
        spec = OrderSpec(
            order_ref=row.order_ref,
            bracket_id=row.bracket_id,
            instrument=to_instrument(inst),
            **{k: getattr(row, k) for k in (*_SPEC, *_SPEC_MORE)},
        )
        status = BrokerOrder.model_validate(
            {"order_ref": row.order_ref, **{k: getattr(row, k) for k in _STATUS}}
        )
        out.append((spec, status, row.created_at))
    return out


async def update_order(session: AsyncSession, o: BrokerOrder) -> None:
    row = await session.get(OrderRow, o.order_ref, with_for_update=True)
    if row is None:
        return
    changed = False
    for k in _STATUS:
        value = getattr(o, k)
        if value is not None and getattr(row, k) != value:
            setattr(row, k, value)
            changed = True
    if changed:
        row.updated_at = func.now()  # pyright: ignore[reportAttributeAccessIssue]


async def update_spec(session: AsyncSession, spec: OrderSpec) -> None:
    row = await session.get(OrderRow, spec.order_ref, with_for_update=True)
    if row is not None:
        row.limit_price, row.stop_price = spec.limit_price, spec.stop_price
        row.updated_at = func.now()  # pyright: ignore[reportAttributeAccessIssue]


async def add_fills(session: AsyncSession, fills: Sequence[BrokerFill]) -> int:
    """Fills of our orders only; stored executions are skipped. Returns the number added."""
    if not fills:
        return 0
    known = set(
        await session.scalars(
            select(OrderRow.order_ref).where(OrderRow.order_ref.in_({f.order_ref for f in fills}))
        )
    )
    rows = [f.model_dump() for f in fills if f.order_ref in known]
    if not rows:
        return 0
    stmt = insert(FillRow).values(rows).on_conflict_do_nothing(index_elements=["exec_id"])
    return len((await session.execute(stmt.returning(FillRow.exec_id))).all())
