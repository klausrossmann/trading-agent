import asyncio
import signal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select

from trading_agent.data.universe import load_universe
from trading_agent.db import audit
from trading_agent.db.models import AuditLog, Base
from trading_agent.db.session import create_engine, session_factory
from trading_agent.scheduler import serve
from trading_agent.settings import HeartbeatJob, ScheduleConfig, Settings, load_data_config

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]


async def _events(settings: Settings) -> list[str]:
    engine = create_engine(settings.database_url)
    try:
        async with session_factory(engine)() as session:
            rows = await session.scalars(select(AuditLog.event).order_by(AuditLog.id))
            return list(rows)
    finally:
        await engine.dispose()


@pytest.mark.usefixtures("migrated")
async def test_schema_matches_models(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            diff = await conn.run_sync(
                lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata)
            )
    finally:
        await engine.dispose()
    assert diff == []


@pytest.mark.usefixtures("migrated")
async def test_audit_record_persists(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    try:
        await audit.record(
            session_factory(engine), actor="test", event="test.event", payload={"a": 1}
        )
        async with session_factory(engine)() as session:
            row = await session.scalar(select(AuditLog).where(AuditLog.event == "test.event"))
    finally:
        await engine.dispose()
    assert row is not None
    assert row.payload == {"a": 1}
    assert row.ts.tzinfo is not None


@pytest.mark.usefixtures("migrated")
async def test_serve_starts_and_stops_on_sigterm(settings: Settings) -> None:
    schedule = ScheduleConfig(heartbeat=HeartbeatJob(interval_minutes=5))
    data_cfg, universe = load_data_config(ROOT / "config"), load_universe(ROOT / "config")
    task = asyncio.create_task(serve(settings, schedule, data_cfg, universe))
    for _ in range(50):
        if "agent.started" in await _events(settings):
            break
        await asyncio.sleep(0.1)
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(task, timeout=10)
    events = await _events(settings)
    assert events[-2:] == ["agent.started", "agent.stopped"]
