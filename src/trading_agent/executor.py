"""Execution service (IMPLEMENTATION.md 10): approved entries become brackets at the broker,
and the broker's order states move them through the state machine (10.2).

Works with any `OrderBroker`: the simulator while the gateway is disabled, IBKR afterwards.
Every step is safe to repeat: brackets are keyed by the proposal id, orders by order_ref.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from typing import Any
from uuid import UUID

import structlog

from trading_agent.data.ingest import Sessions, describe_error
from trading_agent.db import orders as repo
from trading_agent.db import steps as steps_repo
from trading_agent.domain.market import BERLIN, Instrument
from trading_agent.domain.orders import (
    Acknowledged,
    Bracket,
    BracketEvent,
    BracketRequest,
    BrokerFill,
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
)
from trading_agent.domain.risk import RiskDecision
from trading_agent.domain.steps import Step, StepName, StepStatus
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


def _step(
    inst: Instrument,
    bracket_id: UUID,
    at: datetime,
    name: StepName,
    status: StepStatus,
    summary: str,
    **more: Any,
) -> Step:
    return Step(
        day=at.astimezone(BERLIN).date(),
        market=inst.market,
        instrument_id=inst.id,
        symbol=inst.yahoo_symbol,
        step=name,
        status=status,
        summary=summary,
        proposal_id=bracket_id,
        **more,
    )


def _num(value: Decimal) -> str:
    """100.0500 -> 100.05, 3.0000 -> 3"""
    return f"{value.normalize():f}"


def _order_text(spec: OrderSpec) -> str:
    text = f"{spec.kind} {spec.action} {_num(spec.quantity)} {spec.order_type}"
    price = spec.limit_price if spec.limit_price is not None else spec.stop_price
    return f"{text} {_num(price)}" if price is not None else text


def _specs_json(specs: Sequence[OrderSpec]) -> dict[str, Any]:
    return {"orders": [s.model_dump(mode="json", exclude={"instrument"}) for s in specs]}


def _orders_json(orders: Sequence[BrokerOrder]) -> dict[str, Any]:
    return {"orders": [o.model_dump(mode="json") for o in orders]}


def sync_steps(
    spec: OrderSpec,
    known: BrokerOrder,
    now: BrokerOrder,
    found: Sequence[BracketEvent],
    fills: Sequence[BrokerFill],
    at: datetime,
) -> list[Step]:
    """What one order's sync changed, as steps: the broker's new status or message, fills,
    an exit, an entry that ended."""
    step = partial(_step, spec.instrument, spec.bracket_id, at, order_ref=spec.order_ref)
    out: list[Step] = []
    new_message = now.message if now.message and now.message != known.message else None
    if now.status != known.status or new_message:
        summary = f"{spec.kind} order {known.status} -> {now.status}"
        failed = new_message is not None and now.status == "inactive"
        out.append(
            step(
                "broker",
                "error" if failed else "info",
                f"{summary}: {new_message}" if new_message else summary,
                output=now.model_dump(mode="json"),
                error=new_message,
            )
        )
    executions = {"fills": [f.model_dump(mode="json") for f in fills]}
    for e in found:
        if isinstance(e, EntryFilled):
            summary = f"bought {_num(e.quantity)} at {_num(e.price)}"
            out.append(step("fill", "passed", summary, output=executions))
        elif isinstance(e, ExitFilled):
            summary = f"sold {_num(e.quantity)} at {_num(e.price)} ({e.reason})"
            out.append(step("exit", "passed", summary, output=executions))
        elif isinstance(e, EntryEnded):
            summary = f"entry {e.outcome}" + (f": {now.message}" if now.message else "")
            out.append(step("cancel", "stopped", summary, error=now.message))
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
        step = partial(_step, req.instrument, req.id, self.clock(), "order")
        sent = "; ".join(_order_text(x) for x in specs)
        try:
            placed = await self.broker.place(specs)
        except Exception as exc:
            async with self.sessions.begin() as s:
                failed = step(
                    "error",
                    f"not sent to {self.broker.name}: {sent}",
                    input=_specs_json(specs),
                    error=describe_error(exc),
                )
                await steps_repo.add_steps(s, [failed])
            raise
        problems = [
            f"{o.order_ref}: {o.message or 'inactive'}"
            for o in placed
            if o.message or o.status == "inactive"
        ]
        async with self.sessions.begin() as s:
            for o in placed:
                await repo.update_order(s, o)
            submitted = fsm.apply(stored, Submitted())
            await repo.save_bracket(s, submitted, "submitted")
            done = step(
                "error" if problems else "passed",
                f"sent to {self.broker.name}: {sent}",
                input=_specs_json(specs),
                output=_orders_json(placed),
                error="; ".join(problems) or None,
                order_ref=specs[0].order_ref,
            )
            await steps_repo.add_steps(s, [done])
        log.info("executor.submitted", bracket=str(req.id), symbol=req.instrument.yahoo_symbol)
        return submitted

    async def sync(self) -> list[Bracket]:
        """Broker orders and fills into the open brackets; returns those that changed."""
        at = self.clock()
        current = {o.order_ref: o for o in await self.broker.orders()}
        fills = await self.broker.fills()
        by_ref: dict[str, list[BrokerFill]] = defaultdict(list)
        for f in fills:
            by_ref[f.order_ref].append(f)
        changed: list[Bracket] = []
        async with self.sessions.begin() as s:
            await repo.add_fills(s, fills)
            stored: dict[UUID, list[tuple[OrderSpec, BrokerOrder]]] = defaultdict(list)
            for spec, known, _ in await repo.orders(s):
                stored[spec.bracket_id].append((spec, known))
            for b in await repo.open_brackets(s, self.book):
                new, updates, steps = b, list[BrokerOrder](), list[Step]()
                ordered = sorted(stored[b.id], key=lambda x: KIND_ORDER[x[0].kind])
                try:
                    for spec, known in ordered:
                        now = current.get(spec.order_ref)
                        if now is None:
                            continue
                        found = events(new, spec, known, now, at)
                        for event in found:
                            new = fsm.apply(new, event)
                        updates.append(now)
                        steps += sync_steps(spec, known, now, found, by_ref[spec.order_ref], at)
                except (fsm.InvalidTransition, ValueError) as exc:
                    # stored orders stay as they were, so the next sync sees the same change
                    log.error("executor.sync_failed", bracket=str(b.id), error=str(exc))
                    if ordered:
                        spec = ordered[0][0]
                        failed = _step(
                            spec.instrument,
                            b.id,
                            at,
                            "broker",
                            "error",
                            "sync failed",
                            error=str(exc),
                        )
                        await steps_repo.add_steps(s, [failed])
                    continue
                for o in updates:
                    await repo.update_order(s, o)
                await steps_repo.add_steps(s, steps)
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
            entry = {spec.kind: spec for spec, _, _ in await repo.orders(s, [bracket_id])}["entry"]
            requested = _step(
                entry.instrument,
                bracket_id,
                self.clock(),
                "cancel",
                "info",
                f"entry cancel requested: {reason}",
                order_ref=entry.order_ref,
            )
            await steps_repo.add_steps(s, [requested])
        await self.broker.cancel(entry.order_ref)
        return b

    async def request_exit(self, bracket_id: UUID, reason: str) -> Bracket | None:
        """Market sell of the open position (time stop, invalidation); safe to repeat."""
        async with self.sessions.begin() as s:
            b = await repo.bracket(s, bracket_id, lock=True)
            if b is None or b.state not in ("filled", "exiting"):
                return b
            first = b.state == "filled"
            specs = {spec.kind: spec for spec, _, _ in await repo.orders(s, [bracket_id])}
            spec = specs.get("exit") or exit_spec(b, specs["entry"].instrument)
            if first:
                await repo.add_orders(s, [spec], self.clock())
                b = fsm.apply(b, ExitRequested(reason=reason))
                await repo.save_bracket(s, b, "exit_requested")
        step = partial(
            _step,
            spec.instrument,
            bracket_id,
            self.clock(),
            "exit",
            summary=f"exit ({reason}) sent to {self.broker.name}: {_order_text(spec)}",
            input=_specs_json([spec]),
            order_ref=spec.order_ref,
        )
        try:
            placed = await self.broker.place([spec])
        except Exception as exc:
            async with self.sessions.begin() as s:
                await steps_repo.add_steps(s, [step(status="error", error=describe_error(exc))])
            raise
        problems = "; ".join(o.message for o in placed if o.message) or None
        async with self.sessions.begin() as s:
            for o in placed:
                await repo.update_order(s, o)
            sent = step(
                status="error" if problems else "info", output=_orders_json(placed), error=problems
            )
            if first or problems:
                await steps_repo.add_steps(s, [sent])
        return b

    async def move_stop(self, bracket_id: UUID, stop: Decimal) -> Bracket | None:
        """Tightens the protective stop; raises InvalidTransition on any loosening."""
        async with self.sessions.begin() as s:
            b = await repo.bracket(s, bracket_id, lock=True)
            if b is None or stop == b.stop:
                return b
            old = b.stop
            b = fsm.apply(b, StopMoved(stop=stop))
            specs = {spec.kind: spec for spec, _, _ in await repo.orders(s, [bracket_id])}
            spec = specs["stop"].model_copy(update={"stop_price": stop})
            await repo.update_spec(s, spec)
            await repo.save_bracket(s, b, "stop_moved")
            moved = _step(
                spec.instrument,
                bracket_id,
                self.clock(),
                "stop",
                "info",
                f"stop moved {_num(old)} -> {_num(stop)} at {self.broker.name}",
                order_ref=spec.order_ref,
            )
            await steps_repo.add_steps(s, [moved])
        await self.broker.modify(spec)
        return b

    async def open_brackets(self) -> Sequence[Bracket]:
        async with self.sessions() as s:
            return await repo.open_brackets(s, self.book)
