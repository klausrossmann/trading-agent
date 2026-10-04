"""Earnings module: next report vs the holding window, beat/miss record and past reactions."""

import math
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from trading_agent.calc.indicators import atr
from trading_agent.calc.trend import post_earnings_moves
from trading_agent.data import calendars
from trading_agent.domain.analysis import ValidationIssue
from trading_agent.domain.market import EarningsEvent, EarningsTiming, Instrument, Market
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.validators import check_data_gaps, check_grounded

NAME = "earnings"
PROMPT_VERSION = 1
HISTORY_EVENTS = 8
FABRICATION_CAP = 0.5
TEXT_FIELDS = ("summary", "track_record", "reaction_pattern", "bull_case", "bear_case")


def _num(value: float | None, places: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, places)


class NextReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    timing: EarningsTiming
    sessions_until: int  # sessions after as_of up to and including the report date
    eps_estimate: float | None


class PastReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    timing: EarningsTiming
    eps_estimate: float | None
    eps_actual: float | None
    surprise_pct: float | None
    gap_pct: float | None
    move_pct: float | None


class EarningsInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    market: Market
    sector: str | None
    as_of: date
    holding_window_sessions: int
    next_report: NextReport | None
    event_in_window: bool
    history: list[PastReport]  # newest first
    stats: dict[str, float | int | None]
    unknown: list[str]


def _reported_by(event: EarningsEvent, as_of: date) -> bool:
    """True if the market had reacted to the report by the close of `as_of`."""
    if event.date < as_of:
        return True
    return event.date == as_of and event.timing in ("bmo", "during")


def compute(
    instrument: Instrument,
    frame: pd.DataFrame,
    events: Sequence[EarningsEvent],
    holding_sessions: int,
) -> EarningsInput:
    as_of = pd.Timestamp(frame.index[-1]).date()
    cal = calendars.CALENDAR_BY_MARKET[instrument.market]
    past = sorted((e for e in events if _reported_by(e, as_of)), key=lambda e: e.date)
    upcoming = sorted((e for e in events if not _reported_by(e, as_of)), key=lambda e: e.date)

    next_report: NextReport | None = None
    in_window = False
    if upcoming:
        e = upcoming[0]
        until = len(calendars.sessions(cal, as_of + timedelta(days=1), e.date))
        next_report = NextReport(
            date=e.date,
            timing=e.timing,
            sessions_until=until,
            eps_estimate=_num(float(e.eps_estimate)) if e.eps_estimate is not None else None,
        )
        # After-close or unknown timing: the price reacts one session later.
        reaction = until + (0 if e.timing in ("bmo", "during") else 1)
        in_window = reaction <= holding_sessions

    recent = past[-HISTORY_EVENTS:]
    moves = {m.event_date: m for m in post_earnings_moves(frame, recent)}
    history: list[PastReport] = []
    for e in reversed(recent):
        est = float(e.eps_estimate) if e.eps_estimate is not None else None
        act = float(e.eps_actual) if e.eps_actual is not None else None
        surprise = (act - est) / abs(est) * 100 if est and act is not None else None
        move = moves.get(e.date)
        history.append(
            PastReport(
                date=e.date,
                timing=e.timing,
                eps_estimate=_num(est),
                eps_actual=_num(act),
                surprise_pct=_num(surprise, 1),
                gap_pct=_num(move.gap_pct) if move else None,
                move_pct=_num(move.move_pct) if move else None,
            )
        )

    both = [h for h in history if h.eps_estimate is not None and h.eps_actual is not None]
    abs_moves = [abs(h.move_pct) for h in history if h.move_pct is not None]
    last = float(frame["close"].iloc[-1])
    atr_now = float(atr(frame).iloc[-1])
    stats: dict[str, float | int | None] = {
        "reports_with_estimate": len(both),
        "beats": sum(1 for h in both if (h.eps_actual or 0) > (h.eps_estimate or 0)),
        "misses": sum(1 for h in both if (h.eps_actual or 0) < (h.eps_estimate or 0)),
        "avg_abs_move_pct": _num(sum(abs_moves) / len(abs_moves)) if abs_moves else None,
        "max_abs_move_pct": _num(max(abs_moves)) if abs_moves else None,
        "atr_pct": _num(atr_now / last * 100),
    }

    unknown: list[str] = []
    if next_report is None:
        unknown.append("next_report")
    else:
        if next_report.timing == "unknown":
            unknown.append("next_report.timing")
        if next_report.eps_estimate is None:
            unknown.append("next_report.eps_estimate")
    if not history:
        unknown.append("history")
    unknown += [f"stats.{k}" for k, v in stats.items() if v is None]

    return EarningsInput(
        symbol=instrument.yahoo_symbol,
        name=instrument.name,
        market=instrument.market,
        sector=instrument.sector,
        as_of=as_of,
        holding_window_sessions=holding_sessions,
        next_report=next_report,
        event_in_window=in_window,
        history=history,
        stats=stats,
        unknown=sorted(unknown),
    )


Stance = Literal["no_event_in_window", "hold_through", "exit_before", "wait_until_after"]


class EarningsAssessment(BaseModel):
    summary: str = Field(description="The decision and its main reason, 1-2 sentences")
    track_record: str = Field(description="Beats and misses vs the EPS estimate")
    reaction_pattern: str = Field(description="How the price reacted to past reports")
    event_risk: Literal["none", "low", "medium", "high"]
    stance: Stance
    bull_case: str
    bear_case: str
    confidence: float = Field(ge=0, le=1)
    data_gaps: list[str] = Field(description="Inputs listed in `unknown`; else empty")


class EarningsModule:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: EarningsInput) -> type[EarningsAssessment]:
        return EarningsAssessment

    def validate(self, inp: EarningsInput, out: EarningsAssessment) -> list[ValidationIssue]:
        issues = check_data_gaps(inp.unknown, out.data_gaps)
        if inp.event_in_window == (out.stance == "no_event_in_window"):
            expected = "one of hold_through, exit_before, wait_until_after"
            if not inp.event_in_window:
                expected = "no_event_in_window"
            issues.append(
                ValidationIssue(
                    code="stance",
                    message=f"event_in_window is {str(inp.event_in_window).lower()}, so stance "
                    f"must be {expected}",
                    severity="error",
                )
            )
        if inp.event_in_window and out.event_risk == "none":
            issues.append(
                ValidationIssue(
                    code="event_risk",
                    message="a report falls in the holding window, so event_risk can't be none",
                    severity="error",
                )
            )
        texts = {f: str(getattr(out, f)) for f in TEXT_FIELDS}
        issues += check_grounded(texts, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: EarningsInput, out: EarningsAssessment, issues: Sequence[ValidationIssue]
    ) -> EarningsAssessment:
        if any(i.code == "ungrounded_number" for i in issues):
            return out.model_copy(update={"confidence": min(out.confidence, FABRICATION_CAP)})
        return out
