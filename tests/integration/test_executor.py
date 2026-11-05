"""Executor end to end with the simulator broker: brackets, orders and fills in the DB."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fake_broker import Bar, SimBroker
from sqlalchemy import func, select, text

from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as market_repo
from trading_agent.db import orders as repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import steps as steps_repo
from trading_agent.db.models import AuditLog, FillRow, OrderRow
from trading_agent.domain.market import Instrument
from trading_agent.domain.orders import BracketRequest, BrokerOrder, OrderSpec
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import RiskDecision
from trading_agent.execution.fsm import InvalidTransition
from trading_agent.executor import Executor

pytestmark = pytest.mark.db

D = Decimal
PLACED = datetime(2026, 10, 5, 13, 45, tzinfo=UTC)
DAY1 = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)
DECISION = RiskDecision(approved=True, checks=(), quantity=D(3))
REJECTED = "10243: Fractional-sized order cannot be placed"


class Clock:
    def __init__(self) -> None:
        self.now = PLACED

    def __call__(self) -> datetime:
        return self.now


async def _seed(sessions: Sessions, n: int) -> tuple[Instrument, list[UUID]]:
    inst = Instrument(
        symbol="AAA",
        yahoo_symbol="AAA",
        name="AAA",
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
        indices=("SP100",),
    )
    await ingest.sync_universe(
        sessions, Universe(generated=date(2026, 10, 5), benchmarks={}, instruments=[inst])
    )
    async with sessions.begin() as s:
        await s.execute(text("TRUNCATE brackets, orders, fills, audit_log CASCADE"))
        stored = next(iter((await market_repo.active_instruments(s)).values()))
        ids: list[UUID] = []
        for i in range(n):
            p = Proposal(
                id=uuid4(),
                source="agent",
                as_of=date(2026, 10, 1) + timedelta(days=i),
                instrument_id=stored.id or 0,
                yahoo_symbol="AAA",
                market="US",
                sector=None,
                status="proposed",
                strategy="pullback_uptrend",
                thesis="t",
                invalidation="i",
            )
            ids += await proposals_repo.save_proposals(s, [p])
    return stored, ids


def _request(inst: Instrument, bracket_id: UUID) -> BracketRequest:
    return BracketRequest(
        id=bracket_id,
        instrument=inst,
        quantity=D(3),
        entry=D(100),
        stop=D(96),
        target=D(108),
        expires=DAY2,
    )


async def test_bracket_lifecycle_with_a_time_exit(sessions: Sessions) -> None:
    inst, (bid,) = await _seed(sessions, 1)
    clock = Clock()
    broker = SimBroker(clock=clock)
    ex = Executor(sessions, broker, "agent_paper", clock)

    b = await ex.submit(_request(inst, bid), DECISION)
    assert b.state == "submitted"
    assert (await ex.submit(_request(inst, bid), DECISION)).state == "submitted"
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(OrderRow)) == 3

    [b] = await ex.sync()
    assert b.state == "working"
    assert await ex.sync() == []

    broker.on_bar(inst.id or 0, Bar(101, 102, 99, 101), DAY1)
    clock.now = DAY1
    [b] = await ex.sync()
    assert (b.state, b.filled_qty, b.entry_price) == ("filled", 3, D("100.05"))
    await ex.sync()  # the same fill twice changes nothing
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(FillRow)) == 1

    b = await ex.move_stop(bid, D(100))
    assert b is not None
    assert b.stop == D(100)
    with pytest.raises(InvalidTransition):
        await ex.move_stop(bid, D(99))
    stop_spec = next(spec for spec, _, _ in await _orders(sessions, bid) if spec.kind == "stop")
    assert stop_spec.stop_price == D(100)

    b = await ex.request_exit(bid, "time")
    assert b is not None
    assert b.state == "exiting"
    assert (await ex.request_exit(bid, "time")) == b  # repeat: no second exit order
    broker.on_bar(inst.id or 0, Bar(103, 104, 102, 103), DAY2)
    clock.now = DAY2
    [b] = await ex.sync()
    assert (b.state, b.exit_qty, b.exit_price, b.exit_reason) == (
        "closed",
        3,
        D("102.9485"),
        "time",
    )
    assert await ex.open_brackets() == []
    async with sessions() as s:
        events = (await s.scalars(select(AuditLog.event).order_by(AuditLog.id))).all()
        fills = [f for b, _, f in await repo.book_fills(s, "agent_paper") if b == bid]
    assert events == [
        "bracket.approved",
        "bracket.submitted",
        "bracket.working",
        "bracket.filled",
        "bracket.stop_moved",
        "bracket.exit_requested",
        "bracket.closed",
    ]
    assert [f.order_ref.split("-")[2] for f in fills] == ["entry", "exit"]

    async with sessions() as s:
        steps = await steps_repo.steps(s, symbol="AAA")
    assert [(x.step, x.status) for x in steps] == [
        ("order", "passed"),
        ("broker", "info"),
        ("fill", "passed"),
        ("stop", "info"),
        ("exit", "info"),
        ("broker", "info"),
        ("broker", "info"),
        ("broker", "info"),
        ("exit", "passed"),
    ]
    assert all(x.proposal_id == bid for x in steps)
    order = steps[0]
    assert order.summary == (
        "sent to simulator: entry BUY 3 LMT 100; target SELL 3 LMT 108; stop SELL 3 STP 96"
    )
    assert order.input is not None
    assert order.output is not None
    assert [o["order_ref"] for o in order.output["orders"]] == [
        o["order_ref"] for o in order.input["orders"]
    ]
    assert steps[1].summary == "entry order working -> filled"
    assert steps[2].summary == "bought 3 at 100.05"
    assert [f["exec_id"] for f in (steps[2].output or {})["fills"]] == [fills[0].exec_id]
    assert steps[3].summary == "stop moved 96 -> 100 at simulator"
    assert steps[4].summary == "exit (time) sent to simulator: exit SELL 3 MKT"
    assert steps[-1].summary == "sold 3 at 102.9485 (time)"


async def test_order_errors_are_recorded(sessions: Sessions) -> None:
    inst, (down_id, rejected_id) = await _seed(sessions, 2)

    class Down(SimBroker):
        async def place(self, specs: Sequence[OrderSpec]) -> list[BrokerOrder]:
            raise ConnectionError("Not connected")

    class Rejects(SimBroker):
        async def place(self, specs: Sequence[OrderSpec]) -> list[BrokerOrder]:
            return [
                o.model_copy(update={"status": "inactive", "message": REJECTED})
                if o.order_ref.split("-")[2] == "entry"
                else o
                for o in await super().place(specs)
            ]

    clock = Clock()
    with pytest.raises(ConnectionError):
        await Executor(sessions, Down(), "agent_paper", clock).submit(
            _request(inst, down_id), DECISION
        )
    await Executor(sessions, Rejects(), "agent_paper", clock).submit(
        _request(inst, rejected_id), DECISION
    )
    async with sessions() as s:
        steps = {x.proposal_id: x for x in await steps_repo.steps(s, day=date(2026, 10, 5))}
        message = await s.scalar(select(OrderRow.message).where(OrderRow.message.is_not(None)))
    down, rejected = steps[down_id], steps[rejected_id]
    assert (down.step, down.status, down.error) == (
        "order",
        "error",
        "ConnectionError: Not connected",
    )
    assert down.summary.startswith("not sent to simulator: entry BUY 3 LMT 100")
    assert (rejected.status, rejected.error) == (
        "error",
        f"TA-AAA-entry-{rejected_id.hex[:8]}: {REJECTED}",
    )
    assert message == REJECTED


async def test_cancelled_expired_and_restored(sessions: Sessions) -> None:
    inst, (cancel_id, expire_id, stop_id) = await _seed(sessions, 3)
    clock = Clock()
    broker = SimBroker(clock=clock)
    ex = Executor(sessions, broker, "agent_paper", clock)
    for bid in (cancel_id, expire_id, stop_id):
        await ex.submit(_request(inst, bid), DECISION)
    await ex.sync()

    b = await ex.cancel_entry(cancel_id, "halted")
    assert b is not None
    assert b.cancel_reason == "halted"
    clock.now = DAY1
    by_id = {x.id: x for x in await ex.sync()}
    assert by_id[cancel_id].state == "cancelled"
    assert await ex.cancel_entry(cancel_id, "again") == by_id[cancel_id]  # already ended

    # a restart: a fresh simulator rebuilt from the stored orders
    async with sessions() as s:
        stored = await repo.orders(s)
    broker = SimBroker(clock=clock)
    broker.restore(stored)
    ex = Executor(sessions, broker, "agent_paper", clock)
    assert await ex.sync() == []

    broker.on_bar(inst.id or 0, Bar(105, 106, 104, 105), DAY1)
    broker.on_bar(inst.id or 0, Bar(105, 106, 104, 105), DAY2)
    clock.now = DAY2
    states = {x.id: x.state for x in await ex.sync()}
    assert states == {expire_id: "expired", stop_id: "expired"}


async def test_invalid_broker_state_leaves_the_stored_orders(sessions: Sessions) -> None:
    inst, (bid,) = await _seed(sessions, 1)

    class Overfilled(SimBroker):
        async def orders(self) -> list[BrokerOrder]:
            return [
                o.model_copy(update={"filled": 5, "avg_fill_price": D(100), "status": "filled"})
                if o.order_ref.split("-")[2] == "entry"
                else o
                for o in await super().orders()
            ]

    clock = Clock()
    ex = Executor(sessions, Overfilled(clock=clock), "agent_paper", clock)
    await ex.submit(_request(inst, bid), DECISION)
    assert await ex.sync() == []
    entry = next(o for spec, o, _ in await _orders(sessions, bid) if spec.kind == "entry")
    assert entry.filled == 0


async def _orders(sessions: Sessions, bid: UUID) -> list[tuple[OrderSpec, BrokerOrder, datetime]]:
    async with sessions() as s:
        return await repo.orders(s, [bid])


async def test_fractional_bracket_round_trip(sessions: Sessions) -> None:
    inst, (bid,) = await _seed(sessions, 1)
    clock = Clock()
    broker = SimBroker(clock=clock)
    ex = Executor(sessions, broker, "agent_paper", clock)
    quantity = D("2.5123")
    req = _request(inst, bid).model_copy(update={"quantity": quantity})
    await ex.submit(req, RiskDecision(approved=True, checks=(), quantity=quantity))
    await ex.sync()

    broker.on_bar(inst.id or 0, Bar(101, 102, 99, 101), DAY1)
    clock.now = DAY1
    [b] = await ex.sync()
    assert (b.state, b.filled_qty) == ("filled", quantity)
    broker.on_bar(inst.id or 0, Bar(103, 109, 102, 108), DAY2)
    clock.now = DAY2
    [b] = await ex.sync()
    assert (b.state, b.exit_qty, b.exit_reason) == ("closed", quantity, "target")
    async with sessions() as s:
        fills = [f for b_, _, f in await repo.book_fills(s, "agent_paper") if b_ == bid]
    assert [f.quantity for f in fills] == [quantity, quantity]
