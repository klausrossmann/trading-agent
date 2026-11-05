"""The risk stage: the risk engine's decision for one proposal, with the facts it needs, and
the risk step."""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from trading_agent.core.steps import Step
from trading_agent.marketdata import calendars
from trading_agent.marketdata import store as market_repo
from trading_agent.marketdata.types import BERLIN, Instrument
from trading_agent.portfolio import manager as portfolio_manager
from trading_agent.portfolio.manager import LoadedBook, TradingContext
from trading_agent.portfolio.snapshot import proposal_levels, snapshot
from trading_agent.proposer.types import Proposal
from trading_agent.risk import engine
from trading_agent.risk.types import RiskDecision

HISTORY_DAYS = 150  # calendar days of bars for ATR(14), 20-day value and 60-day correlations


def _sessions_to_earnings(cal: str, today: date, dates: Sequence[date]) -> int | None:
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        return None
    return len(calendars.sessions(cal, today, min(upcoming))) - 1


async def decide(
    ctx: TradingContext, loaded: LoadedBook, p: Proposal, now: datetime, today: date
) -> tuple[RiskDecision, Instrument, dict[str, Any]]:
    """The decision, the instrument and the decision's inputs (for the step log)."""
    cal = calendars.CALENDAR_BY_MARKET[p.market]
    _, portfolio = await portfolio_manager.state(ctx, loaded, today)
    cfg = ctx.limits
    held_ids = {h.instrument_id for h in portfolio.holdings}
    start = today - timedelta(days=HISTORY_DAYS)
    async with ctx.sessions() as s:
        inst = (await market_repo.instruments(s, [p.instrument_id]))[p.instrument_id]
        bars = await market_repo.bars(s, p.instrument_id, start)
        held = {i: await market_repo.bars(s, i, start) for i in held_ids}
        earnings = (await market_repo.earnings_dates(s, [p.instrument_id])).get(p.instrument_id, [])
    if inst.currency not in loaded.rates:
        raise LookupError(f"no EUR/{inst.currency} rate stored")
    market = snapshot(
        inst,
        bars,
        held,
        session_open=calendars.session_time(cal, today, "open"),
        session_close=calendars.session_time(cal, today, "close"),
        sessions_to_earnings=_sessions_to_earnings(cal, today, earnings),
        eur_rate=loaded.rates[inst.currency],
        mid=await ctx.market_info.mid(inst) if ctx.market_info else None,
    )
    controls = await ctx.center.controls()
    levels = proposal_levels(p, await ctx.ticks(inst))
    decision = engine.evaluate(p, levels, portfolio, market, controls, cfg, ctx.fees, now)
    inputs = {
        "levels": {k: str(v) for k, v in levels.items()},
        "confidence": p.confidence,
        "rank": p.rank,
        "market": market.model_dump(mode="json"),
        "portfolio": portfolio.model_dump(mode="json"),
        "controls": controls.model_dump(mode="json"),
    }
    return decision, inst, inputs


def risk_step(p: Proposal, decision: RiskDecision, inputs: dict[str, Any], now: datetime) -> Step:
    if decision.approved:
        summary = (
            f"approved {decision.quantity.normalize():f} shares at {decision.entry}, "
            f"risk {decision.risk:.2f}, value {decision.value:.2f}, fees {decision.fees:.2f}"
        )
    else:
        summary = "; ".join(f"{c.name}: {c.detail}" for c in decision.failures) or "rejected"
    return Step(
        day=now.astimezone(BERLIN).date(),
        market=p.market,
        instrument_id=p.instrument_id,
        symbol=p.yahoo_symbol,
        step="risk",
        status="passed" if decision.approved else "stopped",
        summary=summary,
        input=inputs,
        output=decision.model_dump(mode="json"),
        proposal_id=p.id,
    )
