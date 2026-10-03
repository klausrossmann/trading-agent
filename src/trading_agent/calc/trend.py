"""Trend state, relative strength and post-earnings reactions."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from trading_agent.calc.indicators import sma
from trading_agent.domain.market import EarningsEvent, EarningsTiming

Trend = Literal["up", "down", "sideways", "unknown"]


def trend_state(close: float, fast: float, slow: float) -> Trend:
    """Up: price and fast MA above the slow MA; down: both below; otherwise sideways."""
    if any(np.isnan(v) for v in (close, fast, slow)):
        return "unknown"
    if close > slow and fast > slow:
        return "up"
    if close < slow and fast < slow:
        return "down"
    return "sideways"


def trend_states(close: pd.Series) -> dict[str, Trend]:
    """Daily (SMA 50/200) and weekly (SMA 10/40 weeks, the same spans) trend at the last bar."""
    weekly = close.resample("W-FRI").last().dropna()
    states: dict[str, Trend] = {}
    for name, series, fast, slow in (("daily", close, 50, 200), ("weekly", weekly, 10, 40)):
        if series.empty:
            states[name] = "unknown"
            continue
        states[name] = trend_state(
            float(series.iloc[-1]),
            float(sma(series, fast).iloc[-1]),
            float(sma(series, slow).iloc[-1]),
        )
    return states


def relative_strength(close: pd.Series, benchmark: pd.Series, window: int = 63) -> pd.Series:
    """Return over `window` bars relative to the benchmark: 0.05 = outperformed by 5 %."""
    bench = benchmark.reindex(close.index).ffill()
    return (close / close.shift(window)) / (bench / bench.shift(window)) - 1


@dataclass(frozen=True)
class EarningsMove:
    event_date: date
    timing: EarningsTiming
    reaction_date: date
    gap_pct: float  # reaction-day open vs the close before the announcement
    move_pct: float  # reaction-day close vs the close before the announcement


def post_earnings_moves(frame: pd.DataFrame, events: Sequence[EarningsEvent]) -> list[EarningsMove]:
    """Price reaction to each past announcement.

    Before open / during the session: the event day reacts. After close: the next session.
    Unknown time: a two-day window from the session before to the session after the event day.
    """
    index = frame.index
    moves: list[EarningsMove] = []
    for event in events:
        day = pd.Timestamp(event.date)
        at_or_after = int(index.searchsorted(day, side="left"))
        after = int(index.searchsorted(day, side="right"))
        if event.timing in ("bmo", "during"):
            before_i, reaction_i = at_or_after - 1, at_or_after
        elif event.timing == "amc":
            before_i, reaction_i = after - 1, after
        else:
            before_i, reaction_i = at_or_after - 1, after
        if before_i < 0 or reaction_i >= len(index):
            continue
        prev_close = float(frame["close"].iloc[before_i])
        moves.append(
            EarningsMove(
                event_date=event.date,
                timing=event.timing,
                reaction_date=pd.Timestamp(index[reaction_i]).date(),
                gap_pct=(float(frame["open"].iloc[reaction_i]) / prev_close - 1) * 100,
                move_pct=(float(frame["close"].iloc[reaction_i]) / prev_close - 1) * 100,
            )
        )
    return moves


def average_abs_move(moves: Sequence[EarningsMove]) -> float | None:
    return float(np.mean([abs(m.move_pct) for m in moves])) if moves else None
