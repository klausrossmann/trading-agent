"""The proposer stage: the proposer's input, how its answer and the critic's become a
proposal, the ranking, and the rank step."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from trading_agent.analysts.stage import SymbolAnalysis
from trading_agent.analysts.technical import TechnicalAssessment
from trading_agent.contexts import AnalysisContext
from trading_agent.core.steps import Step
from trading_agent.critic.agent import CriticVerdict, PlanView
from trading_agent.llm.runner import Outcome
from trading_agent.marketdata import store as market_repo
from trading_agent.marketdata.indicators import bars_to_frame
from trading_agent.marketdata.macro import FRED_SERIES, MacroSnapshot, macro_snapshot
from trading_agent.marketdata.types import Market
from trading_agent.portfolio import store as portfolio_repo
from trading_agent.proposer.agent import Holding, ProposerInput, ProposerOutput
from trading_agent.proposer.types import Proposal
from trading_agent.screen import rules as pullback


@dataclass(frozen=True)
class ProposalRun:
    as_of: date | None
    proposals: list[Proposal]
    skipped: dict[str, str]  # symbol -> why the proposer didn't run or its answer was dropped
    cost_usd: float


async def holdings(ctx: AnalysisContext) -> list[Holding]:
    """Open positions of the agent's book; the proposer skips those symbols."""
    async with ctx.sessions() as s:
        rows = [r for r in await portfolio_repo.book_trades(s, ctx.book) if r.exit_date is None]
        instruments = await market_repo.instruments(s, {r.instrument_id for r in rows})
    return [
        Holding(symbol=i.yahoo_symbol, market=i.market, sector=i.sector)
        for r in rows
        if (i := instruments.get(r.instrument_id)) is not None
    ]


async def macro_context(ctx: AnalysisContext, market: Market, as_of: date) -> MacroSnapshot:
    benchmark = ctx.benchmarks.get(market)
    async with ctx.sessions() as s:
        bench = await market_repo.instrument_by_symbol(s, benchmark) if benchmark else None
        bars = (
            await market_repo.bars(s, bench.id, as_of - timedelta(days=450))
            if bench is not None and bench.id is not None
            else []
        )
        series = {sid: await market_repo.macro_series(s, sid) for sid in FRED_SERIES.values()}
    close = bars_to_frame([b for b in bars if b.date <= as_of])["close"] if bars else None
    return macro_snapshot(benchmark, close, series, as_of)


def proposer_input(
    item: SymbolAnalysis,
    tech: TechnicalAssessment,
    held: list[Holding],
    macro: MacroSnapshot | None = None,
    source: str = pullback.NAME,
    catalyst: str | None = None,
) -> ProposerInput:
    t, e = item.technical_input, item.earnings_input
    unknown = list(t.unknown)
    if item.earnings.output is None:
        unknown.append("earnings")
    return ProposerInput(
        symbol=t.symbol,
        name=t.name,
        market=t.market,
        currency=t.currency,
        sector=t.sector,
        as_of=t.as_of,
        close=t.close,
        candidate_source=source,
        atr14=t.indicators.get("atr14"),
        levels=t.levels,
        rules=t.rules,
        technical=tech,
        earnings=item.earnings.output,
        earnings_event_in_window=e.event_in_window if item.earnings.output else None,
        next_earnings_date=e.next_report.date if e.next_report else None,
        holdings=held,
        unknown=sorted(unknown),
        macro=macro,
        catalyst=catalyst,
    )


def plan_view(inp: ProposerInput, out: ProposerOutput) -> PlanView:
    price = {lv.name: lv.price for lv in inp.levels}
    entry, stop, target = (price[str(r)] for r in (out.entry_ref, out.stop_ref, out.target_ref))
    return PlanView(
        entry_ref=str(out.entry_ref),
        entry=entry,
        stop_ref=str(out.stop_ref),
        stop=stop,
        target_ref=str(out.target_ref),
        target=target,
        risk_reward=round((target - entry) / (entry - stop), 2),
        stop_atr_multiple=round((entry - stop) / inp.atr14, 2) if inp.atr14 else None,
        confidence=out.confidence,
        thesis=out.thesis,
        invalidation=out.invalidation,
    )


def _ids(*outcomes: Outcome[Any]) -> dict[str, int]:
    return {o.module: o.analysis_id for o in outcomes if o.analysis_id is not None}


def build_proposal(
    item: SymbolAnalysis,
    inp: ProposerInput,
    proposed: Outcome[ProposerOutput],
    out: ProposerOutput,
    critique: Outcome[CriticVerdict] | None,
) -> Proposal:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source": "agent",
        "as_of": inp.as_of,
        "instrument_id": item.instrument_id,
        "yahoo_symbol": inp.symbol,
        "market": inp.market,
        "sector": inp.sector,
        "strategy": inp.candidate_source,
        "thesis": out.thesis,
        "invalidation": out.invalidation,
        "payload": {"proposer": out.model_dump(mode="json")},
    }
    outcomes: list[Outcome[Any]] = [item.technical, item.earnings, proposed]
    if out.decision == "no_trade":
        return Proposal(
            **base,
            status="no_trade",
            confidence=out.confidence,
            analyses=_ids(*outcomes),
        )
    plan = plan_view(inp, out)
    verdict = critique.output if critique is not None else None
    if critique is not None:
        outcomes.append(critique)
    if verdict is not None:
        confidence = min(max(out.confidence + verdict.confidence_delta, 0.0), 1.0)
        base["payload"]["critic"] = verdict.model_dump(mode="json")
    else:
        confidence = out.confidence
        base["payload"]["critic"] = {"unavailable": critique.reason if critique else "not run"}
    return Proposal(
        **base,
        status="blocked" if verdict is not None and verdict.severity == "blocking" else "proposed",
        entry_ref=plan.entry_ref,
        stop_ref=plan.stop_ref,
        target_ref=plan.target_ref,
        entry=plan.entry,
        stop=plan.stop,
        target=plan.target,
        confidence=round(confidence, 4),
        critic_severity=verdict.severity if verdict else None,
        critic_summary=verdict.summary if verdict else None,
        analyses=_ids(*outcomes),
    )


def score(p: Proposal) -> float:
    """What the ranking orders by: the chance to reach the target times the reward per risk."""
    return (p.confidence or 0.0) * (p.risk_reward or 0.0)


def rank(items: list[Proposal]) -> list[Proposal]:
    """`proposed` items numbered by score, best first; the others get no rank."""
    survivors = sorted((p for p in items if p.status == "proposed"), key=score, reverse=True)
    ranked = [p.model_copy(update={"rank": n}) for n, p in enumerate(survivors, start=1)]
    return ranked + [p for p in items if p.status != "proposed"]


def rank_step(day: date, p: Proposal) -> Step:
    confidence, rr = p.confidence or 0.0, p.risk_reward or 0.0
    return Step(
        day=day,
        market=p.market,
        instrument_id=p.instrument_id,
        symbol=p.yahoo_symbol,
        step="rank",
        status="passed",
        summary=(f"rank {p.rank}: confidence {confidence:.2f} x R:R {rr:.2f} = {score(p):.2f}"),
        output={"rank": p.rank, "confidence": confidence, "risk_reward": rr, "score": score(p)},
    )
