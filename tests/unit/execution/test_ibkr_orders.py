"""IBKR order side against a fake IB: bracket structure, idempotency, cancel, modify, status."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from ib_async import IB, CommissionReport, Contract, Execution, Fill, Order, OrderStatus, Trade

from trading_agent.domain.market import Instrument
from trading_agent.domain.orders import BracketRequest
from trading_agent.execution.ibkr import IbkrBroker, ib_order
from trading_agent.execution.orders import bracket_specs

D = Decimal
AAPL = Instrument(
    id=7,
    symbol="AAPL",
    yahoo_symbol="AAPL",
    name="Apple",
    market="US",
    exchange="SMART",
    currency="USD",
    kind="stock",
    conid=265598,
)
GTD = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
REQ = BracketRequest(
    id=uuid4(),
    instrument=AAPL,
    quantity=3,
    entry=D("100.05"),
    stop=D(96),
    target=D(108),
    expires=GTD,
)


class FakeIB:
    def __init__(self) -> None:
        self.open: list[Trade] = []
        self.completed: list[Trade] = []
        self.executions: list[Fill] = []
        self.placed: list[tuple[Contract, Order]] = []
        self.cancelled: list[Order] = []
        self._next = 100
        self.client = SimpleNamespace(getReqId=self._req_id)

    def _req_id(self) -> int:
        self._next += 1
        return self._next

    def openTrades(self) -> list[Trade]:
        return self.open

    async def reqCompletedOrdersAsync(self, apiOnly: bool) -> list[Trade]:
        assert apiOnly
        return self.completed

    async def reqExecutionsAsync(self) -> list[Fill]:
        return self.executions

    def placeOrder(self, contract: Contract, order: Order) -> Trade:
        self.placed.append((contract, order))
        trade = Trade(
            contract=contract, order=order, orderStatus=OrderStatus(status="PreSubmitted")
        )
        self.open = [t for t in self.open if t.order.orderRef != order.orderRef] + [trade]
        return trade

    def cancelOrder(self, order: Order) -> None:
        self.cancelled.append(order)


def _broker(fake: FakeIB) -> IbkrBroker:
    return IbkrBroker(cast(IB, fake))


def test_ib_order_fields() -> None:
    entry, target, stop = bracket_specs(REQ)
    o = ib_order(entry, 5, 0, transmit=False)
    assert (o.orderId, o.action, o.totalQuantity, o.orderType, o.lmtPrice, o.tif) == (
        5,
        "BUY",
        3,
        "LMT",
        100.05,
        "GTD",
    )
    assert (o.goodTillDate, o.outsideRth, o.transmit, o.orderRef) == (
        "20261006-20:00:00",
        False,
        False,
        entry.order_ref,
    )
    s = ib_order(stop, 7, 5, transmit=True)
    assert (s.orderType, s.auxPrice, s.parentId, s.ocaGroup, s.ocaType, s.tif) == (
        "STP",
        96.0,
        5,
        str(REQ.id),
        1,
        "GTC",
    )
    assert ib_order(target, 6, 5, transmit=False).goodTillDate == ""


async def test_bracket_is_sent_once_with_the_stop_transmitting() -> None:
    fake = FakeIB()
    broker = _broker(fake)
    out = await broker.place(bracket_specs(REQ))
    assert [o.status for o in out] == ["working"] * 3
    orders = [o for _, o in fake.placed]
    assert [(o.orderId, o.parentId, o.transmit) for o in orders] == [
        (101, 0, False),
        (102, 101, False),
        (103, 101, True),
    ]
    assert fake.placed[0][0].conId == 265598
    assert [o.broker_order_id for o in out] == [101, 102, 103]

    again = await broker.place(bracket_specs(REQ))  # e.g. after a restart
    assert len(fake.placed) == 3
    assert [o.order_ref for o in again] == [o.order_ref for o in out]


async def test_completed_and_executed_orders_are_not_resent() -> None:
    fake = FakeIB()
    entry, target, stop = bracket_specs(REQ)
    done = Trade(
        contract=Contract(conId=265598),
        order=Order(orderId=50, orderRef=entry.order_ref, permId=9),
        orderStatus=OrderStatus(status="Filled", filled=3, avgFillPrice=100.0),
    )
    fake.completed = [done]
    fake.executions = [
        Fill(
            Contract(),
            Execution(orderRef=target.order_ref),
            CommissionReport(),
            datetime(2026, 10, 6, tzinfo=UTC),
        )
    ]
    out = await _broker(fake).place([entry, target, stop])
    assert [o.status for o in out] == ["filled", "filled", "working"]
    assert out[0].avg_fill_price == D(100)
    assert out[0].perm_id == 9
    assert [(o.orderRef, o.parentId) for _, o in fake.placed] == [(stop.order_ref, 50)]


async def test_cancel_modify_orders_and_fills() -> None:
    fake = FakeIB()
    broker = _broker(fake)
    entry, _, stop = bracket_specs(REQ)
    await broker.place(bracket_specs(REQ))
    await broker.cancel(entry.order_ref)
    await broker.cancel("other:entry")
    assert [o.orderRef for o in fake.cancelled] == [entry.order_ref]

    await broker.modify(stop.model_copy(update={"stop_price": D("99.5")}))
    await broker.modify(stop.model_copy(update={"order_ref": "other:stop"}))
    contract, order = fake.placed[-1]
    assert (order.orderRef, order.auxPrice, order.transmit, contract.conId) == (
        stop.order_ref,
        99.5,
        True,
        265598,
    )
    target_spec = bracket_specs(REQ)[1]
    await broker.modify(target_spec.model_copy(update={"limit_price": D(110)}))
    assert fake.placed[-1][1].lmtPrice == 110.0

    fake.open[0].orderStatus = OrderStatus(status="Cancelled")
    fake.open.append(Trade(order=Order(orderRef=""), orderStatus=OrderStatus(status="Submitted")))
    statuses = {o.order_ref.split(":")[1]: o.status for o in await broker.orders()}
    assert statuses == {"entry": "inactive", "target": "working", "stop": "working"}

    t = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    fake.executions = [
        Fill(
            Contract(),
            Execution(execId="e1", orderRef=entry.order_ref, shares=3.0, price=100.04),
            CommissionReport(commission=1.0),
            t,
        ),
        Fill(
            Contract(),
            Execution(execId="e2", orderRef=entry.order_ref, shares=1.0, price=100.0),
            CommissionReport(commission=1.7976931348623157e308),  # not reported yet
            t,
        ),
        Fill(Contract(), Execution(execId="manual", orderRef=""), CommissionReport(), t),
    ]
    fills = await broker.fills()
    assert [(f.exec_id, f.quantity, f.price, f.commission) for f in fills] == [
        ("e1", 3, D("100.04"), D(1)),
        ("e2", 1, D(100), None),
    ]
