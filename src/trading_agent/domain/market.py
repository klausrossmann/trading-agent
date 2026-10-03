"""Market data domain types. Prices are Decimal; calculators convert to float themselves."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

Market = Literal["US", "EU"]
Currency = Literal["USD", "EUR"]
EarningsTiming = Literal["bmo", "amc", "during", "unknown"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Instrument(_Frozen):
    id: int | None = None
    symbol: str  # exchange ticker, e.g. "BRK.B", "SAP"
    yahoo_symbol: str  # e.g. "BRK-B", "SAP.DE"
    name: str
    market: Market
    exchange: str  # IBKR routing: "SMART" for US, "IBIS" (Xetra) for DE
    currency: Currency
    kind: Literal["stock", "etf"]
    sector: str | None = None
    indices: tuple[str, ...] = ()  # empty for benchmark-only instruments
    conid: int | None = None


class Bar(_Frozen):
    """Daily OHLCV, split-adjusted but not dividend-adjusted (actual traded prices)."""

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class BarSeries(_Frozen):
    yahoo_symbol: str
    source: str
    fetched_at: datetime
    bars: tuple[Bar, ...]


class Observation(_Frozen):
    """One value of a time series (FX rate, macro series)."""

    date: date
    value: Decimal


class EarningsEvent(_Frozen):
    date: date  # in the exchange's local time zone
    ts: datetime | None
    timing: EarningsTiming
    eps_estimate: Decimal | None
    eps_actual: Decimal | None
