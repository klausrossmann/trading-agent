"""The data jobs: prices after each close, macro, earnings, backfill, quality checks."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx
import structlog

from trading_agent.core.db import Sessions
from trading_agent.core.settings import DataConfig, Settings
from trading_agent.marketdata import ingest
from trading_agent.marketdata.ingest import AsyncPriceProvider, IngestResult, PriceProvider
from trading_agent.marketdata.quality import QualityIssue
from trading_agent.marketdata.series import EcbProvider, FredProvider
from trading_agent.marketdata.types import Market
from trading_agent.marketdata.universe import Universe
from trading_agent.marketdata.yahoo import YahooProvider
from trading_agent.telegram import messages
from trading_agent.telegram.bot import LogNotifier, Notifier

log = structlog.get_logger(__name__)


class LivePrices(AsyncPriceProvider, Protocol):
    """Bars from the broker (IBKR), usable only while connected."""

    def connected(self) -> bool: ...


@dataclass
class DataContext:
    """What the data jobs need: the database, config/data.yaml and the data sources."""

    sessions: Sessions
    cfg: DataConfig
    yahoo: YahooProvider
    ecb: EcbProvider
    fred: FredProvider | None
    notifier: Notifier = field(default_factory=LogNotifier)
    blocked: dict[str, list[str]] = field(default_factory=dict[str, list[str]])  # for /status
    ibkr: LivePrices | None = None

    @classmethod
    def build(
        cls,
        settings: Settings,
        cfg: DataConfig,
        sessions: Sessions,
        http: httpx.AsyncClient,
        notifier: Notifier | None = None,
    ) -> "DataContext":
        key = settings.fred_api_key
        fred = FredProvider(http, key) if key is not None and key.get_secret_value() else None
        return cls(
            sessions, cfg, YahooProvider(), EcbProvider(http), fred, notifier or LogNotifier()
        )


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
    previous = set(ctx.blocked.get(market, []))
    ctx.blocked[market] = sorted(blocked)
    new = {k: v for k, v in blocked.items() if k not in previous}  # alert once per symbol
    if alert := messages.quality_alert(market, new):
        await ctx.notifier.send(alert)


def price_source(ctx: DataContext, market: Market) -> PriceProvider | AsyncPriceProvider:
    """config/data.yaml prices.sources; Yahoo whenever the gateway isn't connected."""
    if ctx.cfg.prices.sources.get(market) != "ibkr":
        return ctx.yahoo
    if ctx.ibkr is None or not ctx.ibkr.connected():
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
