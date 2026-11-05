"""The agent's main loop: builds everything, plans the jobs, runs until stopped."""

import asyncio
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import httpx
import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobEvent, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import SecretStr

from trading_agent import orchestrator
from trading_agent.contexts import AnalysisContext, BookContext
from trading_agent.core import audit, telemetry
from trading_agent.core.db import Sessions, create_engine, session_factory
from trading_agent.core.log import describe_error
from trading_agent.core.settings import CronJob, DataConfig, ScheduleConfig, SessionJob, Settings
from trading_agent.ibkr import link as gateway
from trading_agent.ibkr.executor import Executor
from trading_agent.llm.prompts import load_prompt
from trading_agent.marketdata import calendars, ingest, jobs
from trading_agent.marketdata.jobs import DataContext
from trading_agent.marketdata.universe import Universe
from trading_agent.news import agent as news_module
from trading_agent.news import stage as news
from trading_agent.news.agent import NewsAgent
from trading_agent.news.sources import FinnhubNews, NewsSource, YahooNews
from trading_agent.portfolio import manager as portfolio_manager
from trading_agent.portfolio.types import book_for_mode
from trading_agent.risk.controls import ControlCenter
from trading_agent.telegram import heartbeat, journal, messages
from trading_agent.telegram.bot import (
    Command,
    Interaction,
    LogNotifier,
    Notifier,
    Reply,
    TelegramBot,
    build_notifier,
)
from trading_agent.telegram.briefing import (
    RuntimeState,
    briefing_text,
    budget_text,
    morning_briefing,
    status_text,
)

log = structlog.get_logger(__name__)

JobFn = Callable[[], Awaitable[object]]


async def _heartbeat(
    http: httpx.AsyncClient, url: SecretStr | None, state: RuntimeState | None
) -> None:
    ok = await heartbeat.ping(http, url)
    if state is not None and url is not None and url.get_secret_value():
        state.heartbeat_at, state.heartbeat_ok = datetime.now(UTC), ok


def build_scheduler(
    schedule: ScheduleConfig,
    settings: Settings,
    http: httpx.AsyncClient,
    job_fns: Mapping[str, JobFn] | None = None,
    state: RuntimeState | None = None,
) -> AsyncIOScheduler:
    job_fns = job_fns or {}
    if unknown := set(schedule.jobs) - set(job_fns):
        raise ValueError(f"schedule.yaml names unknown jobs: {sorted(unknown)}")
    tz = ZoneInfo(schedule.timezone)
    scheduler = AsyncIOScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1})
    scheduler.add_job(
        _heartbeat,
        "interval",
        id="heartbeat",
        minutes=schedule.heartbeat.interval_minutes,
        args=[http, settings.heartbeat_url, state],
        next_run_time=datetime.now(tz),
        misfire_grace_time=60,
    )
    session_jobs: dict[str, SessionJob] = {}
    for name, spec in schedule.jobs.items():
        if isinstance(spec, CronJob):
            scheduler.add_job(
                job_fns[name],
                CronTrigger(timezone=tz, **spec.cron),  # pyright: ignore[reportArgumentType]
                id=name,
                misfire_grace_time=spec.misfire_grace_minutes * 60,
            )
        else:
            session_jobs[name] = spec
    if session_jobs:
        plan = partial(_plan_today, scheduler, session_jobs, job_fns, tz)
        scheduler.add_job(
            plan,
            CronTrigger(hour=0, minute=5, timezone=tz),
            id="plan_session_jobs",
            next_run_time=datetime.now(tz),
        )
    return scheduler


async def _plan_today(
    scheduler: AsyncIOScheduler,
    specs: Mapping[str, SessionJob],
    job_fns: Mapping[str, JobFn],
    tz: ZoneInfo,
) -> None:
    plan_session_jobs(scheduler, specs, job_fns, datetime.now(tz).date())


def plan_session_jobs(
    scheduler: AsyncIOScheduler,
    specs: Mapping[str, SessionJob],
    job_fns: Mapping[str, JobFn],
    day: date,
) -> list[str]:
    """Add one-off jobs for `day`'s sessions; past run times still run within their grace time."""
    planned: list[str] = []
    for name, spec in specs.items():
        anchor = calendars.session_time(spec.calendar, day, spec.anchor)
        if anchor is None:
            continue
        job_id = f"{name}:{day.isoformat()}"
        scheduler.add_job(
            job_fns[name],
            "date",
            run_date=anchor + timedelta(minutes=spec.offset_minutes),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=spec.misfire_grace_minutes * 60,
        )
        planned.append(job_id)
    log.info("scheduler.planned", day=day.isoformat(), jobs=planned)
    return planned


def job_functions(
    data: DataContext,
    book: BookContext,
    notifier: Notifier,
    analysis: AnalysisContext,
    link: gateway.BrokerLink | None = None,
    center: ControlCenter | None = None,
    trade: portfolio_manager.TradingContext | None = None,
    expected: gateway.Expected | None = None,
    news_ctx: news.NewsContext | None = None,
) -> dict[str, JobFn]:
    halt = center.halt if center else None
    fns: dict[str, JobFn] = {
        "ingest_macro": partial(jobs.macro, data),
        "ingest_earnings": partial(jobs.earnings, data),
        "ingest_eod_eu": partial(jobs.eod, data, "EU"),
        "ingest_eod_us": partial(jobs.eod, data, "US"),
        "evening_digest": partial(
            journal.evening_digest, book.sessions, book.book, book.budget_eur, notifier
        ),
        "briefing": partial(morning_briefing, book.briefing, data.blocked, notifier),
        "scan_eu": telemetry.traced(
            "scan_eu", partial(orchestrator.scheduled_scan, analysis, notifier, "EU")
        ),
        "scan_us": telemetry.traced(
            "scan_us", partial(orchestrator.scheduled_scan, analysis, notifier, "US")
        ),
        "reconcile": partial(gateway.reconcile_positions, link, notifier, expected, halt),
    }
    if trade is not None:
        fns |= {
            "place_eu": partial(orchestrator.place, trade, "EU"),
            "place_us": partial(orchestrator.place, trade, "US"),
            "execution_eu": partial(orchestrator.end_of_day, trade, "EU"),
            "execution_us": partial(orchestrator.end_of_day, trade, "US"),
            "monitor": partial(orchestrator.monitor, trade),
        }
    if news_ctx is not None:
        fns |= {
            "news_positions": partial(news.watch_positions, news_ctx),
            "news_eu": telemetry.traced("news_eu", partial(news.scan_news, news_ctx, "EU")),
            "news_us": telemetry.traced("news_us", partial(news.scan_news, news_ctx, "US")),
        }
    return fns


def interaction(review: journal.Review, center: ControlCenter) -> Interaction:
    """Buttons go to the kill switch (k:...) or the review; replies only to the review."""

    async def on_button(data: str) -> Reply:
        if data.startswith("k:"):
            return await center.on_button(data)
        return await review.on_button(data)

    return Interaction(on_button, review.on_text)


def news_context(
    settings: Settings,
    trade: portfolio_manager.TradingContext,
    analysis: AnalysisContext,
    http: httpx.AsyncClient,
) -> news.NewsContext:
    key = settings.finnhub_api_key
    sources: list[NewsSource] = [YahooNews()]
    if key is not None and key.get_secret_value():
        sources.append(FinnhubNews(http, key))
    else:
        log.warning("news.no_finnhub_key", effect="no news for US stocks")
    return news.NewsContext(
        trade=trade,
        runner=analysis.runner,
        agent=NewsAgent(
            load_prompt(settings.prompts_dir, news_module.NAME, news_module.PROMPT_VERSION)
        ),
        sources=sources,
        params=analysis.news,
    )


def trading_context(
    settings: Settings,
    sessions: Sessions,
    book: BookContext,
    center: ControlCenter,
    notifier: Notifier,
    link: gateway.BrokerLink,
) -> portfolio_manager.TradingContext:
    """Every order goes to IBKR through `link`; there is no other broker."""
    mode = settings.app_mode
    return portfolio_manager.TradingContext(
        sessions=sessions,
        executor=Executor(sessions, link.broker, book_for_mode(mode)),
        center=center,
        risk=book.risk,
        fees=book.fees,
        rules=book.pullback,
        mode=mode,
        notifier=notifier,
        market_info=link.market_info,
        news=book.news,
    )


def alert_on_job_failures(
    scheduler: AsyncIOScheduler, notifier: Notifier
) -> set[asyncio.Task[None]]:
    """Send a Telegram alert when a job raises or misses its run time."""
    loop = asyncio.get_running_loop()
    tasks: set[asyncio.Task[None]] = set()

    def spawn(text: str) -> None:
        task = loop.create_task(notifier.send(text))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def listener(event: JobEvent) -> None:
        if isinstance(event, JobExecutionEvent) and event.exception is not None:
            text = messages.job_alert(event.job_id, describe_error(event.exception))
        else:
            text = messages.missed_alert(event.job_id)
        loop.call_soon_threadsafe(spawn, text)

    scheduler.add_listener(listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    return tasks


def commands(
    state: RuntimeState,
    sessions: Sessions,
    book: BookContext,
    analysis: AnalysisContext | None = None,
) -> dict[str, Command]:
    async def status(_: list[str]) -> str:
        return await status_text(state, sessions)

    async def briefing(_: list[str]) -> str:
        return await briefing_text(book.briefing, state.blocked, datetime.now(UTC).date())

    out = {
        "status": Command("mode, heartbeat, data and next jobs", status),
        "briefing": Command("the morning briefing, now", briefing),
    }
    if analysis is not None:
        monthly_usd = analysis.models.budget.monthly_usd

        async def budget(_: list[str]) -> str:
            return await budget_text(analysis.sessions, analysis.runner, monthly_usd)

        out["budget"] = Command("LLM spend this month and the budget mode", budget)
    return out


@dataclass
class Agent:
    """Everything the running agent is made of, built once at start."""

    state: RuntimeState
    book: BookContext
    analysis: AnalysisContext
    center: ControlCenter
    link: gateway.BrokerLink
    trade: portfolio_manager.TradingContext
    notifier: Notifier
    expected: gateway.Expected  # the book's positions, for the reconciliation
    live: bool  # live mode: orders wait for /confirm_live


def build_agent(settings: Settings, sessions: Sessions, universe: Universe) -> Agent:
    """Contexts, kill switch, IBKR link (not yet running), Telegram, and how they call
    each other: the link reconciles on connect, a halt cancels unfilled entries."""
    state = RuntimeState(
        mode=settings.app_mode,
        started_at=datetime.now(UTC),
        tracing=telemetry.state(),
        trace_ui_url=settings.trace_ui_url,
        dashboard_ui_url=settings.dashboard_ui_url,
    )
    book = BookContext.load(settings.config_dir, sessions, universe, settings.app_mode)
    analysis = AnalysisContext.build(settings, sessions, universe)
    review = journal.Review(sessions)
    center = ControlCenter(sessions, settings.app_mode)
    link = gateway.BrokerLink(settings, LogNotifier())
    center.gateway_mode = link.mode
    state.gateway = link.status
    trade = trading_context(settings, sessions, book, center, LogNotifier(), link)
    notifier = build_notifier(
        settings.telegram_bot_token,
        settings.telegram_owner_chat_id,
        commands(state, sessions, book, analysis)
        | journal.commands(sessions, review, settings.trace_ui_url)
        | center.commands()
        | portfolio_manager.commands(trade),
        interaction(review, center),
    )
    center.notifier = trade.notifier = link.notifier = notifier
    live = center.start_live_interlock()

    async def kill_switch_line() -> str:
        return messages.kill_switch_line(await center.kill_switch())

    state.kill_switch, state.interlock = kill_switch_line, center.interlock_line
    expected = partial(portfolio_manager.expected_positions, trade)
    center.on_halt = partial(portfolio_manager.cancel_entries, trade, "halted")
    link.on_connect = partial(
        gateway.on_connect,
        sessions=sessions,
        notifier=notifier,
        expected=expected,
        halt=center.halt,
    )
    return Agent(state, book, analysis, center, link, trade, notifier, expected, live)


def schedule_jobs(
    agent: Agent,
    settings: Settings,
    schedule: ScheduleConfig,
    data_cfg: DataConfig,
    http: httpx.AsyncClient,
) -> AsyncIOScheduler:
    """Every job of config/schedule.yaml, bound to its stage function."""
    data = DataContext.build(settings, data_cfg, agent.book.sessions, http, agent.notifier)
    data.blocked = agent.state.blocked  # the quality check's result shows in /status
    data.ibkr = agent.link.prices
    fns = job_functions(
        data,
        agent.book,
        agent.notifier,
        agent.analysis,
        agent.link,
        agent.center,
        agent.trade,
        agent.expected,
        news_context(settings, agent.trade, agent.analysis, http),
    )
    scheduler = build_scheduler(schedule, settings, http, fns, agent.state)
    agent.state.scheduler = scheduler
    return scheduler


def _stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


async def _sync_universe(sessions: Sessions, universe: Universe) -> None:
    sync = await ingest.sync_universe(sessions, universe)
    log.info(
        "universe.synced", inserted=sync.inserted, updated=sync.updated, deactivated=sync.removed
    )


async def serve(
    settings: Settings, schedule: ScheduleConfig, data_cfg: DataConfig, universe: Universe
) -> None:
    """`trading-agent run`: start-up (HOW-IT-WORKS.md 9.1), then the jobs until SIGTERM."""
    stop = _stop_on_signals()
    engine = create_engine(settings.database_url)
    sessions = session_factory(engine)
    try:
        await audit.record(
            sessions, actor="agent", event="agent.started", payload={"mode": settings.app_mode}
        )
        await _sync_universe(sessions, universe)
        agent = build_agent(settings, sessions, universe)
        notifier = agent.notifier
        if isinstance(notifier, TelegramBot):
            notifier.start()
        link_task = asyncio.create_task(agent.link.run(stop))
        async with httpx.AsyncClient() as http:
            scheduler = schedule_jobs(agent, settings, schedule, data_cfg, http)
            pending_alerts = alert_on_job_failures(scheduler, notifier)
            scheduler.start()
            log.info(
                "agent.started",
                mode=settings.app_mode,
                jobs=[j.id for j in scheduler.get_jobs()],
                tracing=agent.state.tracing,
            )
            if isinstance(notifier, TelegramBot):
                await notifier.wait_ready(within_s=30)
            kill_switch = await agent.state.kill_switch() if agent.state.kill_switch else ""
            await notifier.send(f"▶️ Agent started ({settings.app_mode}), kill switch {kill_switch}")
            if agent.live:
                await notifier.send(messages.live_confirm_alert())
            try:
                await stop.wait()
            finally:
                scheduler.shutdown(wait=False)
                await notifier.send("⏹ Agent stopping")
                await asyncio.gather(*pending_alerts, return_exceptions=True)
                await link_task
                if isinstance(notifier, TelegramBot):
                    await notifier.stop()
        await audit.record(sessions, actor="agent", event="agent.stopped")
        log.info("agent.stopped")
    finally:
        await engine.dispose()
