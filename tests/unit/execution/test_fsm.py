"""Bracket state machine (10.2): every transition, partial fills, and what must raise."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_agent.domain.orders import (
    Acknowledged,
    Bracket,
    BracketEvent,
    CancelRequested,
    EntryEnded,
    EntryFilled,
    ExitFilled,
    ExitRequested,
    StopMoved,
    Submitted,
)
from trading_agent.execution.fsm import InvalidTransition, apply

D = Decimal
NEW = Bracket(
    id=uuid4(),
    book="agent_paper",
    instrument_id=1,
    quantity=3,
    entry=D(100),
    stop=D(96),
    initial_stop=D(96),
    target=D(108),
    expires=datetime(2026, 10, 7, 20, 0, tzinfo=UTC),
)


def run(b: Bracket, *events: BracketEvent) -> Bracket:
    for e in events:
        b = apply(b, e)
    return b


WORKING = run(NEW, Submitted(), Acknowledged())
FILLED = run(WORKING, EntryFilled(quantity=3, price=D("99.5")))


def test_happy_path_to_target() -> None:
    assert run(NEW, Submitted()).state == "submitted"
    assert WORKING.state == "working"
    assert apply(WORKING, Acknowledged()) == WORKING
    assert (FILLED.state, FILLED.filled_qty, FILLED.entry_price) == ("filled", 3, D("99.5"))
    closed = apply(FILLED, ExitFilled(quantity=3, price=D(108), reason="target"))
    assert (closed.state, closed.exit_qty, closed.exit_price, closed.exit_reason) == (
        "closed",
        3,
        D(108),
        "target",
    )


def test_partial_fills_average_the_price() -> None:
    b = run(WORKING, EntryFilled(quantity=1, price=D(100)), EntryFilled(quantity=2, price=D(99)))
    assert (b.state, b.entry_price) == ("filled", D("99.3333"))
    b = run(b, ExitFilled(quantity=1, price=D(96), reason="stop"))
    assert (b.state, b.open_qty) == ("exiting", 2)
    b = apply(b, ExitFilled(quantity=2, price=D(95), reason="stop"))
    assert (b.state, b.exit_price) == ("closed", D("95.3333"))


def test_fill_straight_from_submitted() -> None:
    assert run(NEW, Submitted(), EntryFilled(quantity=3, price=D(100))).state == "filled"


@pytest.mark.parametrize(("outcome"), ["expired", "cancelled"])
def test_unfilled_entry_ends(outcome: str) -> None:
    ended = apply(WORKING, EntryEnded(outcome=outcome))  # pyright: ignore[reportArgumentType]
    assert ended.state == outcome


def test_partial_entry_is_kept_when_it_ends() -> None:
    partial = apply(WORKING, EntryFilled(quantity=2, price=D(100)))
    assert partial.state == "working"
    kept = apply(partial, EntryEnded(outcome="expired"))
    assert (kept.state, kept.filled_qty, kept.open_qty) == ("filled", 2, 2)
    # stopped out while the entry still worked, then the entry ended: nothing is left
    stopped = apply(partial, ExitFilled(quantity=2, price=D(96), reason="stop"))
    assert stopped.state == "working"
    assert apply(stopped, EntryEnded(outcome="expired")).state == "closed"


def test_late_entry_status_changes_nothing() -> None:
    assert apply(FILLED, EntryEnded(outcome="cancelled")) == FILLED


def test_cancel_request_is_remembered() -> None:
    b = apply(WORKING, CancelRequested(reason="halted"))
    assert (b.state, b.cancel_reason) == ("working", "halted")
    with pytest.raises(InvalidTransition):
        apply(FILLED, CancelRequested(reason="halted"))


def test_requested_exit_keeps_its_reason() -> None:
    exiting = apply(FILLED, ExitRequested(reason="time"))
    assert exiting.state == "exiting"
    assert apply(exiting, ExitRequested(reason="other")) == exiting
    closed = apply(exiting, ExitFilled(quantity=3, price=D(101), reason="exit"))
    assert (closed.state, closed.exit_reason) == ("closed", "time")


def test_stops_only_tighten() -> None:
    assert apply(FILLED, StopMoved(stop=D("99.5"))).stop == D("99.5")
    assert apply(FILLED, StopMoved(stop=D(96))).stop == D(96)
    with pytest.raises(InvalidTransition, match="only ever tightened"):
        apply(FILLED, StopMoved(stop=D(95)))
    closed = apply(FILLED, ExitFilled(quantity=3, price=D(96), reason="stop"))
    with pytest.raises(InvalidTransition):
        apply(closed, StopMoved(stop=D(97)))


@pytest.mark.parametrize(
    ("b", "event"),
    [
        (WORKING, Submitted()),
        (NEW, Acknowledged()),
        (NEW, EntryFilled(quantity=1, price=D(100))),
        (WORKING, EntryFilled(quantity=4, price=D(100))),
        (WORKING, EntryFilled(quantity=0, price=D(100))),
        (FILLED, EntryFilled(quantity=1, price=D(100))),
        (WORKING, ExitRequested(reason="time")),
        (WORKING, ExitFilled(quantity=1, price=D(96), reason="stop")),  # nothing filled yet
        (FILLED, ExitFilled(quantity=4, price=D(96), reason="stop")),
        (NEW, ExitFilled(quantity=1, price=D(96), reason="stop")),
        (run(WORKING, EntryEnded(outcome="expired")), EntryEnded(outcome="expired")),
    ],
)
def test_events_that_do_not_fit_raise(b: Bracket, event: BracketEvent) -> None:
    with pytest.raises(InvalidTransition):
        apply(b, event)
