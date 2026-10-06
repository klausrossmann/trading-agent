"""Analyses (module outputs, the LLM cache) and LLM calls (cost and audit)."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_agent.db.models import AnalysisRow, LlmCallRow
from trading_agent.domain.analysis import (
    AnalysisRecord,
    LlmCall,
    StoredAnalysis,
    ValidationIssue,
)
from trading_agent.domain.numbers import to_decimal


class DbAnalysisStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def cached(self, input_hash: str) -> StoredAnalysis | None:
        async with self._sessions() as s:
            row = await s.scalar(select(AnalysisRow).where(AnalysisRow.input_hash == input_hash))
        if row is None:
            return None
        return StoredAnalysis(
            id=row.id,
            output=row.output,
            issues=tuple(ValidationIssue.model_validate(i) for i in row.issues),
            status=row.status,  # pyright: ignore[reportArgumentType]  # written by save()
            trace_id=row.trace_id,
        )

    async def save(self, analysis: AnalysisRecord | None, calls: Sequence[LlmCall]) -> int | None:
        async with self._sessions.begin() as s:
            analysis_id: int | None = None
            if analysis is not None:
                row = AnalysisRow(
                    module=analysis.module,
                    prompt_version=analysis.prompt_version,
                    model=analysis.model,
                    instrument_id=analysis.instrument_id,
                    as_of=analysis.as_of,
                    input_hash=analysis.input_hash,
                    input=analysis.input,
                    output=analysis.output,
                    issues=[i.model_dump() for i in analysis.issues],
                    status=analysis.status,
                    cost_usd=to_decimal(analysis.cost_usd, 6),
                    trace_id=analysis.trace_id,
                )
                s.add(row)
                await s.flush()
                analysis_id = row.id
            s.add_all(
                LlmCallRow(
                    **c.model_dump(exclude={"cost_usd"}),
                    cost_usd=to_decimal(c.cost_usd, 6),
                    analysis_id=analysis_id,
                )
                for c in calls
            )
        return analysis_id

    async def trace_id(self, analysis_id: int) -> str | None:
        async with self._sessions() as s:
            return await s.scalar(select(AnalysisRow.trace_id).where(AnalysisRow.id == analysis_id))

    async def spent_usd(self, since: datetime) -> float:
        async with self._sessions() as s:
            total = await s.scalar(
                select(func.coalesce(func.sum(LlmCallRow.cost_usd), 0)).where(
                    LlmCallRow.ts >= since
                )
            )
        return float(total or 0)

    async def spend_by_model(self, since: datetime) -> list[tuple[str, int, float]]:
        """(model, calls, USD) since `since`, most expensive first."""
        stmt = (
            select(LlmCallRow.model, func.count(), func.sum(LlmCallRow.cost_usd))
            .where(LlmCallRow.ts >= since)
            .group_by(LlmCallRow.model)
            .order_by(func.sum(LlmCallRow.cost_usd).desc())
        )
        async with self._sessions() as s:
            return [(m, int(n), float(c or 0)) for m, n, c in await s.execute(stmt)]
