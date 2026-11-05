"""The news stage: company news read by the News agent, and what code does with its answers.

    held stocks   every 30 min    new headlines, or a report within 3 sessions (once a day)
                                  -> News agent -> `exit` >= exit_confidence: market sell at IBKR
                                                   `exit` below it: advice, /exit SYMBOL is yours
    the market    before a scan   new headlines of every other stock -> News agent
                                  -> `buy` >= buy_confidence: the stock joins the next scan
                                     (orchestrator.scan; basic rules, at most 1 news trade a day)

News comes from Finnhub for US stocks (needs FINNHUB_API_KEY) and from Yahoo for EU stocks.
Every answer that matters leaves a `news` step in the step log.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import structlog

from trading_agent import portfolio_manager
from trading_agent.calc.indicators import atr, bars_to_frame, sma
from trading_agent.data import calendars
from trading_agent.data.ingest import describe_error
from trading_agent.data.news import NewsSource
from trading_agent.db import market as market_repo
from trading_agent.db import news as news_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import steps as steps_repo
from trading_agent.domain.market import BERLIN, Bar, Instrument, Market
from trading_agent.domain.news import NewsItem
from trading_agent.domain.orders import Bracket
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.steps import Step, StepStatus
from trading_agent.llm.runner import LlmRunner
from trading_agent.llm.sanitize import clean
from trading_agent.modules.news import (
    NewsAgent,
    NewsInput,
    PositionFacts,
    StockNews,
    StockVerdict,
    stock_news,
)
from trading_agent.notify import messages
from trading_agent.strategies.news import NewsParams

log = structlog.get_logger(__name__)

EARLIER_DAYS = 5  # sorted news shown again with a held stock
REPORT_SESSIONS = 3  # a report this close gets a held stock read even without news
HISTORY_DAYS = 120


@dataclass
class NewsContext:
    trade: portfolio_manager.TradingContext
    runner: LlmRunner
    agent: NewsAgent
    sources: list[NewsSource]
    params: NewsParams = field(default_factory=NewsParams)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class Held:
    bracket: Bracket
    instrument: Instrument
    proposal: Proposal | None
    entered: date


@dataclass(frozen=True)
class Item:
    """One stock of a News agent call."""

    instrument: Instrument
    stock: StockNews
    new: list[NewsItem]
    held: Held | None = None


async def held_positions(ctx: NewsContext) -> list[Held]:
    t = ctx.trade
    brackets, fills, instruments = await portfolio_manager.load_book(t.sessions, t.book, t.fees)
    entered = {f.bracket_id: f.day for f in reversed(fills) if f.kind == "entry"}
    held = [b for b, _ in brackets if b.open_qty > 0 and b.id in entered]
    async with t.sessions() as s:
        proposals = await proposals_repo.by_ids(s, [b.id for b in held])
    return [Held(b, instruments[b.instrument_id], proposals.get(b.id), entered[b.id]) for b in held]


async def fetch(ctx: NewsContext, instruments: Sequence[Instrument], today: date) -> int:
    """New headlines of `instruments` into the database; returns how many were new."""
    start = today - timedelta(days=ctx.params.lookback_days)
    added = 0
    for inst in instruments:
        source = next((s for s in ctx.sources if s.covers(inst)), None)
        if source is None:
            continue
        try:
            items = await source.company_news(inst, start, today)
        except Exception as exc:  # one provider hiccup must not stop the other stocks
            log.warning("news.fetch_failed", symbol=inst.yahoo_symbol, error=describe_error(exc))
            continue
        async with ctx.trade.sessions.begin() as s:
            added += await news_repo.add_news(s, items)
        await ctx.sleep(source.pause_s)
    return added


async def _new_headlines(ctx: NewsContext, inst_id: int) -> list[NewsItem]:
    """The newest untriaged headlines; older ones beyond the limit are marked unread."""
    async with ctx.trade.sessions() as s:
        pending = await news_repo.untriaged(s, [inst_id])
    keep = ctx.params.max_headlines
    if len(pending) > keep:
        async with ctx.trade.sessions.begin() as s:
            for n in pending[:-keep]:
                await news_repo.set_triage(s, n.id, "none", f"not read: over {keep} new items")
    return list(reversed(pending[-keep:]))


def _last(series: pd.Series) -> float | None:
    value = float(series.iloc[-1])
    return round(value, 2) if math.isfinite(value) else None


def position_facts(h: Held, bars: list[Bar], report: date | None) -> PositionFacts:
    frame = bars_to_frame(bars)
    close = frame["close"]
    as_of = bars[-1].date
    b = h.bracket
    entry = float(b.entry_price or b.entry)
    risk = float(b.entry - b.initial_stop)
    last_close = float(close.iloc[-1])
    cal = calendars.CALENDAR_BY_MARKET[h.instrument.market]
    proposal = h.proposal
    return PositionFacts(
        as_of=as_of,
        entry_date=h.entered,
        sessions_held=len(calendars.sessions(cal, h.entered, as_of)) - 1,
        entry_price=round(entry, 2),
        initial_stop=float(b.initial_stop),
        stop=float(b.stop),
        target=float(b.target),
        last_close=round(last_close, 2),
        r_now=round((last_close - entry) / risk, 2) if risk > 0 else 0.0,
        closes_last_10=[round(float(c), 2) for c in close.tail(10)],
        sma20=_last(sma(close, 20)),
        sma50=_last(sma(close, 50)),
        atr14=_last(atr(frame)),
        next_report=report,
        sessions_to_report=len(calendars.sessions(cal, as_of, report)) - 1 if report else None,
        thesis=proposal.thesis if proposal else "unknown",
        invalidation=proposal.invalidation if proposal else "unknown",
    )


async def _held_item(ctx: NewsContext, h: Held, now: datetime, today: date) -> Item | None:
    inst, inst_id = h.instrument, h.bracket.instrument_id
    new = await _new_headlines(ctx, inst_id)
    async with ctx.trade.sessions() as s:
        bars = await market_repo.bars(s, inst_id, today - timedelta(days=HISTORY_DAYS))
        dates = (await market_repo.earnings_dates(s, [inst_id])).get(inst_id, [])
        earlier = await news_repo.recent(s, inst_id, now - timedelta(days=EARLIER_DAYS))
        read_today = any(
            x.step == "news" for x in await steps_repo.steps(s, today, inst.yahoo_symbol)
        )
    if not bars:
        return None
    facts = position_facts(h, bars, next((d for d in dates if d >= today), None))
    near_report = (
        facts.sessions_to_report is not None and facts.sessions_to_report <= REPORT_SESSIONS
    )
    if not new and not (near_report and not read_today):
        return None
    stock = stock_news(inst.yahoo_symbol, inst.name, inst.market, inst.sector, new, facts, earlier)
    return Item(inst, stock, new, h)


async def watch_positions(ctx: NewsContext) -> list[str]:
    """Every 30 minutes: the held stocks' news; returns the symbols the agent wants sold."""
    now = ctx.clock()
    today = now.astimezone(BERLIN).date()
    held = [h for h in await held_positions(ctx) if h.bracket.state == "filled"]
    await fetch(ctx, [h.instrument for h in held], today)
    items = [x for h in held if (x := await _held_item(ctx, h, now, today)) is not None]
    return await _ask(ctx, items, today, "positions")


async def scan_news(ctx: NewsContext, market: Market) -> list[str]:
    """Before a market's scan: news on its stocks not held; returns the buy signals."""
    today = ctx.clock().astimezone(BERLIN).date()
    held = {h.bracket.instrument_id for h in await held_positions(ctx)}
    async with ctx.trade.sessions() as s:
        found = await market_repo.active_instruments(s, market)
    # benchmarks (no index membership) aren't traded
    stocks = [i for k, i in found.items() if k not in held and i.indices]
    await fetch(ctx, stocks, today)
    items: list[Item] = []
    for inst in stocks:
        if inst.id is not None and (new := await _new_headlines(ctx, inst.id)):
            stock = stock_news(inst.yahoo_symbol, inst.name, inst.market, inst.sector, new)
            items.append(Item(inst, stock, new))
    return await _ask(ctx, items, today, market)


async def _ask(ctx: NewsContext, items: Sequence[Item], today: date, scope: str) -> list[str]:
    """News agent calls in batches; returns the symbols with an action above its threshold."""
    acted: list[str] = []
    size = ctx.params.batch
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        inp = NewsInput(as_of=today, stocks=[x.stock for x in batch])
        outcome = await ctx.runner.run(
            ctx.agent, inp, instrument_id=None, as_of=today, subject=f"{scope} {start // size + 1}"
        )
        if outcome.status != "ok" or outcome.output is None:
            log.warning("news.agent_failed", scope=scope, status=outcome.status)
            continue
        verdicts = {v.symbol: v for v in outcome.output.stocks}
        for x in batch:
            v = verdicts.get(x.instrument.yahoo_symbol)
            if v is None:
                continue
            await _store_triage(ctx, x, v)
            if await _act(ctx, x, v, outcome.analysis_id, today):
                acted.append(x.instrument.yahoo_symbol)
    log.info("news.done", scope=scope, stocks=len(items), acted=acted)
    return acted


async def _store_triage(ctx: NewsContext, x: Item, v: StockVerdict) -> None:
    by_ref = {h.ref: n for h, n in zip(x.stock.headlines, x.new, strict=True)}
    async with ctx.trade.sessions.begin() as s:
        for t in v.items:
            if t.ref in by_ref:
                await news_repo.set_triage(s, by_ref[t.ref].id, t.relevance, t.note)
    if x.held is None:
        return
    for t in v.items:
        if t.relevance == "high" and t.ref in by_ref:
            headline = clean(by_ref[t.ref].headline, 160)
            await ctx.trade.notifier.send(
                messages.news_alert(x.instrument.yahoo_symbol, headline, clean(t.note, 200))
            )


async def _act(
    ctx: NewsContext, x: Item, v: StockVerdict, analysis_id: int | None, today: date
) -> bool:
    symbol, reason = x.instrument.yahoo_symbol, clean(v.reason, 600)
    if x.held is None and v.action != "buy":
        return False
    sure = v.confidence >= (
        ctx.params.exit_confidence if x.held is not None else ctx.params.buy_confidence
    )
    action = "hold" if v.action == "none" else v.action
    status: StepStatus = "passed" if v.action != "none" and sure else "info"
    error: str | None = None
    if x.held is None:
        if sure:
            await ctx.trade.notifier.send(
                messages.news_buy_alert(symbol, x.instrument.market, v.confidence, reason)
            )
    elif v.action == "exit" and not sure:
        await ctx.trade.notifier.send(
            messages.news_exit_advice(symbol, v.confidence, reason, ctx.params.exit_confidence)
        )
    elif v.action == "exit" and not ctx.trade.connected():
        status, error = "error", "IB Gateway not connected"
        await ctx.trade.notifier.send(messages.not_connected_alert(f"News exit {symbol}", [symbol]))
    elif v.action == "exit":
        try:
            await ctx.trade.executor.request_exit(x.held.bracket.id, "news")
        except Exception as exc:  # the exit step holds the broker's error
            log.exception("news.exit_failed", symbol=symbol)
            status, error = "error", describe_error(exc)
        else:
            await ctx.trade.notifier.send(messages.news_exit_alert(symbol, v.confidence, reason))
    step = Step(
        day=today,
        market=x.instrument.market,
        instrument_id=x.instrument.id,
        symbol=symbol,
        step="news",
        status=status,
        summary=f"{action}, confidence {v.confidence:.2f}: {reason}",
        input={"headlines": [n.headline for n in x.new]},
        output=v.model_dump(mode="json"),
        error=error,
        analysis_id=analysis_id,
        proposal_id=x.held.bracket.id if x.held is not None else None,
    )
    async with ctx.trade.sessions.begin() as s:
        await steps_repo.add_steps(s, [step])
    return status == "passed"
