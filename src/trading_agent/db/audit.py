from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_agent.db.models import AuditLog


async def record(
    sessions: async_sessionmaker[AsyncSession],
    *,
    actor: str,
    event: str,
    payload: dict[str, Any] | None = None,
) -> None:
    async with sessions.begin() as session:
        session.add(AuditLog(actor=actor, event=event, payload=payload or {}))
