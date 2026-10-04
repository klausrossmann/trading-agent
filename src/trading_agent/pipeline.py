"""Agent pipeline (IMPLEMENTATION.md 8): candidates -> modules -> proposer -> critic -> ranking.

Stores one proposal per proposer decision. No orders: the agent_shadow book simulates the
proposed trades until the risk engine and execution arrive (M8).
"""

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog

from trading_agent import jobs
from trading_agent.agents.critic import CriticInput, CriticVerdict, PlanView
from trading_agent.agents.portfolio import Candidate, PortfolioInput, Ranking
from trading_agent.agents.proposer import Holding, ProposerInput, ProposerOutput
from trading_agent.db import market as market_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import trades as trades_repo
from trading_agent.domain.market import Market
from trading_agent.domain.proposals import Proposal
from trading_agent.llm.runner import Outcome
from trading_agent.modules.technical import TechnicalAssessment
from trading_agent.notify import messages
from trading_agent.notify.telegram import Notifier
from trading_agent.strategies import pullback

log = structlog.get_logger(__name__)

AGENT_BOOK = "agent_shadow"
BUY_RATINGS = ("buy", "strong_buy")


@dataclass(frozen=True)
class ProposalRun:
    as_of: date | None
    proposals: list[Proposal]
    skipped: dict[str, str]  # symbol -> why the proposer didn't run or its answer was dropped
    cost_usd: float


async def holdings(ctx: jobs.AnalysisContext) -> list[Holding]:
    async with ctx.sessions() as s:
        rows = await trades_repo.book_trades(s, AGENT_BOOK)
        instruments = await market_repo.active_instruments(s)
    return [
        Holding(symbol=i.yahoo_symbol, market=i.market, sector=i.sector)
        for r in rows
        if r.exit_date is None and (i := instruments.get(r.instrument_id)) is not None
    ]


def proposer_input(
    item: jobs.SymbolAnalysis, tech: TechnicalAssessment, held: list[Holding]
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
        candidate_source=pullback.NAME,
        atr14=t.indicators.get("atr14"),
        levels=t.levels,
        rules=t.rules,
        technical=tech,
        earnings=item.earnings.output,
        earnings_event_in_window=e.event_in_window if item.earnings.output else None,
        next_earnings_date=e.next_report.date if e.next_report else None,
        holdings=held,
        unknown=sorted(unknown),
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
    item: jobs.SymbolAnalysis,
    instrument_id: int,
    inp: ProposerInput,
    proposed: Outcome[ProposerOutput],
    out: ProposerOutput,
    critique: Outcome[CriticVerdict] | None,
) -> Proposal:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source": "agent",
        "as_of": inp.as_of,
        "instrument_id": instrument_id,
        "yahoo_symbol": inp.symbol,
        "market": inp.market,
        "sector": inp.sector,
        "strategy": pullback.NAME,
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


def _fallback_order(items: list[Proposal]) -> list[Proposal]:
    return sorted(items, key=lambda p: (p.confidence or 0) * (p.risk_reward or 0), reverse=True)


async def rank(
    ctx: jobs.AnalysisContext, items: list[Proposal], held: list[Holding]
) -> tuple[list[Proposal], Outcome[Ranking] | None]:
    """Ranks `proposed` items; the LLM only runs when there is a choice to make."""
    survivors = [p for p in items if p.status == "proposed"]
    others = [p for p in items if p.status != "proposed"]
    ranked = _fallback_order(survivors)
    outcome: Outcome[Ranking] | None = None
    if len(survivors) > 1:
        inp = PortfolioInput(
            as_of=max(p.as_of for p in survivors),
            candidates=[
                Candidate(
                    symbol=p.yahoo_symbol,
                    market=p.market,
                    sector=p.sector,
                    confidence=p.confidence or 0.0,
                    risk_reward=round(p.risk_reward or 0.0, 2),
                    thesis=p.thesis,
                    critic_severity=p.critic_severity or "none",
                    critic_summary=p.critic_summary or "no critique available",
                )
                for p in ranked
            ],
            holdings=held,
            free_slots=max(ctx.max_open_positions - len(held), 0),
            budget_mode=await ctx.runner.mode(),
        )
        outcome = await ctx.runner.run(ctx.portfolio, inp, instrument_id=None, as_of=inp.as_of)
        if outcome.status == "ok" and outcome.output is not None:
            order = [r.symbol for r in outcome.output.ranking]
            notes = {r.symbol: r.note for r in outcome.output.ranking}
            ranked = sorted(ranked, key=lambda p: order.index(p.yahoo_symbol))
            ranked = [
                p.model_copy(
                    update={
                        "analyses": p.analyses | _ids(outcome),
                        "payload": p.payload
                        | {
                            "portfolio_manager": {
                                "note": notes[p.yahoo_symbol],
                                "rationale": outcome.output.rationale,
                            }
                        },
                    }
                )
                for p in ranked
            ]
    ranked = [p.model_copy(update={"rank": n}) for n, p in enumerate(ranked, start=1)]
    return ranked + others, outcome


async def propose(
    ctx: jobs.AnalysisContext,
    market: Market | None = None,
    symbols: list[str] | None = None,
    today: date | None = None,
) -> ProposalRun:
    """Analyse today's candidates, let the agents decide, store the proposals."""
    today = today or datetime.now(UTC).date()
    scan = await jobs.analyse(ctx, symbols, market, today=today)
    held = await holdings(ctx)
    async with ctx.sessions() as s:
        ids = {i.yahoo_symbol: k for k, i in (await market_repo.active_instruments(s)).items()}
    held_symbols = {h.symbol for h in held}
    items: list[Proposal] = []
    skipped: dict[str, str] = {}
    cost = scan.cost_usd
    for item in scan.items:
        tech = item.technical.output
        if item.symbol in held_symbols:
            skipped[item.symbol] = "already held"
            continue
        if item.technical.status != "ok" or tech is None:
            skipped[item.symbol] = f"technical {item.technical.status}"
            continue
        if tech.rating not in BUY_RATINGS:
            skipped[item.symbol] = f"technical rating {tech.rating}"
            continue
        inp = proposer_input(item, tech, held)
        as_of = inp.as_of
        proposed = await ctx.runner.run(
            ctx.proposer, inp, instrument_id=ids[item.symbol], as_of=as_of
        )
        cost += proposed.cost_usd
        out = proposed.output
        if proposed.status != "ok" or out is None:
            skipped[item.symbol] = f"proposer {proposed.status}: {proposed.reason or 'invalid'}"
            continue
        critique: Outcome[CriticVerdict] | None = None
        if out.decision == "propose":
            critique = await ctx.runner.run(
                ctx.critic,
                CriticInput(facts=inp, proposal=plan_view(inp, out)),
                instrument_id=ids[item.symbol],
                as_of=as_of,
            )
            cost += critique.cost_usd
            if critique.status != "ok":
                critique = Outcome(
                    critique.module,
                    critique.status,
                    critique.model,
                    analysis_id=critique.analysis_id,
                    cost_usd=critique.cost_usd,
                    reason=critique.reason or "critic answer failed validation",
                )
        items.append(build_proposal(item, ids[item.symbol], inp, proposed, out, critique))

    ranked, ranking = await rank(ctx, items, held)
    if ranking is not None:
        cost += ranking.cost_usd
    if ranked:
        async with ctx.sessions.begin() as s:
            stored = await proposals_repo.save_proposals(s, ranked)
        ranked = [p.model_copy(update={"id": i}) for p, i in zip(ranked, stored, strict=True)]
    log.info(
        "proposals.done",
        market=market,
        candidates=len(scan.items),
        statuses=dict(Counter(p.status for p in ranked)),
        skipped=skipped,
        cost_usd=round(cost, 6),
    )
    as_of = max((i.technical_input.as_of for i in scan.items), default=None)
    return ProposalRun(as_of, ranked, skipped, cost)


def summary(run: ProposalRun, market: Market | None) -> str:
    scope = f" {market}" if market else ""
    as_of = f", as of {messages.day_label(run.as_of)}" if run.as_of else ""
    return messages.render_proposals(
        f"Proposals{scope}{as_of}", run.proposals, skipped=len(run.skipped), cost_usd=run.cost_usd
    )


async def scheduled_scan(
    ctx: jobs.AnalysisContext, notifier: Notifier, market: Market
) -> ProposalRun:
    run = await propose(ctx, market)
    await notifier.send(summary(run, market))
    return run
