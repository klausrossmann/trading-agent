"""Scheduled and CLI-triggered jobs (orchestration layer): wires providers, config and DB."""

from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import structlog
from pydantic_ai.models import Model

from trading_agent import screen, telemetry
from trading_agent.agents import critic, proposer
from trading_agent.agents.critic import CriticAgent
from trading_agent.agents.proposer import ProposerAgent
from trading_agent.calc.fees import FeeSchedule
from trading_agent.calc.indicators import bars_to_frame
from trading_agent.calc.trend import trend_states
from trading_agent.data import calendars, ingest
from trading_agent.data.ibkr import IbkrPriceProvider
from trading_agent.data.ingest import AsyncPriceProvider, IngestResult, PriceProvider, Sessions
from trading_agent.data.quality import QualityIssue
from trading_agent.data.series import EcbProvider, FredProvider
from trading_agent.data.universe import Universe
from trading_agent.data.yahoo import YahooProvider
from trading_agent.db import market as market_repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.market import Market
from trading_agent.domain.risk import Mode
from trading_agent.domain.trading import Book, book_for_mode
from trading_agent.llm.budget import BudgetMode, month_start
from trading_agent.llm.fake import fake_model
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
from trading_agent.settings import DataConfig, QualityConfig, Settings, load_data_config, load_fees
from trading_agent.strategies.news import NewsParams, load_news_params
from trading_agent.strategies.pullback import PullbackParams, load_params

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
    gateway: Callable[[], str] | None = None  # IB Gateway status, when enabled
    kill_switch: Callable[[], Awaitable[str]] | None = None
    interlock: Callable[[], str | None] | None = None
    tracing: str = "off"
    trace_ui_url: str = ""
    dashboard_ui_url: str = ""


@dataclass
class DataContext:
    sessions: Sessions
    cfg: DataConfig
    yahoo: YahooProvider
    ecb: EcbProvider
    fred: FredProvider | None
    notifier: Notifier = field(default_factory=LogNotifier)
    state: RuntimeState | None = None
    ibkr: IbkrPriceProvider | None = None

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
    pullback: PullbackParams
    risk: RiskConfig
    fees: FeeSchedule
    benchmarks: dict[Market, str]
    mode: Mode = "paper"
    news: NewsParams = field(default_factory=NewsParams)

    @classmethod
    def load(
        cls, config_dir: Path, sessions: Sessions, universe: Universe, mode: Mode = "paper"
    ) -> "BookContext":
        return cls(
            sessions,
            load_params(config_dir),
            load_risk_config(config_dir),
            load_fees(config_dir),
            dict(universe.benchmarks),
            mode,
            load_news_params(config_dir),
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
        if inst is not None:
            current = out[inst.market]
            out[inst.market] = d if current is None else max(current, d)
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

    earnings_lines = [
        messages.EarningsLine(instruments[i].yahoo_symbol, d, timing)
        for i, d, timing in upcoming
        if i in instruments
    ]
    data = await screen.load_market_data(sessions, book.benchmarks, book.pullback)
    setups = [r.symbol for r in screen.check(data, book.pullback) if r.passed]
    blocked = None
    if state is not None and state.blocked:
        blocked = sorted(s for symbols in state.blocked.values() for s in symbols)
    return messages.Briefing(
        day=today,
        markets=markets,
        eur_usd=usd_per_eur,
        earnings=earnings_lines,
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
            **({"gateway": state.gateway()} if state.gateway else {}),
            **({"kill_switch": await state.kill_switch()} if state.kill_switch else {}),
            interlock=state.interlock() if state.interlock else None,
            tracing=state.tracing,
            trace_ui_url=state.trace_ui_url,
            dashboard_ui_url=state.dashboard_ui_url,
        )
    )


# --- LLM analysis (M5) ---


@dataclass
class AnalysisContext:
    sessions: Sessions
    book: Book  # the agent's own book: its open positions are not proposed again
    models: ModelsConfig
    runner: LlmRunner
    technical: TechnicalModule
    earnings: EarningsModule
    rules: PlanRules
    holding_sessions: int  # entry validity + time stop
    pullback: PullbackParams
    benchmarks: dict[Market, str]
    proposer: ProposerAgent
    critic: CriticAgent
    quality: QualityConfig
    blacklist: frozenset[str]
    news: NewsParams = field(default_factory=NewsParams)

    @classmethod
    def build(cls, settings: Settings, sessions: Sessions, universe: Universe) -> "AnalysisContext":
        cfg_dir = settings.config_dir
        prompts = settings.prompts_dir
        models = load_models_config(cfg_dir)
        factory: Callable[[str], Model] = (
            (lambda _: fake_model())
            if settings.llm_fake
            else partial(build_model, gemini_api_key=settings.gemini_api_key)
        )
        runner = LlmRunner(
            models,
            DbAnalysisStore(sessions),
            factory,
            dev_overrides=settings.llm_dev_overrides,
            tracer_provider=telemetry.provider(),
            trace_content=settings.trace_content,
        )
        p = load_params(cfg_dir)
        risk = load_risk_config(cfg_dir)
        return cls(
            sessions=sessions,
            book=book_for_mode(settings.app_mode),
            models=models,
            runner=runner,
            technical=TechnicalModule(
                load_prompt(prompts, technical_module.NAME, technical_module.PROMPT_VERSION)
            ),
            earnings=EarningsModule(
                load_prompt(prompts, earnings_module.NAME, earnings_module.PROMPT_VERSION)
            ),
            rules=PlanRules(
                min_risk_reward=float(risk.per_trade.min_risk_reward),
                stop_atr_min=float(risk.per_trade.stop_atr_min),
                stop_atr_max=float(risk.per_trade.stop_atr_max),
            ),
            holding_sessions=p.entry_valid_sessions + p.time_stop_sessions,
            pullback=p,
            benchmarks=dict(universe.benchmarks),
            proposer=ProposerAgent(load_prompt(prompts, proposer.NAME, proposer.PROMPT_VERSION)),
            critic=CriticAgent(load_prompt(prompts, critic.NAME, critic.PROMPT_VERSION)),
            quality=load_data_config(cfg_dir).quality,
            blacklist=frozenset(risk.instruments.blacklist),
            news=load_news_params(cfg_dir),
        )


@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    instrument_id: int
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


async def screen_stocks(
    ctx: AnalysisContext,
    market: Market | None,
    top: int,
    held: Collection[str] = (),
    now: datetime | None = None,
    signals: Mapping[int, float] | None = None,
) -> list[screen.ScreenResult]:
    """Every active stock with the screen's verdict; the passed ones (best relative strength
    first, at most `top`) are analysed. Blocked by the quality checks, blacklisted in risk.yaml
    or already held means stopped. Stocks with a news buy signal (`signals`: instrument id ->
    confidence) only need the basic rules; at most `news.max_candidates` of them."""
    data = await screen.load_market_data(ctx.sessions, ctx.benchmarks, ctx.pullback)
    issues = await ingest.run_quality(
        ctx.sessions, ctx.quality, now=now or datetime.now(UTC), market=market
    )
    excluded = {s: "blacklisted in risk.yaml" for s in ctx.blacklist}
    for i in issues:
        if i.blocking:
            excluded[i.yahoo_symbol] = f"data quality: {i.check}"
    found = screen.select(screen.check(data, ctx.pullback, market), excluded, held, top)
    if not signals:
        return found
    from_news = screen.news_check(data, ctx.pullback, signals, market)
    return screen.merge(found, screen.select(from_news, excluded, held, ctx.news.max_candidates))


async def scan_size(ctx: AnalysisContext) -> int:
    scan = ctx.models.scan
    return scan.candidates_lean if await ctx.runner.mode() == "lean" else scan.candidates


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
        found = await screen_stocks(ctx, market, top or await scan_size(ctx))
        symbols = [r.symbol for r in found if r.passed]
    async with ctx.sessions() as s:
        instruments = await market_repo.active_instruments(s)
    by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
    today = today or datetime.now(UTC).date()
    items: list[SymbolAnalysis] = []
    missing: list[str] = []
    for symbol in symbols:
        inst_id = by_symbol.get(symbol)
        history = (
            await load_history(
                ctx.sessions, inst_id, today, ctx.benchmarks.get(instruments[inst_id].market)
            )
            if inst_id is not None
            else None
        )
        if history is None:
            missing.append(symbol)
            continue
        t_in = technical_module.compute(
            history.instrument, history.frame, history.benchmark_close, ctx.rules
        )
        t_out = await ctx.runner.run(
            ctx.technical,
            t_in,
            instrument_id=history.instrument_id,
            as_of=history.as_of,
            subject=symbol,
        )
        e_in = earnings_module.compute(
            history.instrument, history.frame, history.events, ctx.holding_sessions
        )
        e_out = await ctx.runner.run(
            ctx.earnings,
            e_in,
            instrument_id=history.instrument_id,
            as_of=history.as_of,
            subject=symbol,
        )
        items.append(SymbolAnalysis(symbol, history.instrument_id, t_in, t_out, e_in, e_out))
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
