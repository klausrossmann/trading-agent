"""The analyst stage: technical and earnings analyst per candidate, and their steps."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog

from trading_agent.analysts import earnings as earnings_module
from trading_agent.analysts import technical as technical_module
from trading_agent.analysts.earnings import EarningsAssessment, EarningsInput
from trading_agent.analysts.history import load_history
from trading_agent.analysts.technical import TechnicalAssessment, TechnicalInput
from trading_agent.contexts import AnalysisContext
from trading_agent.core.steps import Step, StepName, StepStatus
from trading_agent.llm.budget import BudgetMode
from trading_agent.llm.runner import Outcome
from trading_agent.marketdata import store as market_repo
from trading_agent.marketdata.types import Market
from trading_agent.screen import stage as screen_stage

log = structlog.get_logger(__name__)

BUY_RATINGS = ("buy", "strong_buy")


@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    instrument_id: int
    technical_input: TechnicalInput
    technical: Outcome[TechnicalAssessment]
    earnings_input: EarningsInput
    earnings: Outcome[EarningsAssessment]

    @property
    def outcomes(self) -> tuple[Outcome[Any], ...]:
        return (self.technical, self.earnings)


@dataclass(frozen=True)
class ScanResult:
    mode: BudgetMode
    items: list[SymbolAnalysis]
    missing: list[str]  # requested symbols without stored bars

    @property
    def cost_usd(self) -> float:
        return sum(o.cost_usd for i in self.items for o in i.outcomes)


async def scan_size(ctx: AnalysisContext) -> int:
    scan = ctx.models.scan
    return scan.candidates_lean if await ctx.runner.mode() == "lean" else scan.candidates


async def analyse(
    ctx: AnalysisContext,
    symbols: list[str] | None = None,
    market: Market | None = None,
    top: int | None = None,
    today: date | None = None,
) -> ScanResult:
    """Technical and earnings analysis for the given symbols, or for today's top setups."""
    mode = await ctx.runner.mode()
    if symbols is None:
        found = await screen_stage.screen_stocks(ctx, market, top or await scan_size(ctx))
        symbols = [r.symbol for r in found if r.passed]
    async with ctx.sessions() as s:
        instruments = await market_repo.active_instruments(s)
    by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
    today = today or datetime.now(UTC).date()
    items: list[SymbolAnalysis] = []
    missing: list[str] = []
    for symbol in symbols:
        inst_id = by_symbol.get(symbol)
        history = (
            await load_history(
                ctx.sessions, inst_id, today, ctx.benchmarks.get(instruments[inst_id].market)
            )
            if inst_id is not None
            else None
        )
        if history is None:
            missing.append(symbol)
            continue
        t_in = technical_module.compute(
            history.instrument, history.frame, history.benchmark_close, ctx.rules
        )
        t_out = await ctx.runner.run(
            ctx.technical,
            t_in,
            instrument_id=history.instrument_id,
            as_of=history.as_of,
            subject=symbol,
        )
        e_in = earnings_module.compute(
            history.instrument, history.frame, history.events, ctx.holding_sessions
        )
        e_out = await ctx.runner.run(
            ctx.earnings,
            e_in,
            instrument_id=history.instrument_id,
            as_of=history.as_of,
            subject=symbol,
        )
        items.append(SymbolAnalysis(symbol, history.instrument_id, t_in, t_out, e_in, e_out))
    result = ScanResult(mode, items, missing)
    outcomes = [o for i in items for o in i.outcomes]
    log.info(
        "scan.done",
        symbols=len(items),
        analyses=len(outcomes),
        cached=sum(1 for o in outcomes if o.cached),
        statuses=dict(Counter(o.status for o in outcomes)),
        cost_usd=round(result.cost_usd, 6),
        budget_mode=mode,
        missing=missing,
    )
    return result


def agent_step(
    day: date,
    item: SymbolAnalysis,
    name: StepName,
    outcome: Outcome[Any],
    status: StepStatus,
    summary: str,
) -> Step:
    """The agent's input and every attempt are in `analyses` and `llm_calls` (analysis_id)."""
    return Step(
        day=day,
        market=item.technical_input.market,
        instrument_id=item.instrument_id,
        symbol=item.symbol,
        step=name,
        status=status,
        summary=summary,
        output=outcome.output.model_dump(mode="json") if outcome.output is not None else None,
        error=None if outcome.status == "ok" else f"{outcome.status}: {outcome.reason or ''}",
        analysis_id=outcome.analysis_id,
    )


def earnings_step(day: date, item: SymbolAnalysis) -> Step:
    """Never stops a stock by itself: the proposer must pass on `wait_until_after`."""
    e = item.earnings
    if e.status != "ok" or e.output is None:
        return agent_step(
            day, item, "earnings", e, "error", f"earnings {e.status}, proposer runs without it"
        )
    out = e.output
    return agent_step(
        day, item, "earnings", e, "passed", f"{out.stance}, event risk {out.event_risk}"
    )
