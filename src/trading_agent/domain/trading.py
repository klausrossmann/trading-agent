"""Round-trip trades of a book (simulated or real)."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trading_agent.domain.market import Market
from trading_agent.domain.risk import Mode

Book = Literal["baseline_sim", "agent_paper", "agent_shadow", "agent_live", "backtest"]


def book_for_mode(mode: Mode) -> Book:
    """The book that holds the agent's own orders."""
    return "agent_paper" if mode == "paper" else "agent_live"


class Trade(BaseModel):
    """Prices and fees in the instrument currency; P&L and risk in EUR. Open: no exit_date."""

    model_config = ConfigDict(frozen=True)

    book: Book
    strategy: str
    instrument_id: int
    yahoo_symbol: str
    market: Market
    sector: str | None
    signal_date: date
    entry_date: date
    entry_price: float
    quantity: float
    stop: float  # initial stop
    target: float
    risk_eur: float  # quantity * (planned entry - stop), at the entry FX rate
    fees: float  # both sides so far
    fees_eur: float
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_net_eur: float | None = None
    r_multiple: float | None = None
    holding_sessions: int | None = None
