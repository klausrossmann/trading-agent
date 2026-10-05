from datetime import date

import numpy as np
import pandas as pd
import pytest

from trading_agent.calc.trend import (
    post_earnings_moves,
    relative_strength,
    trend_state,
    trend_states,
)
from trading_agent.domain.market import EarningsEvent, EarningsTiming


@pytest.mark.parametrize(
    ("close", "fast", "slow", "expected"),
    [
        (110, 105, 100, "up"),
        (90, 95, 100, "down"),
        (105, 95, 100, "sideways"),  # price above, but the fast MA is still below
        (95, 105, 100, "sideways"),
        (100, float("nan"), 100, "unknown"),
    ],
)
def test_trend_state(close: float, fast: float, slow: float, expected: str) -> None:
    assert trend_state(close, fast, slow) == expected


def _business_days(values: np.ndarray, start: str = "2024-01-01") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)))


def test_trend_states_daily_and_weekly() -> None:
    assert trend_states(_business_days(np.linspace(50, 150, 400))) == {
        "daily": "up",
        "weekly": "up",
    }
    assert trend_states(_business_days(np.linspace(150, 50, 400))) == {
        "daily": "down",
        "weekly": "down",
    }
    assert trend_states(_business_days(np.linspace(50, 60, 30))) == {
        "daily": "unknown",
        "weekly": "unknown",
    }


def test_relative_strength() -> None:
    stock = _business_days(np.array([10.0, 12, 20]))
    bench = _business_days(np.array([100.0, 100, 125]))
    rs = relative_strength(stock, bench, window=2)
    assert float(rs.iloc[-1]) == pytest.approx(2.0 / 1.25 - 1)  # +100 % vs +25 % -> 0.6


def _week() -> pd.DataFrame:
    days = pd.bdate_range("2026-09-28", "2026-10-02")  # Mon..Fri
    closes = np.array([100.0, 101, 102, 103, 104])
    return pd.DataFrame({"open": closes - 0.5, "close": closes}, index=days)


def _event(day: date, timing: EarningsTiming) -> EarningsEvent:
    return EarningsEvent(date=day, ts=None, timing=timing, eps_estimate=None, eps_actual=None)


def test_post_earnings_reaction_windows() -> None:
    wed = date(2026, 9, 30)
    moves = post_earnings_moves(
        _week(),
        [
            _event(wed, "bmo"),  # Tue close 101 -> Wed close 102
            _event(wed, "amc"),  # Wed close 102 -> Thu close 103
            _event(wed, "unknown"),  # Tue close 101 -> Thu close 103
            _event(date(2026, 10, 2), "amc"),  # reaction day not in data yet
            _event(date(2026, 9, 28), "bmo"),  # no close before
        ],
    )
    assert [(m.timing, m.reaction_date, round(m.move_pct, 3)) for m in moves] == [
        ("bmo", date(2026, 9, 30), round((102 / 101 - 1) * 100, 3)),
        ("amc", date(2026, 10, 1), round((103 / 102 - 1) * 100, 3)),
        ("unknown", date(2026, 10, 1), round((103 / 101 - 1) * 100, 3)),
    ]
    assert moves[0].gap_pct == pytest.approx((101.5 / 101 - 1) * 100)
