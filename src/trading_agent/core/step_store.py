"""The step log (domain/steps.py)."""

from collections.abc import Collection, Sequence
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core.steps import Step, StepName
from trading_agent.core.tables import StepRow
from trading_agent.marketdata.types import Market

_FIELDS = tuple(Step.model_fields)


def _step(r: StepRow) -> Step:
    return Step.model_validate({k: getattr(r, k) for k in _FIELDS})


async def add_steps(session: AsyncSession, steps: Sequence[Step]) -> None:
    session.add_all(
        StepRow(
            **s.model_dump(mode="json", exclude={"ts", "day", "proposal_id"}),
            day=s.day,
            proposal_id=s.proposal_id,
        )
        for s in steps
    )


async def replace_steps(
    session: AsyncSession, day: date, names: Collection[StepName], steps: Sequence[Step]
) -> None:
    """A re-run of a scan replaces that day's earlier steps of the same stocks."""
    symbols = {s.symbol for s in steps}
    if symbols:
        await session.execute(
            delete(StepRow).where(
                StepRow.day == day, StepRow.step.in_(names), StepRow.symbol.in_(symbols)
            )
        )
    await add_steps(session, steps)


async def steps(
    session: AsyncSession, day: date | None = None, symbol: str | None = None
) -> list[Step]:
    stmt = select(StepRow).order_by(StepRow.ts, StepRow.id)
    if day is not None:
        stmt = stmt.where(StepRow.day == day)
    if symbol is not None:
        stmt = stmt.where(StepRow.symbol == symbol)
    return [_step(r) for r in await session.scalars(stmt)]


async def news_signals(session: AsyncSession, market: Market | None, since: datetime) -> list[Step]:
    """The News agent's buy signals since `since` that passed the threshold, oldest first."""
    stmt = (
        select(StepRow)
        .where(
            StepRow.step == "news",
            StepRow.status == "passed",
            StepRow.output["action"].astext == "buy",
            StepRow.ts >= since,
        )
        .order_by(StepRow.ts, StepRow.id)
    )
    if market is not None:
        stmt = stmt.where(StepRow.market == market)
    return [_step(r) for r in await session.scalars(stmt)]
