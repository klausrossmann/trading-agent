from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import SecretStr
from typer.testing import CliRunner

from trading_agent.__main__ import app
from trading_agent.scheduler import JobFn, build_scheduler, plan_session_jobs
from trading_agent.settings import (
    HeartbeatJob,
    ScheduleConfig,
    SessionJob,
    Settings,
    load_schedule,
)

ROOT = Path(__file__).resolve().parents[2]


async def _noop() -> None:
    return None


JOBS: dict[str, JobFn] = {
    name: _noop
    for name in (
        "ingest_macro",
        "ingest_earnings",
        "ingest_eod_eu",
        "ingest_eod_us",
        "baseline_sim",
        "agent_sim",
        "briefing",
        "scan_eu",
        "scan_us",
    )
}


@pytest.fixture
def settings() -> Settings:
    return Settings(db_password=SecretStr("x"))


async def test_heartbeat_job_registered(settings: Settings) -> None:
    schedule = ScheduleConfig(heartbeat=HeartbeatJob(interval_minutes=5))
    async with httpx.AsyncClient() as http:
        job = build_scheduler(schedule, settings, http).get_job("heartbeat")
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(minutes=5)


async def test_repo_schedule_wires_cron_and_session_jobs(settings: Settings) -> None:
    async with httpx.AsyncClient() as http:
        scheduler = build_scheduler(load_schedule(ROOT / "config"), settings, http, JOBS)
    ids = {j.id for j in scheduler.get_jobs()}
    assert {"heartbeat", "ingest_macro", "ingest_earnings", "plan_session_jobs"} <= ids
    macro = scheduler.get_job("ingest_macro")
    assert macro is not None
    assert isinstance(macro.trigger, CronTrigger)


async def test_unknown_job_name_rejected(settings: Settings) -> None:
    schedule = ScheduleConfig(
        heartbeat=HeartbeatJob(interval_minutes=5),
        jobs={"typo": SessionJob(calendar="XNYS", anchor="close", offset_minutes=30)},
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="typo"):
            build_scheduler(schedule, settings, http, JOBS)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        # Friday: US close 16:00 ET = 20:00 UTC, Xetra close 17:30 CEST = 15:30 UTC
        (
            date(2026, 10, 2),
            {
                "ingest_eod_eu:2026-10-02": datetime(2026, 10, 2, 16, 0, tzinfo=UTC),
                "ingest_eod_us:2026-10-02": datetime(2026, 10, 2, 20, 30, tzinfo=UTC),
            },
        ),
        (date(2026, 10, 3), {}),  # Saturday
        # Christmas Eve: Xetra closed, NYSE half-day until 13:00 ET (18:00 UTC)
        (
            date(2026, 12, 24),
            {"ingest_eod_us:2026-12-24": datetime(2026, 12, 24, 18, 30, tzinfo=UTC)},
        ),
    ],
)
async def test_session_jobs_follow_exchange_calendar(
    day: date, expected: dict[str, datetime]
) -> None:
    specs = {
        "ingest_eod_eu": SessionJob(calendar="XETR", anchor="close", offset_minutes=30),
        "ingest_eod_us": SessionJob(calendar="XNYS", anchor="close", offset_minutes=30),
    }
    scheduler = AsyncIOScheduler(timezone="Europe/Berlin")
    scheduler.start(paused=True)  # the planner always runs on a started scheduler
    planned = plan_session_jobs(scheduler, specs, JOBS, day)
    run_dates = {
        job_id: job.trigger.run_date
        for job_id in planned
        if (job := scheduler.get_job(job_id)) is not None
    }
    assert run_dates == expected
    assert plan_session_jobs(scheduler, specs, JOBS, day) == planned  # re-planning is idempotent
    assert len(scheduler.get_jobs()) == len(expected)
    scheduler.shutdown(wait=False)


def test_cli_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "backfill", "quality"):
        assert command in result.output
