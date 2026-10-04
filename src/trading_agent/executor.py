"""Execution service (IMPLEMENTATION.md 10): approved entries become brackets at the broker,
and the broker's order states move them through the state machine (10.2).

Works with any `OrderBroker`: the simulator while the gateway is disabled, IBKR afterwards.
Every step is safe to repeat: brackets are keyed by the proposal id, orders by order_ref.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import structlog

from trading_agent.data.ingest import Sessions
from trading_agent.db import orders as repo
from trading_agent.domain.orders import (
    Acknowledged,
    Bracket,
    BracketEvent,
    BracketRequest,
    BrokerOrder,
    CancelRequested,
    EntryEnded,
    EntryFilled,
    ExitFilled,
    ExitRequested,
    OrderKind,
    OrderSpec,
    StopMoved,
    Submitted,
    order_ref,
)
from trading_agent.domain.risk import RiskDecision
from trading_agent.execution import fsm
from trading_agent.execution.orders import OrderBroker, bracket_specs, exit_spec

log = structlog.get_logger(__name__)

PRICE = Decimal("0.0001")
KIND_ORDER: dict[OrderKind, int] = {"entry": 0, "target": 1, "stop": 2, "exit": 3}


def _delta_price(known: BrokerOrder, now: BrokerOrder) -> Decimal:
    """Average price of the quantity filled since `known`."""
    if now.avg_fill_price is None:
        raise ValueError(f"{now.order_ref}: fill without a price")
    before = (known.avg_fill_price or Decimal(0)) * known.filled
    return ((now.avg_fill_price * now.filled - before) / (now.filled - known.filled)).quantize(
        PRICE
    )


def events(
    b: Bracket, spec: OrderSpec, known: BrokerOrder, now: BrokerOrder, at: datetime
) -> list[BracketEvent]:
    """What changed for one order between the stored status and the broker's."""
    out: list[BracketEvent] = []
    if spec.kind == "entry" and now.status == "working" and b.state == "submitted":
        out.append(Acknowledged())
    delta = now.filled - known.filled
    if delta > 0:
        price = _delta_price(known, now)
        if spec.kind == "entry":
            out.append(EntryFilled(quantity=delta, price=price))
        else:
            reason = spec.kind if spec.kind != "exit" else (b.exit_reason or "exit")
            out.append(ExitFilled(quantity=delta, price=price, reason=reason))
    if spec.kind == "entry" and now.status == "inactive":
        expired = b.cancel_reason is None and at >= b.expires
        out.append(EntryEnded(outcome="expired" if expired else "cancelled"))
    return out


class Executor:
    def __init__(
        self,
        sessions: Sessions,
        broker: OrderBroker,
        book: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.broker = broker
        self.book = book
        self.clock = clock

    async def submit(self, req: BracketRequest, decision: RiskDecision) -> Bracket:
        """Stores the bracket, then sends it. A repeat only re-sends what is still unsent."""
        if req.instrument.id is None:
            raise ValueError("instrument without a database id")
        specs = bracket_specs(req)
        new = Bracket(
            id=req.id,
            book=self.book,
            instrument_id=req.instrument.id,
            quantity=req.quantity,
            entry=req.entry,
            stop=req.stop,
            initial_stop=req.stop,
            target=req.target,
            expires=req.expires,
        )
        async with self.sessions.begin() as s:
            await repo.insert_bracket(s, new, specs, decision.model_dump(mode="json"), self.clock())
            stored = await repo.bracket(s, req.id)
        if stored is None or stored.state != "approved":
            return stored or new
        placed = await self.broker.place(specs)
        async with self.sessions.begin() as s:
            for o in placed:
                await repo.update_order(s, o)
            submitted = fsm.apply(stored, Submitted())
            await repo.save_bracket(s, submitted, "submitted")
        log.info("executor.submitted", bracket=str(req.id), symbol=req.instrument.yahoo_symbol)
        return submitted

    async def sync(self) -> list[Bracket]:
        """Broker orders and fills into the open brackets; returns those that changed."""
        at = self.clock()
        current = {o.order_ref: o for o in await self.broker.orders()}
        fills = await self.broker.fills()
        changed: list[Bracket] = []
        async with self.sessions.begin() as s:
            await repo.add_fills(s, fills)
            stored: dict[UUID, list[tuple[OrderSpec, BrokerOrder]]] = defaultdict(list)
            for spec, known, _ in await repo.orders(s):
                stored[spec.bracket_id].append((spec, known))
            for b in await repo.open_brackets(s, self.book):
                new, updates = b, list[BrokerOrder]()
                try:
                    for spec, known in sorted(stored[b.id], key=lambda x: KIND_ORDER[x[0].kind]):
                        now = current.get(spec.order_ref)
                        if now is None:
                            continue
                        for event in events(new, spec, known, now, at):
                            new = fsm.apply(new, event)
                        updates.append(now)
                except (fsm.InvalidTransition, ValueError) as exc:
                    # stored orders stay as they were, so the next sync sees the same change
                    log.error("executor.sync_failed", bracket=str(b.id), error=str(exc))
                    continue
                for o in updates:
                    await repo.update_order(s, o)
                if new != b:
                    await repo.save_bracket(s, new, new.state)
                    changed.append(new)
        return changed

    async def cancel_entry(self, bracket_id: UUID, reason: str) -> Bracket | None:
        """Asks the broker to cancel a not yet filled entry; `sync` records the outcome."""
        async with self.sessions.begin() as s:
            b = await repo.bracket(s, bracket_id, lock=True)
            if b is None or b.state not in ("approved", "submitted", "working"):
                return b
            b = fsm.apply(b, CancelRequested(reason=reason))
            await repo.save_bracket(s, b, "cancel_requested")
        await self.broker.cancel(order_ref(bracket_id, "entry"))
        return b

    async def request_exit(self, bracket_id: UUID, reason: str) -> Bracket | None:
        """Market sell of the open position (time stop, invalidation); safe to repeat."""
        async with self.sessions.begin() as s:
            b = await repo.bracket(s, bracket_id, lock=True)
            if b is None or b.state not in ("filled", "exiting"):
                return b
            specs = {spec.kind: spec for spec, _, _ in await repo.orders(s, [bracket_id])}
            spec = specs.get("exit") or exit_spec(b, specs["entry"].instrument)
            if b.state == "filled":
                await repo.add_orders(s, [spec], self.clock())
                b = fsm.apply(b, ExitRequested(reason=reason))
                await repo.save_bracket(s, b, "exit_requested")
        placed = await self.broker.place([spec])
        async with self.sessions.begin() as s:
            for o in placed:
                await repo.update_order(s, o)
        return b

    async def move_stop(self, bracket_id: UUID, stop: Decimal) -> Bracket | None:
        """Tightens the protective stop; raises InvalidTransition on any loosening."""
        async with self.sessions.begin() as s:
            b = await repo.bracket(s, bracket_id, lock=True)
            if b is None or stop == b.stop:
                return b
            b = fsm.apply(b, StopMoved(stop=stop))
            specs = {spec.kind: spec for spec, _, _ in await repo.orders(s, [bracket_id])}
            spec = specs["stop"].model_copy(update={"stop_price": stop})
            await repo.update_spec(s, spec)
            await repo.save_bracket(s, b, "stop_moved")
        await self.broker.modify(spec)
        return b

    async def open_brackets(self) -> Sequence[Bracket]:
        async with self.sessions() as s:
            return await repo.open_brackets(s, self.book)
