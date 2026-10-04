"""Stored Markdown reports."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.models import ReportRow


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
