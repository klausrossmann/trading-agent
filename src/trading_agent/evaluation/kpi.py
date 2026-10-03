"""Book KPIs (IMPLEMENTATION.md 14): trade statistics and equity-curve statistics."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading_agent.domain.trading import Trade

TRADING_DAYS = 252


@dataclass(frozen=True)
class TradeStats:
    trades: int
    win_rate_pct: float
    avg_r: float
    expectancy_eur: float
    profit_factor: float
    total_pnl_eur: float
    fees_eur: float
    avg_holding_sessions: float


@dataclass(frozen=True)
class EquityStats:
    start_value: float
    end_value: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float


def trade_stats(trades: Sequence[Trade]) -> TradeStats:
    closed = [t for t in trades if t.pnl_net_eur is not None]
    if not closed:
        return TradeStats(0, math.nan, math.nan, math.nan, math.nan, 0.0, 0.0, math.nan)
    pnl = np.array([t.pnl_net_eur or 0.0 for t in closed])
    gains, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return TradeStats(
        trades=len(closed),
        win_rate_pct=float((pnl > 0).mean() * 100),
        avg_r=float(np.mean([t.r_multiple or 0.0 for t in closed])),
        expectancy_eur=float(pnl.mean()),
        profit_factor=float(gains / losses) if losses > 0 else math.inf,
        total_pnl_eur=float(pnl.sum()),
        fees_eur=float(sum(t.fees_eur for t in closed)),
        avg_holding_sessions=float(np.mean([t.holding_sessions or 0 for t in closed])),
    )


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return math.nan
    peak = equity.cummax()
    return float(((equity / peak - 1) * 100).min())


def equity_stats(equity: pd.Series) -> EquityStats:
    """`equity`: daily values (date index), first value = starting capital."""
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    returns = equity.pct_change().dropna()
    std = float(returns.std())
    downside = float(returns[returns < 0].std())
    mean = float(returns.mean())
    return EquityStats(
        start_value=start,
        end_value=end,
        total_return_pct=(end / start - 1) * 100,
        cagr_pct=((end / start) ** (1 / years) - 1) * 100,
        max_drawdown_pct=max_drawdown_pct(equity),
        sharpe=mean / std * math.sqrt(TRADING_DAYS) if std > 0 else math.nan,
        sortino=mean / downside * math.sqrt(TRADING_DAYS) if downside > 0 else math.nan,
    )
