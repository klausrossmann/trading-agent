"""Critic: looks for reasons not to take a proposal (a different model vendor once keyed)."""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.agents.proposer import ProposerInput
from trading_agent.domain.analysis import ValidationIssue
from trading_agent.domain.proposals import Severity
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.validators import check_grounded

NAME = "critic"
PROMPT_VERSION = 1
SEVERITY_ORDER: tuple[Severity, ...] = ("none", "minor", "major", "blocking")


class PlanView(BaseModel):
    """The proposal as the critic sees it: refs resolved to prices by code."""

    model_config = ConfigDict(frozen=True)

    entry_ref: str
    entry: float
    stop_ref: str
    stop: float
    target_ref: str
    target: float
    risk_reward: float
    stop_atr_multiple: float | None
    confidence: float
    thesis: str
    invalidation: str


class CriticInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    facts: ProposerInput
    proposal: PlanView


class Objection(BaseModel):
    point: str = Field(description="One concrete problem, citing the facts")
    severity: Literal["minor", "major", "blocking"]


class CriticVerdict(BaseModel):
    objections: list[Objection] = Field(description="Most important first; empty if none")
    severity: Severity = Field(description="The highest objection severity, or none")
    confidence_delta: float = Field(
        ge=-0.5, le=0.1, description="Adjustment to the proposer's confidence"
    )
    summary: str = Field(description="Verdict in 1-2 sentences")


def max_severity(objections: Sequence[Objection]) -> Severity:
    if not objections:
        return "none"
    return max(objections, key=lambda o: SEVERITY_ORDER.index(o.severity)).severity


class CriticAgent:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: CriticInput) -> type[CriticVerdict]:
        return CriticVerdict

    def validate(self, inp: CriticInput, out: CriticVerdict) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        expected = max_severity(out.objections)
        if out.severity != expected:
            issues.append(
                ValidationIssue(
                    code="severity",
                    message=f"severity must be the highest objection severity: {expected}",
                    severity="error",
                )
            )
        texts = {"summary": out.summary} | {
            f"objections[{i}]": o.point for i, o in enumerate(out.objections)
        }
        issues += check_grounded(texts, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: CriticInput, out: CriticVerdict, issues: Sequence[ValidationIssue]
    ) -> CriticVerdict:
        return out
