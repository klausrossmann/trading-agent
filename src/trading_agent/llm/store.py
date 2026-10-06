"""In-memory analysis store for tests and evals (the agent uses `db.analyses.DbAnalysisStore`)."""

from collections.abc import Sequence
from datetime import datetime

from trading_agent.domain.analysis import AnalysisRecord, LlmCall, StoredAnalysis


class MemoryStore:
    def __init__(self, spent_usd: float = 0.0) -> None:
        self.analyses: dict[int, AnalysisRecord] = {}
        self.calls: list[LlmCall] = []
        self._spent_before = spent_usd

    async def cached(self, input_hash: str) -> StoredAnalysis | None:
        for i, a in self.analyses.items():
            if a.input_hash == input_hash:
                return StoredAnalysis(
                    id=i, output=a.output, issues=a.issues, status=a.status, trace_id=a.trace_id
                )
        return None

    async def save(self, analysis: AnalysisRecord | None, calls: Sequence[LlmCall]) -> int | None:
        self.calls.extend(calls)
        if analysis is None:
            return None
        analysis_id = len(self.analyses) + 1
        self.analyses[analysis_id] = analysis
        return analysis_id

    async def spent_usd(self, since: datetime) -> float:
        return self._spent_before + sum(c.cost_usd for c in self.calls)
