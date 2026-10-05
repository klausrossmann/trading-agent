"""Company news and its triage."""

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.models import NewsRow
from trading_agent.domain.news import NewsItem, Relevance

_FIELDS = ("instrument_id", "ts", "headline", "summary", "source", "url")


def _item(r: NewsRow) -> NewsItem:
    return NewsItem(
        id=r.id,
        instrument_id=r.instrument_id,
        ts=r.ts,
        headline=r.headline,
        summary=r.summary,
        source=r.source,
        url=r.url,
        relevance=cast(Relevance | None, r.relevance),
        note=r.note,
    )


async def add_news(session: AsyncSession, items: Sequence[NewsItem]) -> int:
    """Stores items not seen before; returns how many were new."""
    if not items:
        return 0
    rows = [{"id": i.id, **{k: getattr(i, k) for k in _FIELDS}} for i in items]
    stmt = insert(NewsRow).values(rows).on_conflict_do_nothing(index_elements=["id"])
    return len((await session.execute(stmt.returning(NewsRow.id))).all())


async def untriaged(session: AsyncSession, instrument_ids: Sequence[int]) -> list[NewsItem]:
    stmt = (
        select(NewsRow)
        .where(NewsRow.instrument_id.in_(instrument_ids), NewsRow.relevance.is_(None))
        .order_by(NewsRow.instrument_id, NewsRow.ts)
    )
    return [_item(r) for r in await session.scalars(stmt)]


async def set_triage(session: AsyncSession, news_id: str, relevance: Relevance, note: str) -> None:
    row = await session.get(NewsRow, news_id)
    if row is not None:
        row.relevance, row.note = relevance, note


async def recent(session: AsyncSession, instrument_id: int, since: datetime) -> list[NewsItem]:
    """Triaged items since `since`, newest first."""
    stmt = (
        select(NewsRow)
        .where(
            NewsRow.instrument_id == instrument_id,
            NewsRow.ts >= since,
            NewsRow.relevance.is_not(None),
        )
        .order_by(NewsRow.ts.desc())
    )
    return [_item(r) for r in await session.scalars(stmt)]
