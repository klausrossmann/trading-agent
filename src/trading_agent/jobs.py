"""Scheduled and CLI-triggered jobs (orchestration layer): wires providers, config and DB."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import structlog

from trading_agent import backtest
from trading_agent.calc.fees import FeeSchedule
from trading_agent.data import ingest
from trading_agent.data.ingest import IngestResult, Sessions
from trading_agent.data.quality import QualityIssue
from trading_agent.data.series import EcbProvider, FredProvider
from trading_agent.data.universe import Universe
from trading_agent.data.yahoo import YahooProvider
from trading_agent.db import trades as trades_repo
from trading_agent.domain.market import Market
from trading_agent.risk.config import RiskConfig, load_risk_config
from trading_agent.settings import DataConfig, Settings, load_fees

log = structlog.get_logger(__name__)


@dataclass
class DataContext:
    sessions: Sessions
    cfg: DataConfig
    yahoo: YahooProvider
    ecb: EcbProvider
    fred: FredProvider | None

    @classmethod
    def build(
        cls, settings: Settings, cfg: DataConfig, sessions: Sessions, http: httpx.AsyncClient
    ) -> "DataContext":
        key = settings.fred_api_key
        fred = FredProvider(http, key) if key is not None and key.get_secret_value() else None
        return cls(sessions, cfg, YahooProvider(), EcbProvider(http), fred)


@dataclass
class BookContext:
    sessions: Sessions
    cfg: backtest.BacktestConfig
    risk: RiskConfig
    fees: FeeSchedule
    benchmarks: dict[Market, str]

    @classmethod
    def load(cls, config_dir: Path, sessions: Sessions, universe: Universe) -> "BookContext":
        return cls(
            sessions,
            backtest.load_backtest_config(config_dir),
            load_risk_config(config_dir),
            load_fees(config_dir),
            dict(universe.benchmarks),
        )


async def baseline_book(
    ctx: BookContext, today: date | None = None
) -> backtest.BacktestResult | None:
    """Replay the forward `baseline_sim` book from its start date and store its trades."""
    today = today or datetime.now(UTC).date()
    start = ctx.cfg.baseline_book.start
    if today < start:
        log.info("baseline_sim.not_started", start=start.isoformat())
        return None
    data = await backtest.load_market_data(ctx.sessions, ctx.benchmarks, ctx.cfg.pullback)
    result = backtest.run(
        data,
        ctx.cfg,
        ctx.risk,
        ctx.fees,
        start=start,
        end=today,
        markets=ctx.risk.markets.paper,
        book="baseline_sim",
    )
    async with ctx.sessions.begin() as s:
        await trades_repo.replace_book(s, "baseline_sim", result.trades)
    log.info(
        "baseline_sim.updated",
        trades=len(result.trades),
        open=sum(1 for t in result.trades if t.exit_date is None),
        equity_eur=round(float(result.equity.iloc[-1]), 2) if not result.equity.empty else None,
    )
    return result


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


async def eod(ctx: DataContext, market: Market, now: datetime | None = None) -> list[IngestResult]:
    """After an exchange closes: new bars (+ ECB FX for the EU close), then quality checks."""
    now = now or datetime.now(UTC)
    results = [
        await ingest.ingest_prices(ctx.sessions, ctx.yahoo, ctx.cfg.prices, now=now, market=market)
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
        _log_result(r)
    _log_quality(
        await ingest.run_quality(ctx.sessions, ctx.cfg.quality, now=now, market=market), market
    )
    return results


async def macro(ctx: DataContext, now: datetime | None = None) -> IngestResult:
    result = await ingest.ingest_macro(
        ctx.sessions,
        [(ctx.fred, {s: s for s in ctx.cfg.macro.fred}), (ctx.ecb, ctx.cfg.macro.ecb)],
        now=now or datetime.now(UTC),
        backfill_years=ctx.cfg.prices.backfill_years,
    )
    _log_result(result)
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
    _log_result(result)
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
    for r in results[:3]:
        _log_result(r)
    return results
