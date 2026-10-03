from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from trading_agent.data.ingest import Sessions
from trading_agent.db.session import create_engine, session_factory
from trading_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]


@pytest.fixture(scope="session")
def migrated() -> Iterator[None]:
    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture
async def sessions(settings: Settings, migrated: None) -> AsyncIterator[Sessions]:
    """Session factory on an emptied market-data schema."""
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE instruments, fx_daily, macro_series CASCADE"))
    try:
        yield session_factory(engine)
    finally:
        await engine.dispose()
