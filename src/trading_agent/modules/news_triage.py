"""News triage (M9): which new headlines matter for an open position's thesis. Cheap model."""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.domain.analysis import ValidationIssue
from trading_agent.domain.news import NewsItem
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.sanitize import untrusted
from trading_agent.llm.validators import check_grounded

NAME = "news_triage"
PROMPT_VERSION = 1
TEXT_CHARS = 600


class Headline(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str
    published: datetime
    text: str  # headline and summary inside an <untrusted> block


class TriageInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    sector: str | None
    thesis: str
    invalidation: str
    items: list[Headline]


class TriageItem(BaseModel):
    ref: str = Field(description="The item's ref from the input")
    relevance: Literal["none", "low", "high"]
    note: str = Field(description="Why, in one sentence")


class TriageOutput(BaseModel):
    items: list[TriageItem] = Field(description="Every input item exactly once")


def triage_input(
    symbol: str, sector: str | None, thesis: str, invalidation: str, news: Sequence[NewsItem]
) -> TriageInput:
    return TriageInput(
        symbol=symbol,
        sector=sector,
        thesis=thesis,
        invalidation=invalidation,
        items=[
            Headline(
                ref=f"n{i}",
                published=n.ts,
                text=untrusted(n.source or "news", f"{n.headline}. {n.summary}", TEXT_CHARS),
            )
            for i, n in enumerate(news, start=1)
        ],
    )


class NewsTriageModule:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: TriageInput) -> type[TriageOutput]:
        return TriageOutput

    def validate(self, inp: TriageInput, out: TriageOutput) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        expected = [h.ref for h in inp.items]
        got = [i.ref for i in out.items]
        if sorted(got) != sorted(expected):
            issues.append(
                ValidationIssue(
                    code="refs",
                    message=f"answer every item exactly once: {', '.join(expected)}",
                    severity="error",
                )
            )
        texts = {f"items[{i}].note": t.note for i, t in enumerate(out.items)}
        issues += check_grounded(texts, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: TriageInput, out: TriageOutput, issues: Sequence[ValidationIssue]
    ) -> TriageOutput:
        return out
