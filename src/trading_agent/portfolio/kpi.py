"""Trade statistics of the agent's book (IMPLEMENTATION.md 14)."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from trading_agent.portfolio.types import Trade


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
