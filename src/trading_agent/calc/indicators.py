"""Technical indicators over pandas Series, with Wilder smoothing and SMA-seeded EMAs."""

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from trading_agent.domain.market import Bar

FloatArray = NDArray[np.float64]


def bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    """Bars -> float DataFrame with columns open/high/low/close/volume and a DatetimeIndex."""
    frame = pd.DataFrame(
        {
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        },
        index=pd.DatetimeIndex([pd.Timestamp(b.date) for b in bars], name="date"),
    )
    return frame


def _values(series: pd.Series) -> FloatArray:
    return series.to_numpy(dtype=np.float64, na_value=np.nan)


def _seeded_ema(values: FloatArray, alpha: float, n: int) -> FloatArray:
    """EMA seeded with the SMA of the first n valid values; gaps (NaN) carry the last value."""
    out = np.full(len(values), np.nan)
    valid = ~np.isnan(values)
    start = next((i for i in range(len(values) - n + 1) if valid[i : i + n].all()), None)
    if start is None:
        return out
    seed = start + n - 1
    out[seed] = values[start : seed + 1].mean()
    for i in range(seed + 1, len(values)):
        v = values[i]
        out[i] = out[i - 1] if np.isnan(v) else alpha * v + (1 - alpha) * out[i - 1]
    return out


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return pd.Series(_seeded_ema(_values(series), 2 / (n + 1), n), index=series.index)


def wilder(series: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (an EMA with alpha = 1/n), seeded with the first n-value average."""
    return pd.Series(_seeded_ema(_values(series), 1 / n, n), index=series.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    avg_gain = wilder(delta.clip(lower=0), n)
    avg_loss = wilder(-delta.clip(upper=0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = _values(avg_gain) / _values(avg_loss)
        out = np.where(_values(avg_loss) == 0, 100.0, 100 - 100 / (1 + rs))
    out[np.isnan(_values(avg_gain))] = np.nan
    return pd.Series(out, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    signal_line = ema(line, signal)
    return pd.DataFrame(
        {"macd": line, "signal": signal_line, "hist": line - signal_line}, index=close.index
    )


def true_range(frame: pd.DataFrame) -> pd.Series:
    prev_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1, skipna=True)  # first bar: high - low


def atr(frame: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(frame), n)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)  # Bollinger uses the population std
    return pd.DataFrame({"lower": mid - k * std, "mid": mid, "upper": mid + k * std})
