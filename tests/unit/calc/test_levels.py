from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from trading_agent.calc.levels import SwingPoint, fib_levels, level_menu, swing_points, zones


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range("2026-01-05", periods=len(values)))


def test_swing_points_need_lookback_on_both_sides() -> None:
    high = _series([1, 2, 3, 2, 1, 2, 5, 2, 1, 6])
    low = _series([1, 1, 2, 1, 0.5, 1, 2, 1, 1, 2])
    points = swing_points(high, low, lookback=2)
    assert [(p.kind, p.price) for p in points] == [("high", 3), ("low", 0.5), ("high", 5)]
    # index 9 (6.0) is not confirmed: no bars to its right


def test_equal_highs_count_once() -> None:
    high = _series([1, 2, 3, 3, 2, 1, 1])
    low = _series([1.0] * 7)
    assert [p.price for p in swing_points(high, low, lookback=2) if p.kind == "high"] == [3]


def test_zones_cluster_within_tolerance() -> None:
    d = date(2026, 1, 5)
    points = [
        SwingPoint(d, 10.0, "low"),
        SwingPoint(date(2026, 2, 2), 10.4, "high"),
        SwingPoint(d, 10.2, "low"),
        SwingPoint(d, 12.0, "high"),
    ]
    result = zones(points, tolerance=0.5)
    assert [(round(z.price, 4), z.touches) for z in result] == [(10.2, 3), (12.0, 1)]
    assert result[0].last_touch == date(2026, 2, 2)


def test_fib_levels() -> None:
    assert fib_levels(100, 200, up_leg=True) == pytest.approx(
        {"fib_382": 161.8, "fib_500": 150.0, "fib_618": 138.2}
    )
    assert fib_levels(100, 200, up_leg=False) == pytest.approx(
        {"fib_382": 138.2, "fib_500": 150.0, "fib_618": 161.8}
    )


def _frame(n: int, seed: int = 1) -> pd.DataFrame:
    """Uptrend with a 20-bar oscillation, so there are swings, supports and resistances."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 50 + 0.1 * t + 3 * np.sin(t / 20 * 2 * np.pi) + rng.normal(0, 0.3, n)
    spread = 0.5 + rng.random(n)
    return pd.DataFrame(
        {"open": close, "high": close + spread, "low": close - spread, "close": close},
        index=pd.bdate_range("2025-01-01", periods=n),
    )


def test_level_menu_on_long_history() -> None:
    menu = {lvl.name: lvl for lvl in level_menu(_frame(300))}
    close = menu["close"].price
    for name in ("ema20", "sma50", "sma200", "bb_lower", "bb_upper", "high_52w", "low_52w"):
        assert name in menu
    assert {"fib_382", "fib_500", "fib_618", "swing_low_last"} <= menu.keys()
    assert all(lvl.price > 0 for lvl in menu.values())
    assert all(lvl.price == lvl.price.quantize(Decimal("0.01")) for lvl in menu.values())

    assert (
        menu["atr_stop_3x"].price < menu["atr_stop_2x"].price < menu["atr_stop_1_5x"].price < close
    )
    targets = [menu[n].price for n in ("atr_target_3x", "atr_target_4x", "atr_target_6x")]
    assert close < targets[0] < targets[1] < targets[2]
    # Symmetric around the close: 3 x ATR up mirrors 3 x ATR down (cent rounding aside).
    assert abs((targets[0] - close) - (close - menu["atr_stop_3x"].price)) <= Decimal("0.01")
    supports = [menu[n] for n in ("support_1", "support_2", "support_3") if n in menu]
    resistances = [menu[n] for n in ("resistance_1", "resistance_2") if n in menu]
    assert supports
    assert all(s.price < close and (s.touches or 0) >= 1 for s in supports)
    assert [s.price for s in supports] == sorted((s.price for s in supports), reverse=True)
    assert all(r.price > close for r in resistances)
    assert menu["swing_low_last"].price < close


def test_level_menu_short_history_leaves_out_what_it_cannot_compute() -> None:
    names = {lvl.name for lvl in level_menu(_frame(30))}
    assert {"close", "ema20", "bb_lower"} <= names
    assert not names & {"sma50", "sma200"}
    assert level_menu(_frame(0)) == []
