from datetime import date

import numpy as np
import pandas as pd
import pytest

from trading_agent.strategies.pullback import (
    PullbackParams,
    earnings_clear,
    plan_trade,
    setup_mask,
)

P = PullbackParams()


def _ind(**overrides: float) -> pd.DataFrame:
    """One bar that passes every rule; override a column to break one rule."""
    row = {
        "close": 100.0,
        "ema20": 99.0,  # 1 % above
        "sma50": 95.0,
        "sma200": 80.0,
        "rsi": 48.0,
        "atr": 2.0,
        "avg_dollar_volume": 50e6,
        "sessions_since_high": 5.0,
        "rs": 0.1,
    } | overrides
    return pd.DataFrame([row])


def test_setup_passes_when_all_rules_hold() -> None:
    assert bool(setup_mask(_ind(), P).iloc[0])


@pytest.mark.parametrize(
    "overrides",
    [
        {"close": 4.0, "ema20": 3.9, "sma50": 3.8, "sma200": 3.0},  # price below 5
        {"avg_dollar_volume": 10e6},  # illiquid
        {"sma200": 101.0},  # below the 200-day average
        {"sma50": 79.0, "ema20": 105.0},  # SMA50 below SMA200
        {"sessions_since_high": 2.0},  # pullback too short
        {"sessions_since_high": 11.0},  # too long
        {"ema20": 96.0, "sma50": 96.0},  # 4 % above both averages
        {"ema20": 101.0, "sma50": 101.0},  # below both averages
        {"rsi": 39.0},
        {"rsi": 56.0},
        {"rsi": float("nan")},  # warm-up
    ],
)
def test_setup_fails_when_a_rule_breaks(overrides: dict[str, float]) -> None:
    assert not bool(setup_mask(_ind(**overrides), P).iloc[0])


def test_close_near_sma50_is_enough() -> None:
    assert bool(setup_mask(_ind(ema20=90.0, sma50=98.0), P).iloc[0])


def test_earnings_buffer_is_inclusive() -> None:
    d = date(2026, 10, 1)
    end = date(2026, 10, 6)
    assert earnings_clear(d, end, [date(2026, 9, 30), date(2026, 10, 7)])
    assert not earnings_clear(d, end, [d])
    assert not earnings_clear(d, end, [end])


def _history(lows: list[float]) -> pd.DataFrame:
    lows_arr = np.array(lows)
    return pd.DataFrame(
        {"open": lows_arr + 1, "high": lows_arr + 2, "low": lows_arr, "close": lows_arr + 1},
        index=pd.bdate_range("2026-01-05", periods=len(lows)),
    )


def test_plan_uses_lower_of_swing_low_and_atr_stop() -> None:
    # close 101 -> entry 101.202; swing low 98 (confirmed), so stop = min(98 - 1, 101.202 - 4)
    lows = [100.0] * 5 + [98.0] + [100.0] * 9
    plan = plan_trade(_history(lows), atr_now=2.0, p=P)
    assert plan is not None
    assert plan.entry == pytest.approx(101.202)
    assert plan.stop == pytest.approx(97.0)
    assert plan.target == pytest.approx(101.202 + 2 * (101.202 - 97.0))


def test_plan_without_swing_low_uses_atr_stop() -> None:
    plan = plan_trade(_history([100.0] * 15), atr_now=2.0, p=P)
    assert plan is not None
    assert plan.stop == pytest.approx(101.202 - 4)


def test_plan_rejects_missing_atr_and_negative_stop() -> None:
    assert plan_trade(_history([100.0] * 15), atr_now=float("nan"), p=P) is None
    assert plan_trade(_history([1.0] * 15), atr_now=5.0, p=P) is None
