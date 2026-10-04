"""Order specs for a bracket and the simulator broker's daily-bar fills."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_agent.domain.market import Instrument
from trading_agent.domain.orders import Bracket, BracketRequest, BrokerOrder, order_ref
from trading_agent.execution.orders import bracket_specs, exit_spec
from trading_agent.execution.sim import Bar
from trading_agent.execution.sim_broker import SimBroker

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
)
PLACED = datetime(2026, 10, 5, 13, 45, tzinfo=UTC)
DAY1 = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)  # session closes
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)


def request(**kw: object) -> BracketRequest:
    values: dict[str, object] = {
        "id": uuid4(),
        "instrument": AAPL,
        "quantity": 3,
        "entry": D(100),
        "stop": D(96),
        "target": D(108),
        "expires": DAY2,
    }
    return BracketRequest.model_validate(values | kw)


def test_bracket_specs() -> None:
    req = request()
    entry, target, stop = bracket_specs(req)
    assert entry.order_ref == order_ref(req.id, "entry") == f"{req.id}:entry"
    assert (entry.action, entry.order_type, entry.limit_price, entry.tif, entry.good_till) == (
        "BUY",
        "LMT",
        D(100),
        "GTD",
        DAY2,
    )
    assert (entry.parent_ref, entry.oca_group) == (None, None)
    assert (target.action, target.order_type, target.limit_price, target.tif) == (
        "SELL",
        "LMT",
        D(108),
        "GTC",
    )
    assert (stop.order_type, stop.stop_price, stop.limit_price, stop.good_till) == (
        "STP",
        D(96),
        None,
        None,
    )
    for child in (target, stop):
        assert (child.parent_ref, child.oca_group, child.quantity) == (
            entry.order_ref,
            str(req.id),
            3,
        )


def test_exit_spec_sells_the_open_quantity() -> None:
    b = Bracket(
        id=uuid4(),
        book="agent_paper",
        instrument_id=7,
        state="filled",
        quantity=3,
        entry=D(100),
        stop=D(96),
        initial_stop=D(96),
        target=D(108),
        expires=DAY2,
        filled_qty=2,
    )
    spec = exit_spec(b, AAPL)
    assert (spec.kind, spec.order_type, spec.action, spec.quantity, spec.tif) == (
        "exit",
        "MKT",
        "SELL",
        2,
        "DAY",
    )
    assert spec.oca_group == str(b.id)


async def placed(req: BracketRequest, broker: SimBroker | None = None) -> SimBroker:
    broker = broker or SimBroker(clock=lambda: PLACED)
    await broker.place(bracket_specs(req))
    return broker


async def status(broker: SimBroker) -> dict[str, tuple[str, int, Decimal | None]]:
    return {
        o.order_ref.split(":")[1]: (o.status, o.filled, o.avg_fill_price)
        for o in await broker.orders()
    }


async def test_place_is_idempotent() -> None:
    req = request()
    broker = await placed(req)
    again = await broker.place(bracket_specs(req))
    assert [o.status for o in again] == ["working"] * 3
    assert len(await broker.orders()) == 3


async def test_entry_then_target() -> None:
    broker = await placed(request())
    broker.on_bar(7, Bar(101, 102, 99, 101), DAY1)
    s = await status(broker)
    assert s["entry"] == ("filled", 3, D("100.05"))  # limit 100 + 0.05 % slippage
    assert s["stop"][0] == s["target"][0] == "working"
    broker.on_bar(7, Bar(104, 109, 103, 108), DAY2)
    s = await status(broker)
    assert s["target"] == ("filled", 3, D("107.946"))
    assert s["stop"][0] == "inactive"
    fills = await broker.fills()
    assert [(f.order_ref.split(":")[1], f.quantity, f.ts) for f in fills] == [
        ("entry", 3, DAY1),
        ("target", 3, DAY2),
    ]
    assert fills[0].exec_id == f"sim:{fills[0].order_ref}:20261005"
    broker.on_bar(7, Bar(90, 91, 80, 85), DAY3)  # nothing left to fill
    assert len(await broker.fills()) == 2


async def test_stopped_out_on_the_entry_bar() -> None:
    broker = await placed(request())
    broker.on_bar(7, Bar(101, 109, 95, 97), DAY1)  # the target is not checked on the entry bar
    s = await status(broker)
    assert s["stop"] == ("filled", 3, D("95.952"))
    assert s["target"][0] == "inactive"


async def test_stop_gap_on_a_later_bar() -> None:
    broker = await placed(request())
    broker.on_bar(7, Bar(101, 102, 99, 101), DAY1)
    broker.on_bar(7, Bar(90, 92, 89, 91), DAY2)
    assert (await status(broker))["stop"][:2] == ("filled", 3)


async def test_entry_expires_at_good_till() -> None:
    broker = await placed(request())
    broker.on_bar(7, Bar(105, 106, 104, 105), DAY1)
    assert (await status(broker))["entry"][0] == "working"
    broker.on_bar(7, Bar(105, 106, 104, 105), DAY2)
    assert {k: v[0] for k, v in (await status(broker)).items()} == {
        "entry": "inactive",
        "target": "inactive",
        "stop": "inactive",
    }


async def test_orders_placed_after_the_session_wait() -> None:
    broker = await placed(request())
    broker.on_bar(7, Bar(99, 100, 98, 99), PLACED)  # session ended when the order was placed
    broker.on_bar(8, Bar(99, 100, 98, 99), DAY1)  # another instrument
    assert (await status(broker))["entry"][0] == "working"


async def test_cancel_and_modify() -> None:
    req = request()
    broker = await placed(req)
    entry, _, stop = bracket_specs(req)
    await broker.modify(stop.model_copy(update={"stop_price": D(97)}))
    await broker.cancel(entry.order_ref)
    await broker.cancel(entry.order_ref)  # twice is fine
    await broker.cancel("unknown:entry")
    await broker.modify(stop.model_copy(update={"stop_price": D(98)}))  # inactive: ignored
    assert {k: v[0] for k, v in (await status(broker)).items()} == {
        "entry": "inactive",
        "target": "inactive",
        "stop": "inactive",
    }


async def test_modified_stop_is_used() -> None:
    req = request()
    broker = await placed(req)
    broker.on_bar(7, Bar(101, 102, 99, 101), DAY1)
    await broker.modify(bracket_specs(req)[2].model_copy(update={"stop_price": D(100)}))
    broker.on_bar(7, Bar(101, 102, 99.5, 100), DAY2)
    assert (await status(broker))["stop"] == ("filled", 3, D("99.95"))


async def test_market_exit_fills_at_the_open() -> None:
    req = request()
    broker = await placed(req)
    broker.on_bar(7, Bar(101, 102, 99, 101), DAY1)
    b = Bracket(
        id=req.id,
        book="agent_paper",
        instrument_id=7,
        state="filled",
        quantity=3,
        entry=D(100),
        stop=D(96),
        initial_stop=D(96),
        target=D(108),
        expires=DAY2,
        filled_qty=3,
    )
    await broker.place([exit_spec(b, AAPL)])
    broker.on_bar(7, Bar(103, 109, 95, 104), DAY2)
    s = await status(broker)
    assert s["exit"] == ("filled", 3, D("102.9485"))
    assert s["stop"][0] == s["target"][0] == "inactive"


async def test_restore_continues_where_it_stopped() -> None:
    req = request()
    specs = bracket_specs(req)
    broker = SimBroker()
    broker.restore(
        [
            (
                specs[0],
                BrokerOrder(
                    order_ref=specs[0].order_ref, status="filled", filled=3, avg_fill_price=D(100)
                ),
                PLACED,
            ),
            (specs[1], BrokerOrder(order_ref=specs[1].order_ref, status="working"), PLACED),
            (specs[2], BrokerOrder(order_ref=specs[2].order_ref, status="working"), PLACED),
        ]
    )
    broker.on_bar(7, Bar(97, 98, 95, 96), DAY2)
    assert (await status(broker))["stop"][:2] == ("filled", 3)


async def test_quiet_bars_and_missing_children() -> None:
    req = request()
    _, target, stop = bracket_specs(req)
    broker = await placed(req)
    broker.on_bar(7, Bar(101, 102, 99, 101), DAY1)
    broker.on_bar(7, Bar(101, 103, 98, 102), DAY2)  # neither stop nor target
    await broker.cancel(target.order_ref)  # a child, so the stop stays
    assert (await status(broker))["stop"][0] == "working"
    await broker.cancel(stop.order_ref)
    broker.on_bar(7, Bar(90, 91, 80, 85), DAY3)  # unprotected: nothing fills
    assert (await status(broker))["stop"] == ("inactive", 0, None)

    lone = request()  # an entry without children
    await broker.place(bracket_specs(lone)[:1])
    broker.on_bar(7, Bar(101, 102, 95, 97), DAY3 + timedelta(days=1))
    assert len([f for f in await broker.fills() if f.order_ref.startswith(str(lone.id))]) == 1


async def test_a_stop_order_needs_its_price() -> None:
    req = request()
    entry, target, stop = bracket_specs(req)
    broker = SimBroker(clock=lambda: PLACED)
    await broker.place([entry, target, stop.model_copy(update={"stop_price": None})])
    with pytest.raises(ValueError, match="price"):
        broker.on_bar(7, Bar(101, 102, 99, 101), DAY1)
