"""Named price levels: the only prices an LLM may choose from."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

LevelKind = Literal[
    "price", "ma", "band", "support", "resistance", "swing", "fib", "range", "atr_stop"
]


class LevelRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str  # e.g. "sma50", "support_1", "fib_618", "atr_stop_2x"
    price: Decimal
    kind: LevelKind
    touches: int | None = None  # for support/resistance zones
