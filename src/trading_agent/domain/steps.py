"""The step log: what happened to one stock on one day, phase by phase, with inputs, outputs
and errors. The dashboard's day view reads it."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading_agent.domain.market import Market

StepName = Literal[
    "news",
    "screen",
    "technical",
    "earnings",
    "proposer",
    "critic",
    "rank",
    "risk",
    "order",
    "broker",
    "fill",
    "stop",
    "exit",
    "cancel",
]
StepStatus = Literal["passed", "stopped", "error", "info"]
SCAN_STEPS: tuple[StepName, ...] = ("screen", "technical", "earnings", "proposer", "critic", "rank")


class Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date  # Berlin date the step ran
    market: Market
    instrument_id: int | None
    symbol: str
    step: StepName
    status: StepStatus
    summary: str
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    analysis_id: int | None = None
    proposal_id: UUID | None = None
    order_ref: str | None = None
    ts: datetime | None = None  # set by the database
