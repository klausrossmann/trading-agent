"""The orchestrator: the whole trading day in one place, stage by stage.

    news        before each scan    every stock's new headlines -> News agent -> buy signals
    scan        before each open    screen (+ news buy signals) -> technical -> earnings ->
                                    proposer -> critic -> rank
    place       15 min after open   risk engine -> bracket order at IBKR (or nothing, if offline)
    monitor     every 10 min        IBKR orders and fills into the book, fill and sale alerts
    news        every 30 min        held stocks' headlines -> News agent -> exit at the market
    end_of_day  after each close    fills -> breakeven / trailing / time stop -> book, loss limits

`scheduler.py` decides when each stage runs; the news stages are in `news.py`.
Every stage writes each stock's steps to the step log (dashboard -> Day). The building blocks
live elsewhere: `pipeline.py` (agent inputs, proposals, ranking), `trading.py` (risk decision,
book, IBKR sync), `executor.py` (orders), `risk/engine.py` (the 9 checks).
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial

import structlog

from trading_agent import jobs, pipeline, trading
from trading_agent.agents.critic import CriticInput, CriticVerdict
from trading_agent.calc.indicators import atr, bars_to_frame
from trading_agent.calc.macro import MacroSnapshot
from trading_agent.calc.stops import breakeven_stop, chandelier_stop
from trading_agent.data import calendars
from trading_agent.data.ingest import describe_error
from trading_agent.db import book as book_repo
from trading_agent.db import market as market_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import steps as steps_repo
from trading_agent.domain.market import BERLIN, Bar, Market
from trading_agent.domain.orders import Bracket, BracketRequest
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.steps import SCAN_STEPS, Step
from trading_agent.llm.runner import Outcome
from trading_agent.llm.sanitize import untrusted
from trading_agent.notify import messages
from trading_agent.notify.telegram import Notifier
from trading_agent.portfolio.snapshot import to_tick
from trading_agent.strategies import news, pullback
from trading_agent.trading import HISTORY_DAYS, TradingContext

log = structlog.get_logger(__name__)


# --- scan: which stocks are worth buying, and at which prices ---


def _catalyst(signal: Step) -> str:
    headlines = (signal.input or {}).get("headlines", [])[:3]
    reason = (signal.output or {}).get("reason", "")
    return untrusted("news", f"Headlines: {' | '.join(headlines)}. News agent: {reason}", 800)


async def news_signals(
    ctx: jobs.AnalysisContext, market: Market | None, now: datetime
) -> dict[int, Step]:
    """The latest buy signal per stock within `news.signal_hours`."""
    since = now - timedelta(hours=ctx.news.signal_hours)
    async with ctx.sessions() as s:
        found = await steps_repo.news_signals(s, market, since)
    return {x.instrument_id: x for x in found if x.instrument_id is not None}


async def scan(
    ctx: jobs.AnalysisContext,
    market: Market | None = None,
    symbols: list[str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> pipeline.ProposalRun:
    """Screen the market (or take `symbols`), add the stocks with a news buy signal, let the
    agents decide stock by stock, rank the survivors and store the proposals and every
    stock's steps. No orders here."""
    now = now or datetime.now(UTC)
    today = today or now.astimezone(BERLIN).date()
    held = await pipeline.holdings(ctx)
    held_symbols = {h.symbol for h in held}
    signals = await news_signals(ctx, market, now)
    catalysts = {x.symbol: _catalyst(x) for x in signals.values()}
    sources: dict[str, str] = {}
    steps: list[Step] = []
    if symbols is None:
        confidence = {k: float((x.output or {})["confidence"]) for k, x in signals.items()}
        found = await jobs.screen_stocks(
            ctx, market, await jobs.scan_size(ctx), held_symbols, now, confidence
        )
        steps += [pipeline.screen_step(today, r) for r in found]
        symbols = [r.symbol for r in found if r.passed]
        sources = {r.symbol: r.source for r in found if r.passed}
    analysed = await jobs.analyse(ctx, symbols, market, today=today)
    items: list[Proposal] = []
    skipped: dict[str, str] = {}
    macros: dict[tuple[Market, date], MacroSnapshot] = {}
    cost = analysed.cost_usd
    for item in analysed.items:
        tech = item.technical.output
        step = partial(pipeline.agent_step, today, item)
        if item.symbol in held_symbols:
            skipped[item.symbol] = "already held"
            continue
        # technical analyst: only buy and strong_buy go on
        if item.technical.status != "ok" or tech is None:
            skipped[item.symbol] = f"technical {item.technical.status}"
            steps.append(step("technical", item.technical, "error", skipped[item.symbol]))
            continue
        if tech.rating not in pipeline.BUY_RATINGS:
            skipped[item.symbol] = f"technical rating {tech.rating}"
            summary = f"rating {tech.rating}, needs buy or strong_buy"
            steps.append(step("technical", item.technical, "stopped", summary))
            continue
        summary = f"rating {tech.rating}, setup quality {tech.setup_quality:.2f}"
        steps.append(step("technical", item.technical, "passed", summary))
        # earnings analyst: advice for the proposer, never a stop by itself
        steps.append(pipeline.earnings_step(today, item))
        # proposer: propose (with levels from the menu) or no_trade
        t_in = item.technical_input
        key = (t_in.market, t_in.as_of)
        if key not in macros:
            macros[key] = await pipeline.macro_context(ctx, *key)
        inp = pipeline.proposer_input(
            item,
            tech,
            held,
            macros[key],
            sources.get(item.symbol, pullback.NAME),
            catalysts.get(item.symbol),
        )
        proposed = await ctx.runner.run(
            ctx.proposer,
            inp,
            instrument_id=item.instrument_id,
            as_of=inp.as_of,
            subject=item.symbol,
        )
        cost += proposed.cost_usd
        out = proposed.output
        if proposed.status != "ok" or out is None:
            skipped[item.symbol] = f"proposer {proposed.status}: {proposed.reason or 'invalid'}"
            steps.append(step("proposer", proposed, "error", skipped[item.symbol]))
            continue
        critique: Outcome[CriticVerdict] | None = None
        if out.decision == "no_trade":
            steps.append(step("proposer", proposed, "stopped", f"no trade: {out.no_trade_reason}"))
        else:
            plan = pipeline.plan_view(inp, out)
            summary = (
                f"buy at {plan.entry_ref} {plan.entry:g}, stop {plan.stop_ref} {plan.stop:g}, "
                f"target {plan.target_ref} {plan.target:g}, R:R {plan.risk_reward:g}, "
                f"confidence {out.confidence:.2f}"
            )
            steps.append(step("proposer", proposed, "passed", summary))
            # critic: blocking stops the trade, otherwise it adjusts the confidence
            critique = await ctx.runner.run(
                ctx.critic,
                CriticInput(facts=inp, proposal=plan),
                instrument_id=item.instrument_id,
                as_of=inp.as_of,
                subject=item.symbol,
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
            steps.append(pipeline.critic_step(step, critique))
        items.append(pipeline.build_proposal(item, inp, proposed, out, critique))

    ranked = pipeline.rank(items)
    if ranked:
        async with ctx.sessions.begin() as s:
            stored = await proposals_repo.save_proposals(s, ranked)
        ranked = [p.model_copy(update={"id": i}) for p, i in zip(ranked, stored, strict=True)]
    steps += [pipeline.rank_step(today, p) for p in ranked if p.rank is not None]
    ids = {p.yahoo_symbol: p.id for p in ranked}
    steps = [s.model_copy(update={"proposal_id": ids.get(s.symbol)}) for s in steps]
    async with ctx.sessions.begin() as s:
        await steps_repo.replace_steps(s, today, SCAN_STEPS, steps)
    log.info(
        "proposals.done",
        market=market,
        candidates=len(analysed.items),
        statuses=dict(Counter(p.status for p in ranked)),
        skipped=skipped,
        cost_usd=round(cost, 6),
    )
    as_of = max((i.technical_input.as_of for i in analysed.items), default=None)
    return pipeline.ProposalRun(as_of, ranked, skipped, cost)


def summary(run: pipeline.ProposalRun, market: Market | None) -> str:
    scope = f" {market}" if market else ""
    as_of = f", as of {messages.day_label(run.as_of)}" if run.as_of else ""
    return messages.render_proposals(
        f"Proposals{scope}{as_of}", run.proposals, skipped=len(run.skipped), cost_usd=run.cost_usd
    )


async def scheduled_scan(
    ctx: jobs.AnalysisContext, notifier: Notifier, market: Market
) -> pipeline.ProposalRun:
    run = await scan(ctx, market)
    await notifier.send(summary(run, market))
    return run


# --- place: risk engine, then the bracket order at IBKR ---


@dataclass
class Placement:
    placed: list[messages.PlacedLine] = field(default_factory=list[messages.PlacedLine])
    rejected: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


NOT_CONNECTED = "not placed: IBKR not connected"


async def _not_connected(
    ctx: TradingContext, market: Market, todo: Sequence[Proposal], now: datetime
) -> Placement:
    """No risk decision and no order: each proposal gets an error step, you get an alert."""
    steps = [
        Step(
            day=now.astimezone(BERLIN).date(),
            market=p.market,
            instrument_id=p.instrument_id,
            symbol=p.yahoo_symbol,
            step="order",
            status="error",
            summary=NOT_CONNECTED,
            error="IB Gateway not connected",
            proposal_id=p.id,
        )
        for p in todo
    ]
    async with ctx.sessions.begin() as s:
        await steps_repo.add_steps(s, steps)
    symbols = [p.yahoo_symbol for p in todo]
    await ctx.notifier.send(messages.not_connected_alert(f"Orders {market}", symbols))
    log.warning("place.not_connected", market=market, symbols=symbols)
    return Placement(rejected=[(s, NOT_CONNECTED) for s in symbols])


async def news_trades_today(ctx: TradingContext, now: datetime) -> int:
    """Entries placed today (Berlin date, both markets) for news-triggered proposals."""
    day = now.astimezone(BERLIN).date()
    loaded = await trading.load(ctx)
    ids = [b.id for b, created in loaded.brackets if created.astimezone(BERLIN).date() == day]
    async with ctx.sessions() as s:
        found = await proposals_repo.by_ids(s, ids)
    return sum(1 for p in found.values() if p.strategy == news.NAME)


def _news_limit_step(p: Proposal, reason: str, now: datetime) -> Step:
    return Step(
        day=now.astimezone(BERLIN).date(),
        market=p.market,
        instrument_id=p.instrument_id,
        symbol=p.yahoo_symbol,
        step="risk",
        status="stopped",
        summary=reason,
        proposal_id=p.id,
    )


async def place(ctx: TradingContext, market: Market) -> Placement:
    """After the open: today's proposals (as_of = previous session) in rank order, each through
    the risk engine; approved ones go to IBKR as bracket orders."""
    now = ctx.clock()
    cal = calendars.CALENDAR_BY_MARKET[market]
    today = now.astimezone(calendars.calendar(cal).tz).date()
    result = Placement()
    if calendars.session_time(cal, today, "open") is None:
        log.info("place.skipped", market=market, reason="no session today")
        return result
    as_of = calendars.session_offset(cal, today, -1)
    async with ctx.sessions() as s:
        found = await proposals_repo.proposals(s, start=as_of, end=as_of)
    todo = sorted(
        (p for p in found if p.market == market and p.status == "proposed"),
        key=lambda p: (p.rank is None, p.rank or 0),
    )
    if not todo:
        log.info("place.nothing", market=market, as_of=as_of.isoformat())
        return result
    if not ctx.connected():
        return await _not_connected(ctx, market, todo, now)
    await ctx.executor.sync()
    expires_day = calendars.session_offset(cal, today, ctx.rules.entry_valid_sessions - 1)
    expires = calendars.session_time(cal, expires_day, "close")
    if expires is None:
        raise LookupError(f"no close for {expires_day}")
    news_placed = await news_trades_today(ctx, now)
    for p in todo:
        if p.strategy == news.NAME and news_placed >= ctx.news.max_trades_per_day:
            reason = f"news: at most {ctx.news.max_trades_per_day} news trade a day"
            async with ctx.sessions.begin() as s:
                await steps_repo.add_steps(s, [_news_limit_step(p, reason, now)])
            result.rejected.append((p.yahoo_symbol, reason))
            continue
        decision, inst, inputs = await trading.decide(ctx, await trading.load(ctx), p, now, today)
        async with ctx.sessions.begin() as s:
            await book_repo.save_decision(s, p.id, decision, now)
            await steps_repo.add_steps(s, [trading.risk_step(p, decision, inputs, now)])
        if decision.trip is not None:
            await ctx.center.apply_trip(decision.trip)
        entry, stop, target = decision.entry, decision.stop, decision.target
        if not decision.approved or entry is None or stop is None or target is None:
            first = decision.failures[0]
            result.rejected.append((p.yahoo_symbol, f"{first.name}: {first.detail}"))
            continue
        request = BracketRequest(
            id=p.id,
            instrument=inst,
            quantity=decision.quantity,
            entry=entry,
            stop=stop,
            target=target,
            expires=expires,
        )
        try:
            await ctx.executor.submit(request, decision)
        except Exception as exc:
            # the order step holds the error; the other proposals still get their turn
            log.exception("place.order_failed", symbol=p.yahoo_symbol)
            result.rejected.append((p.yahoo_symbol, f"order error: {describe_error(exc)}"))
            continue
        if p.strategy == news.NAME:
            news_placed += 1
        result.placed.append(
            messages.PlacedLine(
                p.yahoo_symbol, decision.quantity, float(entry), float(stop), float(target)
            )
        )
    broker = ctx.executor.broker.name
    await ctx.notifier.send(
        messages.placement_summary(market, result.placed, result.rejected, broker)
    )
    log.info("place.done", market=market, placed=len(result.placed), rejected=len(result.rejected))
    return result


# --- monitor and end of day: what IBKR did, and managing what is held ---


async def monitor(ctx: TradingContext) -> list[Bracket]:
    """Every few minutes in sessions: IBKR orders and fills into the book."""
    if not ctx.connected():
        log.info("monitor.skipped", reason="IBKR not connected")
        return []
    return await trading.sync(ctx)


async def end_of_day(ctx: TradingContext, market: Market) -> None:
    """After the market's closing prices are stored: sync with IBKR, move stops, send time
    exits, then the book, the account value and the loss limits."""
    now = ctx.clock()
    cal = calendars.CALENDAR_BY_MARKET[market]
    day = calendars.last_closed_session(cal, now)
    if ctx.connected():
        open_ = await ctx.executor.open_brackets()
        async with ctx.sessions() as s:
            instruments = await market_repo.instruments(s, {b.instrument_id for b in open_})
            bars = await market_repo.bars_on(s, instruments, day)
        await trading.sync(ctx)
        await _manage_positions(ctx, market, day, bars)
    else:
        what = f"End of day {market}: fills, stop moves and time stops"
        await ctx.notifier.send(messages.not_connected_alert(what, []))
        log.warning("end_of_day.not_connected", market=market)
    await trading.rebuild_book(ctx, market, day)


async def _manage_positions(
    ctx: TradingContext, market: Market, day: date, bars: dict[int, Bar]
) -> None:
    """Time stop after `time_stop_sessions`; otherwise the stop moves to breakeven at +1R and,
    optionally, trails `trail_atr` ATR under the highest close. Stops only ever move up."""
    cal = calendars.CALENDAR_BY_MARKET[market]
    loaded = await trading.load(ctx)
    fills = {f.bracket_id: f.day for f in loaded.fills if f.kind == "entry"}
    for b in await ctx.executor.open_brackets():
        inst = loaded.instruments.get(b.instrument_id)
        entered = fills.get(b.id)
        if inst is None or inst.market != market or b.state != "filled" or entered is None:
            continue
        held = len(calendars.sessions(cal, entered, day)) - 1
        if held >= ctx.rules.time_stop_sessions:
            await ctx.executor.request_exit(b.id, "time")
            continue
        bar = bars.get(b.instrument_id)
        if bar is None or entered == day or b.entry_price is None:
            continue
        trigger = float(b.entry_price) + ctx.rules.breakeven_r * float(b.entry - b.initial_stop)
        moved = breakeven_stop(float(bar.high), float(b.stop), float(b.entry_price), trigger)
        if ctx.rules.trail_atr is not None and moved >= float(b.entry_price):
            async with ctx.sessions() as s:
                since = entered - timedelta(days=HISTORY_DAYS)
                history = [
                    x for x in await market_repo.bars(s, b.instrument_id, since) if x.date <= day
                ]
            highest = max(float(x.close) for x in history if x.date >= entered)
            last_atr = float(atr(bars_to_frame(history)).iloc[-1])
            moved = chandelier_stop(
                moved, float(b.entry_price), highest, last_atr, ctx.rules.trail_atr
            )
        new_stop = to_tick(Decimal(str(moved)), True, await ctx.ticks(inst))
        if new_stop > b.stop:
            await ctx.executor.move_stop(b.id, new_stop)
            log.info("stop.moved", symbol=inst.yahoo_symbol, stop=str(new_stop))
