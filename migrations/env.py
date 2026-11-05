import asyncio

from alembic import context
from sqlalchemy import Connection

from trading_agent.core.db import create_engine
from trading_agent.core.settings import Settings
from trading_agent.core.tables import Base

target_metadata = Base.metadata


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run() -> None:
    engine = create_engine(Settings().database_url)  # pyright: ignore[reportCallIssue]
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations are not supported.")
asyncio.run(_run())
