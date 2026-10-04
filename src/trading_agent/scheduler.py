"""Job wiring and the agent's main loop (orchestration layer)."""

import asyncio
import signal
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import httpx
import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobEvent, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import SecretStr

from trading_agent import broker, jobs, journal, pipeline, trading
from trading_agent.controls import ControlCenter
from trading_agent.data import calendars, ingest
from trading_agent.data.ingest import describe_error
from trading_agent.data.universe import Universe
from trading_agent.db import audit
from trading_agent.db import orders as orders_repo
from trading_agent.db.session import create_engine, session_factory
from trading_agent.execution.orders import OrderBroker
from trading_agent.execution.sim_broker import SimBroker
from trading_agent.executor import Executor
from trading_agent.notify import heartbeat, messages
from trading_agent.notify.telegram import (
    Command,
    Interaction,
    Notifier,
    Reply,
    TelegramBot,
    build_notifier,
)
from trading_agent.settings import CronJob, DataConfig, ScheduleConfig, SessionJob, Settings

log = structlog.get_logger(__name__)

JobFn = Callable[[], Awaitable[object]]


async def _heartbeat(
    http: httpx.AsyncClient, url: SecretStr | None, state: jobs.RuntimeState | None
) -> None:
    ok = await heartbeat.ping(http, url)
    if state is not None and url is not None and url.get_secret_value():
        state.heartbeat_at, state.heartbeat_ok = datetime.now(UTC), ok


def build_scheduler(
    schedule: ScheduleConfig,
    settings: Settings,
    http: httpx.AsyncClient,
    job_fns: Mapping[str, JobFn] | None = None,
    state: jobs.RuntimeState | None = None,
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
    data: jobs.DataContext,
    book: jobs.BookContext,
    notifier: Notifier,
    analysis: jobs.AnalysisContext,
    link: broker.BrokerLink | None = None,
    center: ControlCenter | None = None,
    trade: trading.TradingContext | None = None,
    expected: broker.Expected | None = None,
) -> dict[str, JobFn]:
    halt = center.halt if center else None
    fns: dict[str, JobFn] = {
        "ingest_macro": partial(jobs.macro, data),
        "ingest_earnings": partial(jobs.earnings, data),
        "ingest_eod_eu": partial(jobs.eod, data, "EU"),
        "ingest_eod_us": partial(jobs.eod, data, "US"),
        "baseline_sim": partial(jobs.baseline_book, book),
        "evening_digest": partial(journal.evening_digest, book, notifier),
        "briefing": partial(jobs.morning_briefing, book, data.state, notifier),
        "scan_eu": partial(pipeline.scheduled_scan, analysis, notifier, "EU"),
        "scan_us": partial(pipeline.scheduled_scan, analysis, notifier, "US"),
        "reconcile": partial(broker.reconcile_positions, link, notifier, expected, halt),
    }
    if trade is not None:
        fns |= {
            "place_eu": partial(trading.place, trade, "EU"),
            "place_us": partial(trading.place, trade, "US"),
            "execution_eu": partial(trading.end_of_day, trade, "EU"),
            "execution_us": partial(trading.end_of_day, trade, "US"),
            "monitor": partial(trading.monitor, trade),
        }
    return fns


def interaction(review: journal.Review, center: ControlCenter) -> Interaction:
    """Buttons go to the kill switch (k:...) or the review; replies only to the review."""

    async def on_button(data: str) -> Reply:
        if data.startswith("k:"):
            return await center.on_button(data)
        return await review.on_button(data)

    return Interaction(on_button, review.on_text)


async def trading_context(
    settings: Settings,
    sessions: jobs.Sessions,
    book: jobs.BookContext,
    center: ControlCenter,
    notifier: Notifier,
    link: broker.BrokerLink | None,
) -> trading.TradingContext:
    """Orders go to IBKR only with IB_ORDERS_ENABLED; otherwise to the simulator, rebuilt
    from the stored orders."""
    sim: SimBroker | None = None
    if settings.ib_orders_enabled and link is not None:
        order_broker: OrderBroker = link.broker
    else:
        sim = SimBroker(slippage_pct=book.cfg.slippage_pct)
        async with sessions() as s:
            sim.restore(await orders_repo.orders(s))
        order_broker = sim
    mode = settings.app_mode
    return trading.TradingContext(
        sessions=sessions,
        executor=Executor(
            sessions, order_broker, "agent_paper" if mode == "paper" else "agent_live"
        ),
        center=center,
        risk=book.risk,
        fees=book.fees,
        rules=book.cfg.pullback,
        mode=mode,
        notifier=notifier,
        sim=sim,
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
    state: jobs.RuntimeState,
    sessions: jobs.Sessions,
    book: jobs.BookContext,
    analysis: jobs.AnalysisContext | None = None,
) -> dict[str, Command]:
    async def status(_: list[str]) -> str:
        return await jobs.status_text(state, sessions)

    async def briefing(_: list[str]) -> str:
        return await jobs.briefing_text(book, state, datetime.now(UTC).date())

    out = {
        "status": Command("mode, heartbeat, data and next jobs", status),
        "briefing": Command("the morning briefing, now", briefing),
    }
    if analysis is not None:

        async def budget(_: list[str]) -> str:
            return await jobs.budget_text(analysis)

        out["budget"] = Command("LLM spend this month and the budget mode", budget)
    return out


async def serve(
    settings: Settings, schedule: ScheduleConfig, data_cfg: DataConfig, universe: Universe
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    engine = create_engine(settings.database_url)
    sessions = session_factory(engine)
    try:
        await audit.record(
            sessions, actor="agent", event="agent.started", payload={"mode": settings.app_mode}
        )
        sync = await ingest.sync_universe(sessions, universe)
        log.info(
            "universe.synced",
            inserted=sync.inserted,
            updated=sync.updated,
            deactivated=sync.removed,
        )
        state = jobs.RuntimeState(mode=settings.app_mode, started_at=datetime.now(UTC))
        book = jobs.BookContext.load(settings.config_dir, sessions, universe)
        analysis = jobs.AnalysisContext.build(settings, sessions, universe)
        review = journal.Review(sessions)
        link: broker.BrokerLink | None = None
        center = ControlCenter(sessions, settings.app_mode)
        notifier = build_notifier(
            settings.telegram_bot_token,
            settings.telegram_owner_chat_id,
            commands(state, sessions, book, analysis)
            | journal.commands(sessions, review)
            | center.commands(),
            interaction(review, center),
        )
        center.notifier = notifier
        live = center.start_live_interlock()

        async def kill_switch_line() -> str:
            return messages.kill_switch_line(await center.kill_switch())

        state.kill_switch, state.interlock = kill_switch_line, center.interlock_line
        if isinstance(notifier, TelegramBot):
            notifier.start()
        link_task: asyncio.Task[None] | None = None
        if settings.ib_enabled:
            link = broker.BrokerLink(settings, notifier)
            center.gateway_mode = link.mode
            state.gateway = link.status
        trade = await trading_context(settings, sessions, book, center, notifier, link)
        expected = (
            partial(trading.expected_positions, trade) if settings.ib_orders_enabled else None
        )
        center.on_halt = partial(trading.cancel_entries, trade, "halted")
        if link is not None:
            link.on_connect = partial(
                broker.on_connect,
                sessions=sessions,
                notifier=notifier,
                expected=expected,
                halt=center.halt,
            )
            link_task = asyncio.create_task(link.run(stop))
        async with httpx.AsyncClient() as http:
            ctx = jobs.DataContext.build(settings, data_cfg, sessions, http, notifier, state)
            ctx.ibkr = link.prices if link else None
            scheduler = build_scheduler(
                schedule,
                settings,
                http,
                job_functions(ctx, book, notifier, analysis, link, center, trade, expected),
                state,
            )
            state.scheduler = scheduler
            pending_alerts = alert_on_job_failures(scheduler, notifier)
            scheduler.start()
            log.info(
                "agent.started", mode=settings.app_mode, jobs=[j.id for j in scheduler.get_jobs()]
            )
            if isinstance(notifier, TelegramBot):
                await notifier.wait_ready(within_s=30)
            await notifier.send(
                f"▶️ Agent started ({settings.app_mode}), kill switch {await kill_switch_line()}"
            )
            if live:
                await notifier.send(messages.live_confirm_alert())
            try:
                await stop.wait()
            finally:
                scheduler.shutdown(wait=False)
                await notifier.send("⏹ Agent stopping")
                await asyncio.gather(*pending_alerts, return_exceptions=True)
                if link_task is not None:
                    await link_task
                if isinstance(notifier, TelegramBot):
                    await notifier.stop()
        await audit.record(sessions, actor="agent", event="agent.stopped")
        log.info("agent.stopped")
    finally:
        await engine.dispose()
