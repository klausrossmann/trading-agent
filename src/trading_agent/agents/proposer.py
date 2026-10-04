"""Proposer: turns the module results for one candidate into a trade proposal or a pass."""

from collections.abc import Sequence
from datetime import date
from functools import cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from trading_agent.domain.analysis import ValidationIssue
from trading_agent.domain.market import Currency, Market
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.validators import check_data_gaps, check_grounded, check_long_plan
from trading_agent.modules.earnings import EarningsAssessment
from trading_agent.modules.technical import Level, PlanRules, TechnicalAssessment

NAME = "proposer"
PROMPT_VERSION = 1
FABRICATION_CAP = 0.5
TEXT_FIELDS = ("thesis", "invalidation", "no_trade_reason")


class Holding(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    market: Market
    sector: str | None


class ProposerInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    market: Market
    currency: Currency
    sector: str | None
    as_of: date
    close: float
    candidate_source: str  # the rule-based screen that picked the symbol
    atr14: float | None
    levels: list[Level]
    rules: PlanRules
    technical: TechnicalAssessment
    earnings: EarningsAssessment | None  # None: the earnings analysis failed
    earnings_event_in_window: bool | None
    next_earnings_date: date | None
    holdings: list[Holding]
    unknown: list[str]


class ProposerOutput(BaseModel):
    decision: Literal["propose", "no_trade"]
    entry_ref: str | None = Field(description="Level name for the buy limit; null for no_trade")
    stop_ref: str | None = Field(description="Level name for the stop; null for no_trade")
    target_ref: str | None = Field(description="Level name for the target; null for no_trade")
    confidence: float = Field(
        ge=0, le=1, description="Probability that the target is hit before the stop"
    )
    thesis: str = Field(description="Why this trade should work, 2-4 sentences")
    invalidation: str = Field(description="What would prove the thesis wrong, naming a level")
    no_trade_reason: str | None = Field(description="Main reason to pass; null when proposing")
    data_gaps: list[str] = Field(description="Inputs listed in `unknown`; else empty")


@cache
def proposer_output_type(names: tuple[str, ...]) -> type[ProposerOutput]:
    """Level refs restricted to this menu, so the model can't invent prices."""
    names_type = Literal.__getitem__(names)  # pyright: ignore[reportAttributeAccessIssue]
    ref = (names_type | None, Field(description="A level name from `levels`, or null"))
    return create_model(
        "ProposerOutput", __base__=ProposerOutput, entry_ref=ref, stop_ref=ref, target_ref=ref
    )


def _error(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity="error")


class ProposerAgent:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: ProposerInput) -> type[ProposerOutput]:
        return proposer_output_type(tuple(lv.name for lv in inp.levels))

    def validate(self, inp: ProposerInput, out: ProposerOutput) -> list[ValidationIssue]:
        issues = check_data_gaps(inp.unknown, out.data_gaps)
        if out.decision == "propose":
            refs = (out.entry_ref, out.stop_ref, out.target_ref)
            if any(r is None for r in refs):
                issues.append(_error("missing_plan", "propose needs entry, stop and target refs"))
            else:
                price = {lv.name: lv.price for lv in inp.levels}
                issues += check_long_plan(
                    price[str(out.entry_ref)],
                    price[str(out.stop_ref)],
                    price[str(out.target_ref)],
                    inp.atr14,
                    min_rr=inp.rules.min_risk_reward,
                    stop_atr=(inp.rules.stop_atr_min, inp.rules.stop_atr_max),
                )
            if inp.earnings is not None and inp.earnings.stance == "wait_until_after":
                issues.append(
                    _error(
                        "earnings_stance",
                        "the earnings stance is wait_until_after, so the decision must be no_trade",
                    )
                )
        elif not (out.no_trade_reason or "").strip():
            issues.append(_error("missing_reason", "no_trade needs a no_trade_reason"))
        texts = {f: str(getattr(out, f) or "") for f in TEXT_FIELDS}
        issues += check_grounded(texts, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: ProposerInput, out: ProposerOutput, issues: Sequence[ValidationIssue]
    ) -> ProposerOutput:
        if any(i.code == "ungrounded_number" for i in issues):
            return out.model_copy(update={"confidence": min(out.confidence, FABRICATION_CAP)})
        return out
