"""The data jobs (prices after each close, macro, earnings, backfill, quality checks) and the
yearly tax report."""

from collections import Counter
from datetime import UTC, date, datetime

import structlog

from trading_agent import trading
from trading_agent.contexts import BookContext, DataContext
from trading_agent.data import ingest
from trading_agent.data.ingest import AsyncPriceProvider, IngestResult, PriceProvider
from trading_agent.data.quality import QualityIssue
from trading_agent.data.universe import Universe
from trading_agent.db import reports as reports_repo
from trading_agent.domain.market import Market
from trading_agent.domain.trading import Book
from trading_agent.notify import messages
from trading_agent.portfolio import tax

log = structlog.get_logger(__name__)


def _log_result(result: IngestResult) -> None:
    event = "ingest.failed_items" if result.failed else "ingest.done"
    log.info(
        event,
        job=result.job,
        inserted=result.inserted,
        updated=result.updated,
        removed=result.removed,
        up_to_date=result.up_to_date,
        restated=result.restated,
        failed=result.failed,
    )


def _log_quality(issues: list[QualityIssue], market: Market | None) -> None:
    blocked = sorted({i.yahoo_symbol for i in issues if i.blocking})
    by_check = Counter(i.check for i in issues)
    level = log.warning if blocked else log.info
    level("quality.checked", market=market, issues=dict(by_check), blocked=blocked)


async def _report(ctx: DataContext, result: IngestResult) -> None:
    _log_result(result)
    if alert := messages.data_alert(result.job, result.failed, result.restated):
        await ctx.notifier.send(alert)


async def _check_quality(ctx: DataContext, market: Market, now: datetime) -> None:
    issues = await ingest.run_quality(ctx.sessions, ctx.cfg.quality, now=now, market=market)
    _log_quality(issues, market)
    blocked: dict[str, list[str]] = {}
    for issue in issues:
        if issue.blocking:
            blocked.setdefault(issue.yahoo_symbol, []).append(issue.check)
    previous = set(ctx.state.blocked.get(market, [])) if ctx.state else set[str]()
    if ctx.state is not None:
        ctx.state.blocked[market] = sorted(blocked)
    new = {k: v for k, v in blocked.items() if k not in previous}  # alert once per symbol
    if alert := messages.quality_alert(market, new):
        await ctx.notifier.send(alert)


def price_source(ctx: DataContext, market: Market) -> PriceProvider | AsyncPriceProvider:
    """config/data.yaml prices.sources; Yahoo whenever the gateway isn't connected."""
    if ctx.cfg.prices.sources.get(market) != "ibkr":
        return ctx.yahoo
    if ctx.ibkr is None or not ctx.ibkr.ib.isConnected():
        log.warning("prices.ibkr_unavailable", market=market, fallback="yahoo")
        return ctx.yahoo
    return ctx.ibkr


async def eod(ctx: DataContext, market: Market, now: datetime | None = None) -> list[IngestResult]:
    """After an exchange closes: new bars (+ ECB FX for the EU close), then quality checks."""
    now = now or datetime.now(UTC)
    results = [
        await ingest.ingest_prices(
            ctx.sessions, price_source(ctx, market), ctx.cfg.prices, now=now, market=market
        )
    ]
    if market == "EU":
        results.append(
            await ingest.ingest_fx(
                ctx.sessions,
                ctx.ecb,
                ctx.cfg.fx,
                now=now,
                backfill_years=ctx.cfg.prices.backfill_years,
            )
        )
    for r in results:
        await _report(ctx, r)
    await _check_quality(ctx, market, now)
    return results


async def macro(ctx: DataContext, now: datetime | None = None) -> IngestResult:
    result = await ingest.ingest_macro(
        ctx.sessions,
        [(ctx.fred, {s: s for s in ctx.cfg.macro.fred}), (ctx.ecb, ctx.cfg.macro.ecb)],
        now=now or datetime.now(UTC),
        backfill_years=ctx.cfg.prices.backfill_years,
    )
    await _report(ctx, result)
    return result


async def earnings(
    ctx: DataContext, now: datetime | None = None, limit: int | None = None
) -> IngestResult:
    result = await ingest.ingest_earnings(
        ctx.sessions,
        ctx.yahoo,
        now=now or datetime.now(UTC),
        limit=limit or ctx.cfg.earnings.daily_limit,
    )
    await _report(ctx, result)
    return result


async def backfill(
    ctx: DataContext, universe: Universe, market: Market | None = None
) -> list[IngestResult]:
    """Everything, idempotent: a second run right after the first changes nothing."""
    now = datetime.now(UTC)
    results = [
        await ingest.sync_universe(ctx.sessions, universe),
        await ingest.ingest_prices(ctx.sessions, ctx.yahoo, ctx.cfg.prices, now=now, market=market),
        await ingest.ingest_fx(
            ctx.sessions, ctx.ecb, ctx.cfg.fx, now=now, backfill_years=ctx.cfg.prices.backfill_years
        ),
        await macro(ctx, now),
        await earnings(ctx, now, limit=ctx.cfg.earnings.history_limit),
    ]
    for r in results[:3]:  # macro() and earnings() log their own results
        _log_result(r)
    return results


async def tax_report(ctx: BookContext, year: int, book: Book = "agent_live") -> str:
    """The yearly tax helper for one book, stored as report `tax:<book>` at 31 Dec."""
    brackets, fills, instruments = await trading.load_book(ctx.sessions, book, ctx.fees)
    body = tax.render(year, book, tax.sales([b for b, _ in brackets], fills, instruments, year))
    async with ctx.sessions.begin() as s:
        await reports_repo.save_report(s, f"tax:{book}", date(year, 12, 31), body)
    return body
