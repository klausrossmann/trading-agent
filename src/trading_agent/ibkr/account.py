"""Read-only broker state (IMPLEMENTATION.md 10): account values and positions."""

from pydantic import BaseModel, ConfigDict


class AccountSnapshot(BaseModel):
    """Amounts in the account's base currency; never the account number."""

    model_config = ConfigDict(frozen=True)

    currency: str
    net_liquidation: float | None = None
    total_cash: float | None = None
    settled_cash: float | None = None
    available_funds: float | None = None


class BrokerPosition(BaseModel):
    model_config = ConfigDict(frozen=True)

    conid: int
    symbol: str  # IBKR symbol, e.g. "BRK B"
    currency: str
    quantity: float
    avg_cost: float
