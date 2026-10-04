"""Risk decisions per proposal and daily equity per book sleeve."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.models import EquityDailyRow, RiskDecisionRow
from trading_agent.domain.risk import Check, RiskDecision


async def save_decision(
    session: AsyncSession, proposal_id: UUID, decision: RiskDecision, ts: datetime
) -> None:
    values = {
        "ts": ts,
        "approved": decision.approved,
        "quantity": decision.quantity,
        "trip": decision.trip,
        "checks": [c.model_dump() for c in decision.checks],
    }
    stmt = insert(RiskDecisionRow).values(proposal_id=proposal_id, **values)
    await session.execute(stmt.on_conflict_do_update(index_elements=["proposal_id"], set_=values))


async def decisions(
    session: AsyncSession, proposal_ids: Sequence[UUID]
) -> dict[UUID, RiskDecision]:
    """Stored decisions (checks, approval, quantity; prices live in the bracket)."""
    stmt = select(RiskDecisionRow).where(RiskDecisionRow.proposal_id.in_(proposal_ids))
    return {
        r.proposal_id: RiskDecision.model_validate(
            {
                "approved": r.approved,
                "quantity": r.quantity,
                "trip": r.trip,
                "checks": [Check.model_validate(c) for c in r.checks],
            }
        )
        for r in await session.scalars(stmt)
    }


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
