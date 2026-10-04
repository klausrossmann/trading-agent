"""Risk engine inputs and decisions (IMPLEMENTATION.md 9). Amounts are Decimal."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

Mode = Literal["paper", "live"]
TradingState = Literal["active", "paused", "halted"]
CheckName = Literal[
    "proposal",
    "global",
    "instrument",
    "levels",
    "sizing",
    "fees",
    "portfolio",
    "loss_limits",
    "rate_limits",
]
CheckOutcome = Literal["pass", "fail", "skip"]  # skip: an input came from a failed check
Trip = Literal["pause_day", "pause_week", "halt"]  # kill-switch change the caller must apply

ZERO = Decimal(0)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Controls(_Frozen):
    """Kill switch and the paper/live interlock (10.4)."""

    trading: TradingState
    app_mode: Mode  # APP_MODE in .env
    gateway_mode: Mode | None  # TRADING_MODE of the connected gateway; None for the simulator
    live_confirmed: bool  # /confirm_live after this start


class KillSwitch(_Frozen):
    """Stored kill-switch state (9.3)."""

    state: TradingState = "active"
    reason: str = ""
    since: datetime | None = None
    until: datetime | None = None  # paused: automatic resume; None means until /resume


class Holding(_Frozen):
    """An open position or a pending entry order of the sleeve."""

    instrument_id: int
    sector: str | None
    value_eur: Decimal  # positions marked to market; pending entries at limit * quantity


class PortfolioState(_Frozen):
    """One budget sleeve (19.1); the sleeve's budget comes with its RiskConfig."""

    holdings: tuple[Holding, ...]
    settled_cash_eur: Decimal  # minus the cash reserved for pending entries
    pnl_today_eur: Decimal  # realized + unrealized since the start of the day
    pnl_week_eur: Decimal
    drawdown_eur: Decimal  # peak equity minus current equity
    orders_today: int  # entry orders submitted today


class MarketSnapshot(_Frozen):
    """Facts about the proposal's instrument at decision time, gathered by the caller."""

    instrument_id: int
    in_universe: bool  # active member of a configured index (excludes OTC, leveraged ETPs)
    session_open: datetime | None  # today's regular session; None on a non-trading day
    session_close: datetime | None
    mid: Decimal  # (bid + ask) / 2, else the last (possibly delayed) price
    atr: Decimal  # ATR(14) at the last close
    avg_daily_value: Decimal  # 20-session average of close * volume, instrument currency
    sessions_to_earnings: int | None  # 0 = today; None if no date is known
    eur_rate: Decimal  # instrument currency per EUR
    correlations: dict[int, float]  # rho of daily returns over 60 sessions, by held instrument


class Check(_Frozen):
    name: CheckName
    outcome: CheckOutcome
    detail: str = ""


class RiskDecision(_Frozen):
    """Quantity and prices are set only when approved; prices in the instrument currency."""

    approved: bool
    checks: tuple[Check, ...]
    trip: Trip | None = None
    quantity: int = 0
    entry: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    risk: Decimal = ZERO  # quantity * (entry - stop)
    value: Decimal = ZERO  # quantity * entry
    fees: Decimal = ZERO  # estimated round trip with the exit at the target

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.outcome == "fail"]
