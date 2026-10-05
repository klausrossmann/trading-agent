"""Proposals and user labels."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.models import InstrumentRow, ProposalRow, UserLabelRow
from trading_agent.domain.numbers import optional_decimal as _dec
from trading_agent.domain.proposals import Label, Proposal

_STORED = (
    "status",
    "strategy",
    "entry_ref",
    "stop_ref",
    "target_ref",
    "thesis",
    "invalidation",
    "critic_severity",
    "critic_summary",
    "rank",
    "analyses",
    "payload",
)


async def save_proposals(session: AsyncSession, proposals: Sequence[Proposal]) -> list[UUID]:
    """Upsert per (source, instrument, as_of): a re-run of the same day keeps the id and labels."""
    ids: list[UUID] = []
    for p in proposals:
        values = {k: getattr(p, k) for k in _STORED} | {
            "entry": _dec(p.entry, 4),
            "stop": _dec(p.stop, 4),
            "target": _dec(p.target, 4),
            "confidence": _dec(p.confidence, 4),
        }
        stmt = insert(ProposalRow).values(
            id=p.id, source=p.source, as_of=p.as_of, instrument_id=p.instrument_id, **values
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_proposals_source_day", set_=values | {"updated_at": func.now()}
        ).returning(ProposalRow.id)
        ids.append((await session.execute(stmt)).scalar_one())
    return ids


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _to_domain(row: ProposalRow, inst: InstrumentRow) -> Proposal:
    return Proposal(
        id=row.id,
        source=row.source,  # pyright: ignore[reportArgumentType]
        as_of=row.as_of,
        instrument_id=row.instrument_id,
        yahoo_symbol=inst.yahoo_symbol,
        market=inst.market,  # pyright: ignore[reportArgumentType]
        sector=inst.sector,
        status=row.status,  # pyright: ignore[reportArgumentType]
        strategy=row.strategy,
        entry_ref=row.entry_ref,
        stop_ref=row.stop_ref,
        target_ref=row.target_ref,
        entry=_float(row.entry),
        stop=_float(row.stop),
        target=_float(row.target),
        confidence=_float(row.confidence),
        rank=row.rank,
        thesis=row.thesis,
        invalidation=row.invalidation,
        critic_severity=row.critic_severity,  # pyright: ignore[reportArgumentType]
        critic_summary=row.critic_summary,
        analyses=row.analyses,
        payload=row.payload,
    )


async def _select(session: AsyncSession, *where: ColumnElement[bool]) -> list[Proposal]:
    stmt = (
        select(ProposalRow, InstrumentRow)
        .join(InstrumentRow, InstrumentRow.id == ProposalRow.instrument_id)
        .where(*where)
        .order_by(ProposalRow.as_of, ProposalRow.rank.nulls_last(), InstrumentRow.yahoo_symbol)
    )
    return [_to_domain(p, i) for p, i in await session.execute(stmt)]


async def proposals(
    session: AsyncSession, *, start: date | None = None, end: date | None = None
) -> list[Proposal]:
    where: list[ColumnElement[bool]] = []
    if start is not None:
        where.append(ProposalRow.as_of >= start)
    if end is not None:
        where.append(ProposalRow.as_of <= end)
    return await _select(session, *where)


async def latest_as_of(session: AsyncSession) -> date | None:
    return await session.scalar(select(func.max(ProposalRow.as_of)))


async def latest_for_symbol(session: AsyncSession, yahoo_symbol: str) -> Proposal | None:
    found = await _select(session, func.upper(InstrumentRow.yahoo_symbol) == yahoo_symbol.upper())
    return found[-1] if found else None


async def get(session: AsyncSession, proposal_id: UUID) -> Proposal | None:
    found = await _select(session, ProposalRow.id == proposal_id)
    return found[0] if found else None


async def by_ids(session: AsyncSession, ids: Sequence[UUID]) -> dict[UUID, Proposal]:
    return {p.id: p for p in await _select(session, ProposalRow.id.in_(ids))}


async def set_label(
    session: AsyncSession, proposal_id: UUID, label: Label, reason: str | None = None
) -> None:
    stmt = insert(UserLabelRow).values(proposal_id=proposal_id, label=label, reason=reason)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[UserLabelRow.proposal_id],
            set_={"label": label, "reason": reason, "ts": func.now()},
        )
    )


async def set_reason(session: AsyncSession, proposal_id: UUID, reason: str) -> bool:
    row = await session.get(UserLabelRow, proposal_id)
    if row is None:
        return False
    row.reason = reason
    return True


async def labels(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, tuple[Label, str | None]]:
    stmt = select(UserLabelRow).where(UserLabelRow.proposal_id.in_(ids))
    return {r.proposal_id: (cast(Label, r.label), r.reason) for r in await session.scalars(stmt)}
