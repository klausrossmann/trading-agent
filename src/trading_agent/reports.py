"""Weekly report job and CLI (IMPLEMENTATION.md 14; orchestration)."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd
import structlog

from trading_agent import jobs, trading
from trading_agent.db import book as book_repo
from trading_agent.db import market as market_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import reports as reports_repo
from trading_agent.db import trades as trades_repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.trading import Book, Trade
from trading_agent.evaluation import weekly
from trading_agent.evaluation.kpi import max_drawdown_pct
from trading_agent.notify.telegram import Notifier
from trading_agent.portfolio import tax

log = structlog.get_logger(__name__)

BOOKS: tuple[Book, ...] = ("agent_paper", "agent_shadow", "baseline_sim")


async def _usd_per_eur(ctx: jobs.BookContext) -> Decimal:
    async with ctx.sessions() as s:
        rates = await market_repo.fx_rates(s, "USD")
    return rates[-1].value if rates else Decimal(1)


async def _benchmarks(ctx: jobs.BookContext, start: date) -> dict[str, float]:
    async with ctx.sessions() as s:
        by_symbol = {
            i.yahoo_symbol: k for k, i in (await market_repo.active_instruments(s)).items()
        }
        out: dict[str, float] = {}
        for symbol in ctx.benchmarks.values():
            if symbol not in by_symbol:
                continue
            bars = await market_repo.bars(s, by_symbol[symbol], start)
            if len(bars) >= 2:
                out[symbol] = float(bars[-1].close / bars[0].close - 1) * 100
    return out


async def _drawdowns(ctx: jobs.BookContext) -> dict[str, float]:
    out: dict[str, float] = {}
    for markets, cfg in ctx.risk.sleeves(list(ctx.risk.markets.paper), "paper"):
        key = ",".join(markets)
        async with ctx.sessions() as s:
            history = await book_repo.equity_history(s, "agent_paper", key)
        if history:
            values = [float(cfg.capital.agent_budget_eur), *(float(e) for _, e in history)]
            out[key] = max_drawdown_pct(pd.Series(values))
    return out


async def weekly_input(ctx: jobs.BookContext, week_end: date) -> weekly.WeeklyInput:
    start = ctx.cfg.baseline_book.start
    async with ctx.sessions() as s:
        books: dict[str, list[Trade]] = {b: await trades_repo.trades(s, b) for b in BOOKS}
        found = await proposals_repo.proposals(s, start=start)
        labels = await proposals_repo.labels(s, [p.id for p in found])
    by_signal = {(p.instrument_id, p.as_of): p for p in found}
    outcomes: list[weekly.Outcome] = []
    for t in weekly.agent_sample(books["agent_paper"], books["agent_shadow"]):
        p = by_signal.get((t.instrument_id, t.signal_date))
        if t.pnl_net_eur is None:
            continue
        label = labels.get(p.id) if p else None
        outcomes.append(
            weekly.Outcome(
                confidence=p.confidence if p else None,
                label=label[0] if label else None,
                pnl_eur=t.pnl_net_eur,
            )
        )
    store = DbAnalysisStore(ctx.sessions)
    end = datetime.combine(week_end + timedelta(days=1), time(), UTC)
    usd = float(await _usd_per_eur(ctx))
    total = await store.spent_usd(datetime.combine(start, time(), UTC))
    week = await store.spent_usd(end - timedelta(days=7))
    return weekly.WeeklyInput(
        week_end=week_end,
        start=start,
        books=books,
        outcomes=outcomes,
        llm_eur_week=week / usd,
        llm_eur_total=total / usd,
        benchmarks=await _benchmarks(ctx, start),
        drawdown_pct=await _drawdowns(ctx),
    )


async def weekly_report(
    ctx: jobs.BookContext, notifier: Notifier, today: date | None = None
) -> str:
    """Saturday: store the Markdown report and send the summary."""
    week_end = today or datetime.now(UTC).date()
    w = await weekly_input(ctx, week_end)
    body = weekly.render(w)
    async with ctx.sessions.begin() as s:
        await reports_repo.save_report(s, "weekly", week_end, body)
    await notifier.send(weekly.summary(w))
    log.info("weekly_report.stored", week_end=week_end.isoformat())
    return body


async def tax_report(ctx: jobs.BookContext, year: int, book: Book = "agent_live") -> str:
    """The yearly tax helper for one book, stored as report `tax:<book>` at 31 Dec."""
    brackets, fills, instruments = await trading.load_book(ctx.sessions, book, ctx.fees)
    body = tax.render(year, book, tax.sales([b for b, _ in brackets], fills, instruments, year))
    async with ctx.sessions.begin() as s:
        await reports_repo.save_report(s, f"tax:{book}", date(year, 12, 31), body)
    return body
