"""Indicators against hand-computed reference values."""

import math

import numpy as np
import pandas as pd
import pytest

from trading_agent.calc.indicators import atr, bollinger, ema, macd, rsi, sma, true_range

# Wilder's RSI example data (as used in StockCharts' RSI tutorial).
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89,
    46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]  # fmt: skip


def values(series: pd.Series) -> list[float]:
    return [round(float(v), 4) if math.isfinite(v) else math.nan for v in series]


def assert_series(actual: pd.Series, expected: list[float]) -> None:
    np.testing.assert_allclose(actual.to_numpy(dtype=float), expected, atol=1e-4, equal_nan=True)


def test_sma() -> None:
    assert_series(sma(pd.Series([1.0, 2, 3, 4, 5]), 3), [math.nan, math.nan, 2, 3, 4])


def test_ema_is_seeded_with_sma() -> None:
    # alpha = 2/(3+1) = 0.5; seed = mean(2, 4, 6) = 4; then 0.5*8 + 0.5*4 = 6, ...
    assert_series(ema(pd.Series([2.0, 4, 6, 8, 10, 12]), 3), [math.nan, math.nan, 4, 6, 8, 10])


def test_ema_skips_leading_nan_and_carries_gaps() -> None:
    s = pd.Series([math.nan, 2.0, 4, 6, math.nan, 10])
    assert_series(ema(s, 3), [math.nan, math.nan, math.nan, 4, 4, 7])


def test_rsi_wilder_reference() -> None:
    # First 14 changes: gains 3.34, losses 1.40 -> RS = 0.23857/0.1 -> RSI 70.46.
    # Next change -0.28: avg gain 0.22153, avg loss 0.11286 -> RSI 66.25.
    out = rsi(pd.Series(WILDER_CLOSES))
    assert out.iloc[:14].isna().all()
    assert round(float(out.iloc[14]), 2) == 70.46
    assert round(float(out.iloc[15]), 2) == 66.25


def test_rsi_extremes() -> None:
    assert float(rsi(pd.Series(np.arange(1.0, 30.0))).iloc[-1]) == 100.0
    assert float(rsi(pd.Series(np.arange(30.0, 1.0, -1))).iloc[-1]) == 0.0


def test_true_range_and_atr() -> None:
    frame = pd.DataFrame(
        {
            "high": [10.0, 11, 12, 11, 15],
            "low": [8.0, 9, 9, 10, 14],
            "close": [9.0, 10, 11, 10.5, 14.5],
        }
    )
    # TR: 2 (H-L), 2, 3, 1, 4.5 (gap: H - prev close)
    assert_series(true_range(frame), [2, 2, 3, 1, 4.5])
    # ATR(3): seed (2+2+3)/3 = 2.3333; (2.3333*2 + 1)/3 = 1.8889; (1.8889*2 + 4.5)/3 = 2.7593
    assert_series(atr(frame, 3), [math.nan, math.nan, 2.3333, 1.8889, 2.7593])


def test_macd_on_linear_series() -> None:
    # On x_t = t an SMA-seeded EMA(n) is exactly t - (n-1)/2, so MACD = 12.5 - 5.5 = 7.
    out = macd(pd.Series(np.arange(100.0)))
    assert out["macd"].iloc[25:].round(10).eq(7).all()
    assert out["signal"].iloc[33:].round(10).eq(7).all()
    assert out["hist"].iloc[33:].round(10).eq(0).all()
    assert out["signal"].iloc[:33].isna().all()


def test_bollinger_uses_population_std() -> None:
    out = bollinger(pd.Series([1.0, 2, 3, 4, 5]), n=5)
    # mean 3, population std sqrt(2)
    assert out.iloc[-1].round(4).to_dict() == pytest.approx(
        {"lower": 3 - 2 * math.sqrt(2), "mid": 3.0, "upper": 3 + 2 * math.sqrt(2)}, abs=1e-4
    )
