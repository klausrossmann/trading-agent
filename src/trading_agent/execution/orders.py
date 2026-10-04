"""Order specs for a bracket (10.1) and the interface every broker implements."""

from collections.abc import Sequence
from typing import Protocol

from trading_agent.domain.market import Instrument
from trading_agent.domain.orders import (
    Bracket,
    BracketRequest,
    BrokerFill,
    BrokerOrder,
    OrderSpec,
    order_ref,
)


class OrderBroker(Protocol):
    async def place(self, specs: Sequence[OrderSpec]) -> list[BrokerOrder]:
        """Idempotent: an order_ref the broker already knows is not sent again."""
        ...

    async def cancel(self, order_ref: str) -> None: ...

    async def modify(self, spec: OrderSpec) -> None:
        """New prices for a working order with the same order_ref."""
        ...

    async def orders(self) -> list[BrokerOrder]:
        """Open and completed orders the broker still knows."""
        ...

    async def fills(self) -> list[BrokerFill]: ...


def bracket_specs(req: BracketRequest) -> list[OrderSpec]:
    """Entry LMT (GTD), then target LMT and stop STP (both GTC, one OCA group), stop last."""
    entry_ref = order_ref(req.id, "entry")
    entry = OrderSpec(
        order_ref=entry_ref,
        bracket_id=req.id,
        instrument=req.instrument,
        kind="entry",
        action="BUY",
        order_type="LMT",
        quantity=req.quantity,
        limit_price=req.entry,
        tif="GTD",
        good_till=req.expires,
    )
    child = entry.model_copy(
        update={
            "action": "SELL",
            "limit_price": None,
            "tif": "GTC",
            "good_till": None,
            "parent_ref": entry_ref,
            "oca_group": str(req.id),
        }
    )
    target = child.model_copy(
        update={
            "order_ref": order_ref(req.id, "target"),
            "kind": "target",
            "limit_price": req.target,
        }
    )
    stop = child.model_copy(
        update={
            "order_ref": order_ref(req.id, "stop"),
            "kind": "stop",
            "order_type": "STP",
            "stop_price": req.stop,
        }
    )
    return [entry, target, stop]


def exit_spec(b: Bracket, instrument: Instrument) -> OrderSpec:
    """Market sell of the open quantity, in the OCA group so stop and target cancel."""
    return OrderSpec(
        order_ref=order_ref(b.id, "exit"),
        bracket_id=b.id,
        instrument=instrument,
        kind="exit",
        action="SELL",
        order_type="MKT",
        quantity=b.open_qty,
        tif="DAY",
        oca_group=str(b.id),
    )
