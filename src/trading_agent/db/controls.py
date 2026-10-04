"""Kill-switch state and the pending reset code."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.models import AuditLog, KillSwitchRow
from trading_agent.domain.risk import KillSwitch, TradingState


async def _row(session: AsyncSession, lock: bool) -> KillSwitchRow | None:
    stmt = select(KillSwitchRow)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def kill_switch(session: AsyncSession, *, lock: bool = False) -> KillSwitch:
    """Active when nothing was ever stored. `lock` holds the row until the transaction ends."""
    row = await _row(session, lock)
    if row is None:
        return KillSwitch()
    state: TradingState = row.state  # pyright: ignore[reportAssignmentType]  # checked on write
    return KillSwitch(state=state, reason=row.reason, since=row.since, until=row.until)


async def save_kill_switch(session: AsyncSession, ks: KillSwitch, actor: str) -> None:
    """Stores the state, drops any pending reset code and writes the change to audit_log."""
    values = {
        "state": ks.state,
        "reason": ks.reason,
        "since": ks.since,
        "until": ks.until,
        "reset_code_sha256": None,
        "reset_code_expires": None,
    }
    stmt = insert(KillSwitchRow).values(id=1, **values)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["id"], set_=values | {"updated_at": func.now()})
    )
    session.add(
        AuditLog(actor=actor, event="kill_switch.changed", payload=ks.model_dump(mode="json"))
    )


async def set_reset_code(session: AsyncSession, sha256: str, expires: datetime) -> None:
    row = await _row(session, lock=True)
    if row is None:
        raise LookupError("no kill-switch state stored")
    row.reset_code_sha256, row.reset_code_expires = sha256, expires
    session.add(AuditLog(actor="cli", event="kill_switch.reset_code_issued", payload={}))


async def reset_code(session: AsyncSession) -> tuple[str, datetime] | None:
    row = await _row(session, lock=False)
    if row is None or row.reset_code_sha256 is None or row.reset_code_expires is None:
        return None
    return row.reset_code_sha256, row.reset_code_expires
