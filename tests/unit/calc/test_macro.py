from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from trading_agent.calc.macro import macro_snapshot
from trading_agent.domain.market import Observation

AS_OF = date(2026, 10, 5)


def obs(values: list[float], end: date = AS_OF) -> list[Observation]:
    days = pd.bdate_range(end=end, periods=len(values))
    return [
        Observation(date=d.date(), value=Decimal(str(v))) for d, v in zip(days, values, strict=True)
    ]


def test_snapshot_cut_at_as_of() -> None:
    days = pd.bdate_range(end=AS_OF + timedelta(days=7), periods=260)
    close = pd.Series([100.0 + i for i in range(260)], index=days)
    vix = obs([20.0] * 5 + [15.0] * 20 + [99.0], end=AS_OF + timedelta(days=1))  # last one: future
    snap = macro_snapshot("SPY", close, {"VIXCLS": vix, "DGS10": obs([4.1234])}, AS_OF)
    last = float(close.loc[: pd.Timestamp(AS_OF)].iloc[-1])
    assert snap.benchmark == "SPY"
    assert snap.benchmark_trend == {"daily": "up", "weekly": "up"}
    assert snap.benchmark_return_20d_pct == round((last / (last - 20) - 1) * 100, 2)
    assert snap.benchmark_vs_sma200_pct is not None
    assert snap.benchmark_vs_sma200_pct > 0
    assert (snap.vix, snap.vix_20_ago) == (15.0, 20.0)
    assert snap.us_10y_yield_pct == 4.12
    assert snap.us_high_yield_spread_pct is None


def test_missing_and_stale_data_are_null() -> None:
    stale = obs([3.0] * 30, end=AS_OF - timedelta(days=20))
    snap = macro_snapshot(None, None, {"BAMLH0A0HYM2": stale}, AS_OF)
    assert snap.benchmark_trend == {}
    assert snap.benchmark_vs_sma200_pct is None
    assert snap.us_high_yield_spread_pct is None
    short = pd.Series([100.0] * 10, index=pd.bdate_range(end=AS_OF, periods=10))
    snap = macro_snapshot("SPY", short, {}, AS_OF)
    assert (snap.benchmark_vs_sma200_pct, snap.benchmark_return_20d_pct) == (None, None)
