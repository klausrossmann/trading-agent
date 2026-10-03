"""Baseline strategy "pullback in an uptrend" (IMPLEMENTATION.md 6.2).

Both the candidate generator for the LLM pipeline and the rule-based book it must beat.
All functions only look at data up to the signal bar.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from trading_agent.calc.indicators import atr, ema, rsi, sma
from trading_agent.calc.levels import swing_points
from trading_agent.calc.trend import relative_strength

NAME = "pullback_uptrend"


class PullbackParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000
    avg_volume_sessions: int = 20
    high_lookback: int = 20  # the pullback is measured from the highest close in this window
    pullback_sessions: tuple[int, int] = (3, 10)
    ma_band_pct: tuple[float, float] = (0.0, 3.0)  # close this far above EMA20 or SMA50
    rsi_range: tuple[float, float] = (40.0, 55.0)
    earnings_buffer_sessions: int = 3
    entry_offset_pct: float = 0.2
    entry_valid_sessions: int = 2
    swing_lookback: int = 5
    swing_window: int = 60
    stop_swing_atr: float = 0.5
    stop_atr: float = 2.0
    target_r: float = 2.0
    breakeven_r: float = 1.0
    time_stop_sessions: int = 15
    rs_window: int = 63


@dataclass(frozen=True)
class TradePlan:
    entry: float  # limit price
    stop: float
    target: float

    @property
    def risk_per_share(self) -> float:
        return self.entry - self.stop


def _sessions_since_high(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window, min_periods=window).apply(
        lambda w: len(w) - 1 - int(np.argmax(w)), raw=True
    )


def indicators(frame: pd.DataFrame, benchmark_close: pd.Series, p: PullbackParams) -> pd.DataFrame:
    """Per-bar indicator columns used by the setup and the ranking."""
    close = frame["close"]
    return pd.DataFrame(
        {
            "close": close,
            "ema20": ema(close, 20),
            "sma50": sma(close, 50),
            "sma200": sma(close, 200),
            "rsi": rsi(close),
            "atr": atr(frame),
            "avg_dollar_volume": (close * frame["volume"]).rolling(p.avg_volume_sessions).mean(),
            "sessions_since_high": _sessions_since_high(close, p.high_lookback),
            "rs": relative_strength(close, benchmark_close, p.rs_window),
        },
        index=frame.index,
    )


def setup_mask(ind: pd.DataFrame, p: PullbackParams) -> pd.Series:
    """True on bars where the setup is complete at the close (earnings are checked separately)."""
    close = ind["close"]
    lo, hi = p.ma_band_pct
    above_ema = ((close / ind["ema20"] - 1) * 100).between(lo, hi)
    above_sma = ((close / ind["sma50"] - 1) * 100).between(lo, hi)
    mask = (
        (close > p.min_price)
        & (ind["avg_dollar_volume"] > p.min_avg_dollar_volume)
        & (close > ind["sma200"])
        & (ind["sma50"] > ind["sma200"])
        & ind["sessions_since_high"].between(*p.pullback_sessions)
        & (above_ema | above_sma)
        & ind["rsi"].between(*p.rsi_range)
    )
    return mask.fillna(False).astype(bool)


def earnings_clear(start: date, end: date, earnings: Iterable[date]) -> bool:
    """No announcement between the signal day and the end of the buffer (inclusive)."""
    return not any(start <= d <= end for d in earnings)


def plan_trade(history: pd.DataFrame, atr_now: float, p: PullbackParams) -> TradePlan | None:
    """Entry, stop and target from bars up to and including the signal bar."""
    close = float(history["close"].iloc[-1])
    entry = close * (1 + p.entry_offset_pct / 100)
    if not math.isfinite(atr_now) or atr_now <= 0:
        return None
    window = history.iloc[-p.swing_window :]
    lows = [
        s
        for s in swing_points(window["high"], window["low"], p.swing_lookback)
        if s.kind == "low" and s.price < entry
    ]
    stop = entry - p.stop_atr * atr_now
    if lows:
        stop = min(stop, lows[-1].price - p.stop_swing_atr * atr_now)
    if stop <= 0:
        return None
    return TradePlan(entry=entry, stop=stop, target=entry + p.target_r * (entry - stop))
