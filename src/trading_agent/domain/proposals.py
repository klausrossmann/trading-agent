"""Trade proposals from the agent pipeline and your labels on them (IMPLEMENTATION.md 4, 8)."""

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading_agent.domain.market import Market

# no_trade: the proposer passed; blocked: the critic found a blocking objection;
# proposed: survived the critic and goes to the book (risk engine from M8).
ProposalStatus = Literal["proposed", "blocked", "no_trade"]
Severity = Literal["none", "minor", "major", "blocking"]
Label = Literal["agree", "disagree"]


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    source: Literal["agent"]
    as_of: date  # last bar the analysis saw; the limit order would be placed the next session
    instrument_id: int
    yahoo_symbol: str
    market: Market
    sector: str | None
    status: ProposalStatus
    strategy: str
    entry_ref: str | None = None
    stop_ref: str | None = None
    target_ref: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    confidence: float | None = None  # proposer confidence adjusted by the critic
    rank: int | None = None  # portfolio manager, 1 = best, among `proposed`
    thesis: str
    invalidation: str
    critic_severity: Severity | None = None
    critic_summary: str | None = None
    analyses: dict[str, int] = {}  # module -> analyses.id, the evidence trail
    payload: dict[str, Any] = {}  # full agent outputs

    @property
    def risk_reward(self) -> float | None:
        if self.entry is None or self.stop is None or self.target is None:
            return None
        risk = self.entry - self.stop
        return (self.target - self.entry) / risk if risk > 0 else None
