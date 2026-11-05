"""Strategy "pullback in an uptrend" (IMPLEMENTATION.md 6.2): the screen's rules and the
parameters of trade management (entry validity, breakeven, time stop, trailing stop). All
functions only look at data up to the signal bar.
"""

from collections.abc import Iterable
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from trading_agent.calc.indicators import atr, ema, rsi, sma
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
    entry_valid_sessions: int = 2
    breakeven_r: float = 1.0
    time_stop_sessions: int = 15
    rs_window: int = 63
    # Optional, off (None). After breakeven the stop trails this many ATR under the
    # highest close since the entry.
    trail_atr: float | None = Field(default=None, gt=0)
    # Market regime gate: no new entries while the market's benchmark closes below its SMA of
    # this length, or while VIX (FRED VIXCLS) closes above `regime_max_vix`.
    regime_sma: int | None = Field(default=None, ge=2)
    regime_max_vix: float | None = Field(default=None, gt=0)


def load_params(config_dir: Path) -> PullbackParams:
    raw = yaml.safe_load((config_dir / "strategies.yaml").read_text(encoding="utf-8"))
    return PullbackParams.model_validate(raw["pullback"])


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


def rules(ind: pd.DataFrame, p: PullbackParams) -> pd.DataFrame:
    """One boolean column per setup rule, in the order the screen reports the first failure."""
    close = ind["close"]
    lo, hi = p.ma_band_pct
    above_ema = ((close / ind["ema20"] - 1) * 100).between(lo, hi)
    above_sma = ((close / ind["sma50"] - 1) * 100).between(lo, hi)
    return pd.DataFrame(
        {
            "price": close > p.min_price,
            "liquidity": ind["avg_dollar_volume"] > p.min_avg_dollar_volume,
            "above_sma200": close > ind["sma200"],
            "sma50_above_sma200": ind["sma50"] > ind["sma200"],
            "pullback": ind["sessions_since_high"].between(*p.pullback_sessions),
            "near_average": above_ema | above_sma,
            "rsi": ind["rsi"].between(*p.rsi_range),
        },
        index=ind.index,
    ).fillna(False)


def setup_mask(ind: pd.DataFrame, p: PullbackParams) -> pd.Series:
    """True on bars where the setup is complete at the close (earnings are checked separately)."""
    return rules(ind, p).all(axis=1).astype(bool)


def rule_detail(rule: str, last: pd.Series, p: PullbackParams) -> str:
    """Why `rule` failed on the bar `last` (a row of `indicators`), in plain words."""
    close = float(last["close"])
    if rule == "price":
        return f"close {close:.2f} not above {p.min_price:g}"
    if rule == "liquidity":
        value = float(last["avg_dollar_volume"]) / 1e6
        return f"avg traded value {value:.1f}M below {p.min_avg_dollar_volume / 1e6:g}M"
    if rule == "above_sma200":
        return f"close {close:.2f} not above SMA200 {float(last['sma200']):.2f}"
    if rule == "sma50_above_sma200":
        return f"SMA50 {float(last['sma50']):.2f} not above SMA200 {float(last['sma200']):.2f}"
    if rule == "pullback":
        lo, hi = p.pullback_sessions
        return f"highest close {last['sessions_since_high']:.0f} sessions ago, needs {lo}-{hi}"
    if rule == "near_average":
        ema_pct = (close / float(last["ema20"]) - 1) * 100
        sma_pct = (close / float(last["sma50"]) - 1) * 100
        lo, hi = p.ma_band_pct
        return f"close {ema_pct:+.1f}% vs EMA20, {sma_pct:+.1f}% vs SMA50, needs {lo:g} to {hi:g}%"
    if rule == "rsi":
        lo, hi = p.rsi_range
        return f"RSI {float(last['rsi']):.1f}, needs {lo:g}-{hi:g}"
    return rule


def earnings_clear(start: date, end: date, earnings: Iterable[date]) -> bool:
    """No announcement between the signal day and the end of the buffer (inclusive)."""
    return not any(start <= d <= end for d in earnings)
