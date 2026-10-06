"""Simulator broker (10.5): the OrderBroker interface, filled from daily bars.

Stands in for IBKR while the gateway is disabled, and in tests. Its whole state is the order
list; `restore` rebuilds it from the stored orders after a restart. Same fill model as the
backtest: on the entry bar only the stop is checked, and the stop wins an ambiguous bar.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from trading_agent.domain.numbers import to_decimal
from trading_agent.domain.orders import BrokerFill, BrokerOrder, BrokerStatus, OrderKind, OrderSpec
from trading_agent.execution import sim

PRICE = Decimal("0.0001")
ZERO = Decimal(0)


@dataclass
class _Order:
    spec: OrderSpec
    placed_at: datetime
    status: BrokerStatus = "working"
    filled: Decimal = ZERO
    avg: Decimal | None = None

    def view(self) -> BrokerOrder:
        return BrokerOrder(
            order_ref=self.spec.order_ref,
            status=self.status,
            filled=self.filled,
            avg_fill_price=self.avg,
        )


def _float(value: Decimal | None) -> float:
    if value is None:
        raise ValueError("order without the price its type needs")
    return float(value)


def _deactivate(orders: Iterable["_Order"]) -> None:
    for o in orders:
        if o.status == "working":
            o.status = "inactive"


class SimBroker:
    def __init__(
        self, slippage_pct: float = 0.05, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.slippage_pct = slippage_pct
        self.clock = clock
        self._orders: dict[str, _Order] = {}
        self._fills: list[BrokerFill] = []

    def restore(self, orders: Iterable[tuple[OrderSpec, BrokerOrder, datetime]]) -> None:
        for spec, state, placed_at in orders:
            self._orders[spec.order_ref] = _Order(
                spec, placed_at, state.status, state.filled, state.avg_fill_price
            )

    async def place(self, specs: Sequence[OrderSpec]) -> list[BrokerOrder]:
        now = self.clock()
        return [self._orders.setdefault(s.order_ref, _Order(s, now)).view() for s in specs]

    async def cancel(self, order_ref: str) -> None:
        o = self._orders.get(order_ref)
        if o is None or o.status != "working":
            return
        o.status = "inactive"
        if o.spec.kind == "entry" and o.filled == 0:
            _deactivate(self._group(o.spec)[1].values())

    async def modify(self, spec: OrderSpec) -> None:
        o = self._orders.get(spec.order_ref)
        if o is not None and o.status == "working":
            o.spec = spec

    async def orders(self) -> list[BrokerOrder]:
        return [o.view() for o in self._orders.values()]

    async def fills(self) -> list[BrokerFill]:
        return list(self._fills)

    def _group(self, spec: OrderSpec) -> tuple[_Order | None, dict[OrderKind, _Order]]:
        """(entry, exit-side orders) of the bracket."""
        kinds: dict[OrderKind, _Order] = {
            o.spec.kind: o for o in self._orders.values() if o.spec.bracket_id == spec.bracket_id
        }
        entry = kinds.pop("entry", None)
        return entry, kinds

    def _fill(self, o: _Order, qty: Decimal, price: float, at: datetime) -> None:
        p = to_decimal(price)
        o.avg = (
            p
            if o.avg is None
            else ((o.avg * o.filled + p * qty) / (o.filled + qty)).quantize(PRICE)
        )
        o.filled += qty
        o.status = "filled"
        self._fills.append(
            BrokerFill(
                exec_id=f"sim:{o.spec.order_ref}:{at:%Y%m%d}",
                order_ref=o.spec.order_ref,
                ts=at,
                quantity=qty,
                price=p,
            )
        )

    def _exit(
        self,
        exits: dict[OrderKind, _Order],
        kind: OrderKind,
        qty: Decimal,
        price: float,
        at: datetime,
    ) -> None:
        self._fill(exits[kind], qty, price, at)
        _deactivate(exits.values())  # OCA

    def on_bar(self, instrument_id: int, bar: sim.Bar, at: datetime) -> None:
        """The session of `instrument_id` that ended at `at`; orders placed later wait."""
        slip = self.slippage_pct
        brackets: dict[UUID, list[_Order]] = defaultdict(list)
        for o in self._orders.values():
            if o.spec.instrument.id == instrument_id and o.placed_at < at:
                brackets[o.spec.bracket_id].append(o)
        for orders in brackets.values():
            entry, exits = self._group(orders[0].spec)
            stop, target = exits.get("stop"), exits.get("target")
            if entry is not None and entry.status == "working":
                price = sim.fill_entry(bar, _float(entry.spec.limit_price), slip)
                if price is None:
                    if entry.spec.good_till is not None and at >= entry.spec.good_till:
                        entry.status = "inactive"
                        _deactivate(exits.values())
                    continue
                self._fill(entry, entry.spec.quantity - entry.filled, price, at)
                if stop is not None and stop.status == "working":
                    hit = sim.check_exit(bar, _float(stop.spec.stop_price), None, slip)
                    if hit is not None:
                        self._exit(exits, "stop", entry.filled, hit.price, at)
                continue
            held = (entry.filled if entry else ZERO) - sum((o.filled for o in exits.values()), ZERO)
            market = exits.get("exit")
            if held <= 0:
                continue
            if market is not None and market.status == "working":
                self._exit(exits, "exit", held, sim.open_exit(bar, slip).price, at)
            elif stop is not None and stop.status == "working":
                live = target if target is not None and target.status == "working" else None
                hit = sim.check_exit(
                    bar,
                    _float(stop.spec.stop_price),
                    _float(live.spec.limit_price) if live else None,
                    slip,
                )
                if hit is not None:
                    self._exit(
                        exits, "stop" if hit.reason == "stop" else "target", held, hit.price, at
                    )
