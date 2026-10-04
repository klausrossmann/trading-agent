"""Orders and brackets (IMPLEMENTATION.md 10). Prices in the instrument currency."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading_agent.domain.market import Instrument

OrderKind = Literal["entry", "target", "stop", "exit"]
OrderType = Literal["LMT", "STP", "MKT"]
TimeInForce = Literal["GTD", "GTC", "DAY"]
BrokerStatus = Literal["pending", "working", "filled", "inactive"]  # inactive: cancelled/expired
BracketState = Literal[
    "approved", "submitted", "working", "filled", "exiting", "closed", "expired", "cancelled"
]
TERMINAL: frozenset[BracketState] = frozenset({"closed", "expired", "cancelled"})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def order_ref(bracket_id: UUID, kind: OrderKind) -> str:
    """The idempotency key at the broker (10.1)."""
    return f"{bracket_id}:{kind}"


class OrderSpec(_Frozen):
    """One broker order as we intend it; stored before it is sent."""

    order_ref: str
    bracket_id: UUID
    instrument: Instrument
    kind: OrderKind
    action: Literal["BUY", "SELL"]
    order_type: OrderType
    quantity: int
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: TimeInForce
    good_till: datetime | None = None  # GTD only
    parent_ref: str | None = None  # children of the entry
    oca_group: str | None = None  # stop, target and exit cancel each other


class BrokerOrder(_Frozen):
    """The broker's view of one order."""

    order_ref: str
    status: BrokerStatus
    filled: int = 0
    avg_fill_price: Decimal | None = None
    broker_order_id: int | None = None
    perm_id: int | None = None


class BrokerFill(_Frozen):
    exec_id: str
    order_ref: str
    ts: datetime
    quantity: int
    price: Decimal
    commission: Decimal | None = None


class BracketRequest(_Frozen):
    """An approved entry: what the risk engine sized, ready to become a bracket."""

    id: UUID  # the proposal's id, so one proposal can never become two brackets
    instrument: Instrument
    quantity: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    expires: datetime  # GTD of the entry: end of the second session


class Bracket(_Frozen):
    """Lifecycle of one trade at the broker (10.2)."""

    id: UUID
    book: str
    instrument_id: int
    state: BracketState = "approved"
    quantity: int
    entry: Decimal
    stop: Decimal  # current protective stop
    initial_stop: Decimal
    target: Decimal
    expires: datetime
    filled_qty: int = 0
    entry_price: Decimal | None = None  # average fill
    exit_qty: int = 0
    exit_price: Decimal | None = None  # average fill
    exit_reason: str | None = None  # stop, target, time, invalidated, ...
    cancel_reason: str | None = None  # set when we asked the broker to cancel the entry

    @property
    def open_qty(self) -> int:
        return self.filled_qty - self.exit_qty


# --- events that move a bracket through its states ---


class Submitted(_Frozen):
    kind: Literal["submitted"] = "submitted"


class Acknowledged(_Frozen):
    kind: Literal["acknowledged"] = "acknowledged"


class EntryFilled(_Frozen):
    kind: Literal["entry_filled"] = "entry_filled"
    quantity: int
    price: Decimal


class CancelRequested(_Frozen):
    kind: Literal["cancel_requested"] = "cancel_requested"
    reason: str


class EntryEnded(_Frozen):
    """The entry order is no longer working (GTD reached or cancelled) without a full fill."""

    kind: Literal["entry_ended"] = "entry_ended"
    outcome: Literal["expired", "cancelled"]


class ExitRequested(_Frozen):
    kind: Literal["exit_requested"] = "exit_requested"
    reason: str


class ExitFilled(_Frozen):
    kind: Literal["exit_filled"] = "exit_filled"
    quantity: int
    price: Decimal
    reason: str


class StopMoved(_Frozen):
    kind: Literal["stop_moved"] = "stop_moved"
    stop: Decimal


BracketEvent = (
    Submitted
    | Acknowledged
    | EntryFilled
    | CancelRequested
    | EntryEnded
    | ExitRequested
    | ExitFilled
    | StopMoved
)
