"""Kill-switch state, the pending reset code and the risk decision per proposal."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core.tables import AuditLog, KillSwitchRow, RiskDecisionRow
from trading_agent.risk.types import Check, KillSwitch, RiskDecision, TradingState


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


async def save_decision(
    session: AsyncSession, proposal_id: UUID, decision: RiskDecision, ts: datetime
) -> None:
    values = {
        "ts": ts,
        "approved": decision.approved,
        "quantity": decision.quantity,
        "trip": decision.trip,
        "checks": [c.model_dump() for c in decision.checks],
    }
    stmt = insert(RiskDecisionRow).values(proposal_id=proposal_id, **values)
    await session.execute(stmt.on_conflict_do_update(index_elements=["proposal_id"], set_=values))


async def decisions(
    session: AsyncSession, proposal_ids: Sequence[UUID]
) -> dict[UUID, RiskDecision]:
    """Stored decisions (checks, approval, quantity; prices live in the bracket)."""
    stmt = select(RiskDecisionRow).where(RiskDecisionRow.proposal_id.in_(proposal_ids))
    return {
        r.proposal_id: RiskDecision.model_validate(
            {
                "approved": r.approved,
                "quantity": r.quantity,
                "trip": r.trip,
                "checks": [Check.model_validate(c) for c in r.checks],
            }
        )
        for r in await session.scalars(stmt)
    }
