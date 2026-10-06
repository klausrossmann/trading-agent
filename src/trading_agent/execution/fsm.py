"""Bracket state machine (IMPLEMENTATION.md 10.2). Pure; an event that doesn't fit raises."""

from decimal import Decimal

from trading_agent.domain.orders import (
    Acknowledged,
    Bracket,
    BracketEvent,
    BracketState,
    CancelRequested,
    EntryEnded,
    EntryFilled,
    ExitFilled,
    ExitRequested,
    StopMoved,
    Submitted,
)

PRICE = Decimal("0.0001")


class InvalidTransition(ValueError):
    pass


def _require(b: Bracket, event: BracketEvent, *states: BracketState) -> None:
    if b.state not in states:
        raise InvalidTransition(f"{event.kind} in state {b.state}")


def _quantity(q: Decimal, available: Decimal) -> None:
    if not 0 < q <= available:
        raise InvalidTransition(f"quantity {q} outside 0..{available}")


def _average(qty: Decimal, price: Decimal | None, add_qty: Decimal, add_price: Decimal) -> Decimal:
    if price is None or qty == 0:
        return add_price
    return ((price * qty + add_price * add_qty) / (qty + add_qty)).quantize(PRICE)


def apply(b: Bracket, event: BracketEvent) -> Bracket:
    match event:
        case Submitted():
            _require(b, event, "approved")
            return b.model_copy(update={"state": "submitted"})
        case Acknowledged():
            if b.state == "working":
                return b
            _require(b, event, "submitted")
            return b.model_copy(update={"state": "working"})
        case EntryFilled(quantity=q, price=p):
            _require(b, event, "submitted", "working")
            _quantity(q, b.quantity - b.filled_qty)
            filled = b.filled_qty + q
            return b.model_copy(
                update={
                    "filled_qty": filled,
                    "entry_price": _average(b.filled_qty, b.entry_price, q, p),
                    "state": "filled" if filled == b.quantity else "working",
                }
            )
        case CancelRequested(reason=reason):
            _require(b, event, "approved", "submitted", "working")
            return b.model_copy(update={"cancel_reason": reason})
        case EntryEnded(outcome=outcome):
            if b.state in ("filled", "exiting", "closed"):
                return b  # the entry completed before; a late status changes nothing
            _require(b, event, "approved", "submitted", "working")
            if b.filled_qty == 0:
                return b.model_copy(update={"state": outcome})
            # a partial fill stays open, protected by its stop and target
            return b.model_copy(update={"state": "closed" if b.open_qty == 0 else "filled"})
        case ExitRequested(reason=reason):
            if b.state == "exiting":
                return b
            _require(b, event, "filled")
            return b.model_copy(update={"state": "exiting", "exit_reason": reason})
        case ExitFilled(quantity=q, price=p, reason=reason):
            _require(b, event, "working", "filled", "exiting")
            _quantity(q, b.open_qty)
            exit_qty = b.exit_qty + q
            state: BracketState = b.state  # while the entry still works, more may fill
            if b.state != "working":
                state = "closed" if exit_qty == b.filled_qty else "exiting"
            return b.model_copy(
                update={
                    "exit_qty": exit_qty,
                    "exit_price": _average(b.exit_qty, b.exit_price, q, p),
                    "exit_reason": b.exit_reason or reason,
                    "state": state,
                }
            )
        case StopMoved(stop=stop):  # pragma: no branch  # the match is exhaustive
            _require(b, event, "approved", "submitted", "working", "filled")
            if stop < b.stop:
                raise InvalidTransition("stops are only ever tightened")
            return b.model_copy(update={"stop": stop})
