"""Position review (M9): is the thesis of an open position still intact, given price action,
news and the next report? The answer is advice: an exit needs your confirmation."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.domain.analysis import ValidationIssue
from trading_agent.domain.news import NewsItem, Relevance
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.sanitize import untrusted
from trading_agent.llm.validators import check_grounded

NAME = "position_review"
PROMPT_VERSION = 1
TEXT_CHARS = 600


class PositionFacts(BaseModel):
    """Computed by code from the bracket and daily bars up to `as_of`."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    sector: str | None
    as_of: date
    entry_date: date
    sessions_held: int
    entry_price: float
    initial_stop: float
    stop: float
    target: float
    last_close: float
    r_now: float  # (last close - entry) / (entry - initial stop)
    closes_last_10: list[float]
    sma20: float | None
    sma50: float | None
    atr14: float | None
    next_report: date | None
    sessions_to_report: int | None


class NewsView(BaseModel):
    model_config = ConfigDict(frozen=True)

    published: datetime
    relevance: Relevance | None
    triage_note: str | None
    text: str  # headline and summary inside an <untrusted> block


class ReviewInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    position: PositionFacts
    thesis: str
    invalidation: str
    news: list[NewsView]


class ReviewOutput(BaseModel):
    thesis_intact: bool
    verdict: Literal["hold", "exit"]
    reasons: str = Field(description="1-3 sentences citing the facts")
    confidence: float = Field(ge=0, le=1)


def review_input(
    facts: PositionFacts, thesis: str, invalidation: str, news: Sequence[NewsItem]
) -> ReviewInput:
    return ReviewInput(
        position=facts,
        thesis=thesis,
        invalidation=invalidation,
        news=[
            NewsView(
                published=n.ts,
                relevance=n.relevance,
                triage_note=n.note,
                text=untrusted(n.source or "news", f"{n.headline}. {n.summary}", TEXT_CHARS),
            )
            for n in news
        ],
    )


class PositionReviewModule:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: ReviewInput) -> type[ReviewOutput]:
        return ReviewOutput

    def validate(self, inp: ReviewInput, out: ReviewOutput) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if out.thesis_intact == (out.verdict == "exit"):
            issues.append(
                ValidationIssue(
                    code="verdict",
                    message="verdict must be exit exactly when thesis_intact is false",
                    severity="error",
                )
            )
        issues += check_grounded({"reasons": out.reasons}, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: ReviewInput, out: ReviewOutput, issues: Sequence[ValidationIssue]
    ) -> ReviewOutput:
        return out
