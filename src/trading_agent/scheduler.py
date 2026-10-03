"""Job wiring and the agent's main loop (orchestration layer)."""

import asyncio
import signal
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from trading_agent import jobs
from trading_agent.data import calendars, ingest
from trading_agent.data.universe import Universe
from trading_agent.db import audit
from trading_agent.db.session import create_engine, session_factory
from trading_agent.notify import heartbeat
from trading_agent.settings import CronJob, DataConfig, ScheduleConfig, SessionJob, Settings

log = structlog.get_logger(__name__)

JobFn = Callable[[], Awaitable[object]]


def build_scheduler(
    schedule: ScheduleConfig,
    settings: Settings,
    http: httpx.AsyncClient,
    job_fns: Mapping[str, JobFn] | None = None,
) -> AsyncIOScheduler:
    job_fns = job_fns or {}
    if unknown := set(schedule.jobs) - set(job_fns):
        raise ValueError(f"schedule.yaml names unknown jobs: {sorted(unknown)}")
    tz = ZoneInfo(schedule.timezone)
    scheduler = AsyncIOScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1})
    scheduler.add_job(
        heartbeat.ping,
        "interval",
        id="heartbeat",
        minutes=schedule.heartbeat.interval_minutes,
        kwargs={"client": http, "url": settings.heartbeat_url},
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


def job_functions(data: jobs.DataContext, book: jobs.BookContext) -> dict[str, JobFn]:
    return {
        "ingest_macro": partial(jobs.macro, data),
        "ingest_earnings": partial(jobs.earnings, data),
        "ingest_eod_eu": partial(jobs.eod, data, "EU"),
        "ingest_eod_us": partial(jobs.eod, data, "US"),
        "baseline_sim": partial(jobs.baseline_book, book),
    }


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
        async with httpx.AsyncClient() as http:
            ctx = jobs.DataContext.build(settings, data_cfg, sessions, http)
            book = jobs.BookContext.load(settings.config_dir, sessions, universe)
            scheduler = build_scheduler(schedule, settings, http, job_functions(ctx, book))
            scheduler.start()
            log.info(
                "agent.started", mode=settings.app_mode, jobs=[j.id for j in scheduler.get_jobs()]
            )
            try:
                await stop.wait()
            finally:
                scheduler.shutdown(wait=False)
        await audit.record(sessions, actor="agent", event="agent.stopped")
        log.info("agent.stopped")
    finally:
        await engine.dispose()
