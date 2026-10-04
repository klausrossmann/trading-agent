"""Portfolio manager: ranks the proposals that survived the critic against what's held."""

from collections import Counter
from collections.abc import Sequence
from datetime import date
from functools import cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from trading_agent.agents.proposer import Holding
from trading_agent.domain.analysis import ValidationIssue
from trading_agent.domain.market import Market
from trading_agent.domain.proposals import Severity
from trading_agent.llm.budget import BudgetMode
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.validators import check_grounded

NAME = "portfolio_manager"
PROMPT_VERSION = 1


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    market: Market
    sector: str | None
    confidence: float
    risk_reward: float
    thesis: str
    critic_severity: Severity
    critic_summary: str


class PortfolioInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: date
    candidates: list[Candidate]
    holdings: list[Holding]
    free_slots: int
    budget_mode: BudgetMode


class RankedItem(BaseModel):
    symbol: str
    note: str = Field(description="Why it sits at this rank, one sentence")


class Ranking(BaseModel):
    ranking: list[RankedItem] = Field(description="Every candidate exactly once, best first")
    rationale: str = Field(description="How the ranking fits the portfolio, 1-3 sentences")


@cache
def ranking_type(symbols: tuple[str, ...]) -> type[Ranking]:
    symbol_type = Literal.__getitem__(symbols)  # pyright: ignore[reportAttributeAccessIssue]
    item = create_model("RankedItem", __base__=RankedItem, symbol=(symbol_type, ...))
    return create_model("Ranking", __base__=Ranking, ranking=(list[item], ...))


class PortfolioManagerAgent:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: PortfolioInput) -> type[Ranking]:
        return ranking_type(tuple(c.symbol for c in inp.candidates))

    def validate(self, inp: PortfolioInput, out: Ranking) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        counts = Counter(r.symbol for r in out.ranking)
        missing = sorted({c.symbol for c in inp.candidates} - set(counts))
        repeated = sorted(s for s, n in counts.items() if n > 1)
        if missing or repeated:
            issues.append(
                ValidationIssue(
                    code="ranking",
                    message="rank every candidate exactly once; "
                    f"missing: {', '.join(missing) or '-'}, repeated: {', '.join(repeated) or '-'}",
                    severity="error",
                )
            )
        texts = {"rationale": out.rationale} | {r.symbol: r.note for r in out.ranking}
        issues += check_grounded(texts, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: PortfolioInput, out: Ranking, issues: Sequence[ValidationIssue]
    ) -> Ranking:
        return out
