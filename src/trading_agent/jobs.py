"""Scheduled and CLI-triggered jobs (orchestration layer): wires providers, config and DB."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import structlog

from trading_agent import backtest
from trading_agent.calc.fees import FeeSchedule
from trading_agent.calc.indicators import bars_to_frame
from trading_agent.calc.trend import trend_states
from trading_agent.data import calendars, ingest
from trading_agent.data.ingest import IngestResult, Sessions
from trading_agent.data.quality import QualityIssue
from trading_agent.data.series import EcbProvider, FredProvider
from trading_agent.data.universe import Universe
from trading_agent.data.yahoo import YahooProvider
from trading_agent.db import market as market_repo
from trading_agent.db import trades as trades_repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.market import Market
from trading_agent.llm.budget import BudgetMode, month_start
from trading_agent.llm.models import ModelsConfig, build_model, load_models_config
from trading_agent.llm.prompts import load_prompt
from trading_agent.llm.runner import LlmRunner, Outcome
from trading_agent.modules import earnings as earnings_module
from trading_agent.modules import technical as technical_module
from trading_agent.modules.earnings import EarningsAssessment, EarningsInput, EarningsModule
from trading_agent.modules.history import load_history
from trading_agent.modules.technical import (
    PlanRules,
    TechnicalAssessment,
    TechnicalInput,
    TechnicalModule,
)
from trading_agent.notify import messages
from trading_agent.notify.telegram import LogNotifier, Notifier
from trading_agent.risk.config import RiskConfig, load_risk_config
from trading_agent.settings import DataConfig, Settings, load_fees

log = structlog.get_logger(__name__)


@dataclass
class RuntimeState:
    """What /status reports; updated by the running jobs."""

    mode: str
    started_at: datetime
    heartbeat_at: datetime | None = None
    heartbeat_ok: bool | None = None
    blocked: dict[str, list[str]] = field(default_factory=dict[str, list[str]])  # market -> symbols
    scheduler: Any = None  # AsyncIOScheduler, set once built


@dataclass
class DataContext:
    sessions: Sessions
    cfg: DataConfig
    yahoo: YahooProvider
    ecb: EcbProvider
    fred: FredProvider | None
    notifier: Notifier = field(default_factory=LogNotifier)
    state: RuntimeState | None = None

    @classmethod
    def build(
        cls,
        settings: Settings,
        cfg: DataConfig,
        sessions: Sessions,
        http: httpx.AsyncClient,
        notifier: Notifier | None = None,
        state: RuntimeState | None = None,
    ) -> "DataContext":
        key = settings.fred_api_key
        fred = FredProvider(http, key) if key is not None and key.get_secret_value() else None
        return cls(
            sessions,
            cfg,
            YahooProvider(),
            EcbProvider(http),
            fred,
            notifier or LogNotifier(),
            state,
        )


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
) -> list[backtest.BacktestResult]:
    """Replay the forward `baseline_sim` book from its start date and store its trades.

    One simulated account per paper budget sleeve (US EUR 1,000, EU EUR 5,000).
    """
    today = today or datetime.now(UTC).date()
    start = ctx.cfg.baseline_book.start
    if today < start:
        log.info("baseline_sim.not_started", start=start.isoformat())
        return []
    data = await backtest.load_market_data(ctx.sessions, ctx.benchmarks, ctx.cfg.pullback)
    results: list[backtest.BacktestResult] = []
    for markets, risk in ctx.risk.sleeves(list(ctx.risk.markets.paper), "paper"):
        result = backtest.run(
            data,
            ctx.cfg,
            risk,
            ctx.fees,
            start=start,
            end=today,
            markets=markets,
            book="baseline_sim",
        )
        results.append(result)
        log.info(
            "baseline_sim.updated",
            markets=markets,
            trades=len(result.trades),
            open=sum(1 for t in result.trades if t.exit_date is None),
            equity_eur=round(float(result.equity.iloc[-1]), 2) if not result.equity.empty else None,
        )
    async with ctx.sessions.begin() as s:
        await trades_repo.replace_book(s, "baseline_sim", [t for r in results for t in r.trades])
    return results


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
    for r in results[:3]:
        _log_result(r)
    return results


# --- briefing and status ---


def is_trading_day(day: date) -> bool:
    return any(calendars.session_time(c, day, "open") is not None for c in ("XETR", "XNYS"))


async def _last_bars_by_market(sessions: Sessions) -> dict[str, date | None]:
    async with sessions() as s:
        instruments = await market_repo.active_instruments(s)
        last = await market_repo.last_bar_dates(s)
    out: dict[str, date | None] = {"US": None, "EU": None}
    for inst_id, d in last.items():
        inst = instruments.get(inst_id)
        if inst is not None and (out[inst.market] is None or d > (out[inst.market] or d)):
            out[inst.market] = d
    return out


async def build_briefing(
    book: BookContext, state: RuntimeState | None, today: date
) -> messages.Briefing:
    sessions = book.sessions
    async with sessions() as s:
        instruments = await market_repo.active_instruments(s)
        by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
        markets: list[messages.MarketLine] = []
        for symbol in book.benchmarks.values():
            inst_id = by_symbol.get(symbol)
            bars = (
                await market_repo.bars(s, inst_id, today - timedelta(days=450)) if inst_id else []
            )
            if len(bars) >= 2:
                close = bars_to_frame(bars)["close"]
                markets.append(
                    messages.MarketLine(
                        name=symbol,
                        last_date=bars[-1].date,
                        close=float(bars[-1].close),
                        change_pct=float(bars[-1].close / bars[-2].close - 1) * 100,
                        trend=trend_states(close),
                    )
                )
        fx = await market_repo.fx_rates(s, "USD")
        usd_per_eur = float(fx[-1].value) if fx else None
        horizon = max(calendars.session_offset(c, today, 3) for c in ("XNYS", "XETR"))
        upcoming = await market_repo.upcoming_earnings(s, today, horizon)
        rows = await trades_repo.book_trades(s, "baseline_sim")
        last_close: dict[int, float] = {}
        for row in rows:
            if row.exit_date is None and row.instrument_id not in last_close:
                recent = await market_repo.bars(s, row.instrument_id, today - timedelta(days=14))
                if recent:
                    last_close[row.instrument_id] = float(recent[-1].close)

    earnings_lines = [
        messages.EarningsLine(instruments[i].yahoo_symbol, d, timing)
        for i, d, timing in upcoming
        if i in instruments
    ]

    book_lines: list[messages.SleeveLine] | None = None
    start = book.cfg.baseline_book.start
    if today >= start:
        book_lines = []
        for sleeve_markets, risk in book.risk.sleeves(list(book.risk.markets.paper), "paper"):
            mine = [
                r
                for r in rows
                if instruments.get(r.instrument_id)
                and instruments[r.instrument_id].market in sleeve_markets
            ]
            closed = [r for r in mine if r.exit_date is not None]
            open_lines: list[messages.PositionLine] = []
            for r in mine:
                close = last_close.get(r.instrument_id)
                if r.exit_date is not None or close is None:
                    continue
                entry, stop = float(r.entry_price), float(r.stop)
                rate = usd_per_eur if instruments[r.instrument_id].currency == "USD" else 1.0
                open_lines.append(
                    messages.PositionLine(
                        symbol=instruments[r.instrument_id].yahoo_symbol,
                        entry_date=r.entry_date,
                        r_now=(close - entry) / (entry - stop) if entry > stop else 0.0,
                        pnl_eur=r.quantity * (close - entry) / (rate or 1.0) - float(r.fees_eur),
                    )
                )
            book_lines.append(
                messages.SleeveLine(
                    label=", ".join(sleeve_markets),
                    budget_eur=float(risk.capital.agent_budget_eur),
                    closed_trades=len(closed),
                    closed_pnl_eur=sum(float(r.pnl_net_eur or 0) for r in closed),
                    open=open_lines,
                )
            )

    data = await backtest.load_market_data(sessions, book.benchmarks, book.cfg.pullback)
    setups = [symbol for symbol, _ in backtest.latest_setups(data, book.cfg.pullback)]
    blocked = None
    if state is not None and state.blocked:
        blocked = sorted(s for symbols in state.blocked.values() for s in symbols)
    return messages.Briefing(
        day=today,
        markets=markets,
        eur_usd=usd_per_eur,
        earnings=earnings_lines,
        book=book_lines,
        book_start=start,
        setups=setups[:8],
        last_bars=await _last_bars_by_market(sessions),
        blocked=blocked,
    )


async def briefing_text(book: BookContext, state: RuntimeState | None, today: date) -> str:
    return messages.render_briefing(await build_briefing(book, state, today))


async def morning_briefing(
    book: BookContext, state: RuntimeState | None, notifier: Notifier, today: date | None = None
) -> str | None:
    """Scheduled on weekdays; sends only if Xetra or NYSE has a session today."""
    today = today or datetime.now(UTC).date()
    if not is_trading_day(today):
        log.info("briefing.skipped", reason="no session today")
        return None
    text = await briefing_text(book, state, today)
    await notifier.send(text)
    return text


async def status_text(state: RuntimeState, sessions: Sessions, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    hidden = {"heartbeat", "plan_session_jobs"}
    next_jobs: list[tuple[str, datetime]] = []
    if state.scheduler is not None:
        next_jobs = sorted(
            (
                (j.id, j.next_run_time)
                for j in state.scheduler.get_jobs()
                if j.next_run_time and j.id not in hidden
            ),
            key=lambda x: x[1],
        )[:5]
    return messages.render_status(
        messages.StatusSnapshot(
            mode=state.mode,
            started_at=state.started_at,
            now=now,
            heartbeat_at=state.heartbeat_at,
            heartbeat_ok=state.heartbeat_ok,
            last_bars=await _last_bars_by_market(sessions),
            blocked=state.blocked,
            next_jobs=next_jobs,
        )
    )


# --- LLM analysis (M5) ---


@dataclass
class AnalysisContext:
    sessions: Sessions
    models: ModelsConfig
    runner: LlmRunner
    technical: TechnicalModule
    earnings: EarningsModule
    rules: PlanRules
    holding_sessions: int  # entry validity + time stop of the baseline strategy
    pullback: backtest.PullbackParams
    benchmarks: dict[Market, str]

    @classmethod
    def build(cls, settings: Settings, sessions: Sessions, universe: Universe) -> "AnalysisContext":
        cfg_dir = settings.config_dir
        models = load_models_config(cfg_dir)
        runner = LlmRunner(
            models,
            DbAnalysisStore(sessions),
            partial(build_model, gemini_api_key=settings.gemini_api_key),
            dev_overrides=settings.llm_dev_overrides,
        )
        p = backtest.load_backtest_config(cfg_dir).pullback
        risk = load_risk_config(cfg_dir)
        return cls(
            sessions=sessions,
            models=models,
            runner=runner,
            technical=TechnicalModule(
                load_prompt(
                    settings.prompts_dir, technical_module.NAME, technical_module.PROMPT_VERSION
                )
            ),
            earnings=EarningsModule(
                load_prompt(
                    settings.prompts_dir, earnings_module.NAME, earnings_module.PROMPT_VERSION
                )
            ),
            rules=PlanRules(min_risk_reward=float(risk.per_trade.min_risk_reward)),
            holding_sessions=p.entry_valid_sessions + p.time_stop_sessions,
            pullback=p,
            benchmarks=dict(universe.benchmarks),
        )


@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    technical_input: TechnicalInput
    technical: Outcome[TechnicalAssessment]
    earnings_input: EarningsInput
    earnings: Outcome[EarningsAssessment]

    @property
    def outcomes(self) -> tuple[Outcome[Any], ...]:
        return (self.technical, self.earnings)


@dataclass(frozen=True)
class ScanResult:
    mode: BudgetMode
    items: list[SymbolAnalysis]
    missing: list[str]  # requested symbols without stored bars

    @property
    def cost_usd(self) -> float:
        return sum(o.cost_usd for i in self.items for o in i.outcomes)


async def candidates(ctx: AnalysisContext, market: Market | None, top: int) -> list[str]:
    """Top baseline setups at the latest close, by relative strength."""
    data = await backtest.load_market_data(ctx.sessions, ctx.benchmarks, ctx.pullback)
    markets = {x.instrument.yahoo_symbol: x.instrument.market for x in data.instruments.values()}
    setups = backtest.latest_setups(data, ctx.pullback)
    return [s for s, _ in setups if market is None or markets[s] == market][:top]


async def analyse(
    ctx: AnalysisContext,
    symbols: list[str] | None = None,
    market: Market | None = None,
    top: int | None = None,
    today: date | None = None,
) -> ScanResult:
    """Technical and earnings analysis for the given symbols, or for today's top setups."""
    mode = await ctx.runner.mode()
    if symbols is None:
        scan = ctx.models.scan
        n = top or (scan.candidates_lean if mode == "lean" else scan.candidates)
        symbols = await candidates(ctx, market, n)
    async with ctx.sessions() as s:
        instruments = await market_repo.active_instruments(s)
    by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
    today = today or datetime.now(UTC).date()
    items: list[SymbolAnalysis] = []
    missing: list[str] = []
    for symbol in symbols:
        inst_id = by_symbol.get(symbol)
        inst = instruments.get(inst_id) if inst_id is not None else None
        history = (
            await load_history(ctx.sessions, inst_id, today, ctx.benchmarks.get(inst.market))
            if inst_id is not None and inst is not None
            else None
        )
        if history is None:
            missing.append(symbol)
            continue
        t_in = technical_module.compute(
            history.instrument, history.frame, history.benchmark_close, ctx.rules
        )
        t_out = await ctx.runner.run(
            ctx.technical, t_in, instrument_id=history.instrument_id, as_of=history.as_of
        )
        e_in = earnings_module.compute(
            history.instrument, history.frame, history.events, ctx.holding_sessions
        )
        e_out = await ctx.runner.run(
            ctx.earnings, e_in, instrument_id=history.instrument_id, as_of=history.as_of
        )
        items.append(SymbolAnalysis(symbol, t_in, t_out, e_in, e_out))
    result = ScanResult(mode, items, missing)
    outcomes = [o for i in items for o in i.outcomes]
    log.info(
        "scan.done",
        symbols=len(items),
        analyses=len(outcomes),
        cached=sum(1 for o in outcomes if o.cached),
        statuses=dict(Counter(o.status for o in outcomes)),
        cost_usd=round(result.cost_usd, 6),
        budget_mode=mode,
        missing=missing,
    )
    return result


async def budget_text(ctx: AnalysisContext, now: datetime | None = None) -> str:
    since = month_start(now or datetime.now(UTC))
    store = DbAnalysisStore(ctx.sessions)
    spent = await store.spent_usd(since)
    return messages.render_budget(
        messages.BudgetSnapshot(
            month=since.date(),
            spent_usd=spent,
            monthly_usd=ctx.models.budget.monthly_usd,
            mode=await ctx.runner.mode(),
            by_model=await store.spend_by_model(since),
        )
    )
