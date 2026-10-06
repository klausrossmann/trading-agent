"""News for held positions with triage, and the daily position review (IMPLEMENTATION.md 14.1;
orchestration). Advice only (19.2 #14): a suggested exit goes to Telegram; `/exit SYMBOL` is
yours to send.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import structlog

from trading_agent import trading
from trading_agent.calc.indicators import atr, bars_to_frame, sma
from trading_agent.data import calendars
from trading_agent.data.news import FinnhubNews
from trading_agent.db import market as market_repo
from trading_agent.db import news as news_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.domain.market import Bar, Instrument
from trading_agent.domain.orders import Bracket
from trading_agent.domain.proposals import Proposal
from trading_agent.llm.runner import LlmRunner
from trading_agent.llm.sanitize import clean
from trading_agent.modules.news_triage import NewsTriageModule, triage_input
from trading_agent.modules.position_review import (
    PositionFacts,
    PositionReviewModule,
    review_input,
)
from trading_agent.notify import messages

log = structlog.get_logger(__name__)

NEWS_DAYS = 3  # look-back of each news request
REVIEW_NEWS_DAYS = 5  # triaged news shown to the review
REPORT_SESSIONS = 3  # an upcoming report this close triggers a review
HISTORY_DAYS = 120


@dataclass
class ReviewContext:
    trade: trading.TradingContext
    runner: LlmRunner
    triage: NewsTriageModule
    review: PositionReviewModule
    news: FinnhubNews | None  # None without FINNHUB_API_KEY
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Held:
    bracket: Bracket
    instrument: Instrument
    proposal: Proposal | None
    entered: date


async def held_positions(ctx: ReviewContext) -> list[Held]:
    t = ctx.trade
    brackets, fills, instruments = await trading.load_book(t.sessions, t.book, t.fees)
    entered = {f.bracket_id: f.day for f in reversed(fills) if f.kind == "entry"}
    held = [b for b, _ in brackets if b.open_qty > 0 and b.id in entered]
    async with t.sessions() as s:
        proposals = await proposals_repo.by_ids(s, [b.id for b in held])
    return [Held(b, instruments[b.instrument_id], proposals.get(b.id), entered[b.id]) for b in held]


async def ingest_news(ctx: ReviewContext) -> int:
    """Every 30 minutes: new headlines for held positions, triaged; 📰 alert for high ones."""
    if ctx.news is None:
        log.info("news.skipped", reason="FINNHUB_API_KEY not set")
        return 0
    today = ctx.clock().date()
    held = [h for h in await held_positions(ctx) if ctx.news.covers(h.instrument)]
    added = 0
    for h in held:
        try:
            items = await ctx.news.company_news(h.instrument, today - timedelta(NEWS_DAYS), today)
        except httpx.HTTPError as exc:
            log.warning("news.fetch_failed", symbol=h.instrument.yahoo_symbol, error=repr(exc))
            continue
        async with ctx.trade.sessions.begin() as s:
            added += await news_repo.add_news(s, items)
    for h in held:
        await _triage(ctx, h, today)
    return added


async def _triage(ctx: ReviewContext, h: Held, today: date) -> None:
    inst_id = h.bracket.instrument_id
    async with ctx.trade.sessions() as s:
        pending = await news_repo.untriaged(s, [inst_id])
    if not pending or h.proposal is None:
        return
    inp = triage_input(
        h.instrument.yahoo_symbol,
        h.instrument.sector,
        h.proposal.thesis,
        h.proposal.invalidation,
        pending,
    )
    outcome = await ctx.runner.run(
        ctx.triage, inp, instrument_id=inst_id, as_of=today, subject=h.instrument.yahoo_symbol
    )
    if outcome.status != "ok" or outcome.output is None:
        log.warning("news.triage_failed", symbol=h.instrument.yahoo_symbol, status=outcome.status)
        return
    by_ref = {f"n{i}": n for i, n in enumerate(pending, start=1)}
    async with ctx.trade.sessions.begin() as s:
        for t in outcome.output.items:
            if t.ref in by_ref:
                await news_repo.set_triage(s, by_ref[t.ref].id, t.relevance, t.note)
    for t in outcome.output.items:
        if t.relevance == "high" and t.ref in by_ref:
            headline = clean(by_ref[t.ref].headline, 160)
            await ctx.trade.notifier.send(
                messages.news_alert(h.instrument.yahoo_symbol, headline, clean(t.note, 200))
            )


def _last(series: pd.Series) -> float | None:
    value = float(series.iloc[-1])
    return round(value, 2) if math.isfinite(value) else None


def _facts(h: Held, bars: list[Bar], as_of: date, report: date | None) -> PositionFacts:
    frame = bars_to_frame(bars)
    close = frame["close"]
    b = h.bracket
    entry = float(b.entry_price or b.entry)
    risk = float(b.entry - b.initial_stop)
    last_close = float(close.iloc[-1])
    cal = calendars.CALENDAR_BY_MARKET[h.instrument.market]
    return PositionFacts(
        symbol=h.instrument.yahoo_symbol,
        sector=h.instrument.sector,
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
    )


async def reevaluate_positions(ctx: ReviewContext) -> list[str]:
    """Before the US close: positions with high-relevance news or a report within 3 sessions
    get a review; a broken thesis is reported with the way out (/exit SYMBOL)."""
    now = ctx.clock()
    today = now.date()
    suggested: list[str] = []
    for h in await held_positions(ctx):
        if h.bracket.state != "filled" or h.proposal is None:
            continue
        inst_id = h.bracket.instrument_id
        async with ctx.trade.sessions() as s:
            news = await news_repo.recent(s, inst_id, now - timedelta(days=REVIEW_NEWS_DAYS))
            bars = await market_repo.bars(s, inst_id, today - timedelta(days=HISTORY_DAYS))
            dates = (await market_repo.earnings_dates(s, [inst_id])).get(inst_id, [])
        report = next((d for d in dates if d >= today), None)
        if not bars:
            continue
        facts = _facts(h, bars, bars[-1].date, report)
        fresh_high = any(n.relevance == "high" and n.ts >= now - timedelta(days=1) for n in news)
        near_report = (
            facts.sessions_to_report is not None and facts.sessions_to_report <= REPORT_SESSIONS
        )
        if not (fresh_high or near_report):
            continue
        relevant = [n for n in news if n.relevance in ("low", "high")]
        inp = review_input(facts, h.proposal.thesis, h.proposal.invalidation, relevant)
        outcome = await ctx.runner.run(
            ctx.review, inp, instrument_id=inst_id, as_of=facts.as_of, subject=facts.symbol
        )
        out = outcome.output
        if outcome.status != "ok" or out is None:
            log.warning("review.failed", symbol=facts.symbol, status=outcome.status)
            continue
        log.info("review.done", symbol=facts.symbol, verdict=out.verdict, confidence=out.confidence)
        if out.verdict == "exit":
            suggested.append(facts.symbol)
            await ctx.trade.notifier.send(
                messages.review_alert(facts.symbol, out.confidence, clean(out.reasons, 600))
            )
    return suggested
