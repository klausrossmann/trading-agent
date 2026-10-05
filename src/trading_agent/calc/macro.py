"""Market regime and macro snapshot for the proposer, cut at the scan date. Pure."""

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

import pandas as pd
from pydantic import BaseModel, ConfigDict

from trading_agent.calc.indicators import sma
from trading_agent.calc.trend import Trend, trend_states
from trading_agent.domain.market import Observation

# field -> FRED series id (config/data.yaml macro.fred)
FRED_SERIES = {
    "vix": "VIXCLS",
    "us_10y_yield_pct": "DGS10",
    "us_10y_2y_spread_pct": "T10Y2Y",
    "us_high_yield_spread_pct": "BAMLH0A0HYM2",
}
LOOKBACK = 20  # sessions or observations for the "20 ago" values
MAX_AGE_DAYS = 10  # older observations count as unavailable


class MacroSnapshot(BaseModel):
    """Null fields are unavailable (no FRED key, no benchmark bars, stale series)."""

    model_config = ConfigDict(frozen=True)

    benchmark: str | None
    benchmark_trend: dict[str, Trend]
    benchmark_vs_sma200_pct: float | None
    benchmark_return_20d_pct: float | None
    vix: float | None
    vix_20_ago: float | None
    us_10y_yield_pct: float | None
    us_10y_2y_spread_pct: float | None
    us_high_yield_spread_pct: float | None
    us_high_yield_spread_20_ago: float | None


def _round(value: float) -> float | None:
    return round(value, 2) if math.isfinite(value) else None


def _recent(observations: Sequence[Observation], as_of: date) -> list[float]:
    known = [o for o in observations if o.date <= as_of]
    if not known or known[-1].date < as_of - timedelta(days=MAX_AGE_DAYS):
        return []
    return [float(o.value) for o in known]


def _ago(values: list[float]) -> float | None:
    return round(values[-1 - LOOKBACK], 2) if len(values) > LOOKBACK else None


def macro_snapshot(
    benchmark: str | None,
    benchmark_close: pd.Series | None,
    series: Mapping[str, Sequence[Observation]],
    as_of: date,
) -> MacroSnapshot:
    """`benchmark_close`: daily closes with a date index; `series`: FRED id -> observations."""
    close = (
        benchmark_close.loc[: pd.Timestamp(as_of)]
        if benchmark_close is not None
        else pd.Series(dtype=float)
    )
    vs_sma = ret = None
    if not close.empty:
        vs_sma = _round((float(close.iloc[-1]) / float(sma(close, 200).iloc[-1]) - 1) * 100)
        if len(close) > LOOKBACK:
            ret = _round((float(close.iloc[-1]) / float(close.iloc[-1 - LOOKBACK]) - 1) * 100)
    values = {f: _recent(series.get(sid, ()), as_of) for f, sid in FRED_SERIES.items()}

    def last(f: str) -> float | None:
        return round(values[f][-1], 2) if values[f] else None

    return MacroSnapshot(
        benchmark=benchmark,
        benchmark_trend=trend_states(close) if not close.empty else {},
        benchmark_vs_sma200_pct=vs_sma,
        benchmark_return_20d_pct=ret,
        vix=last("vix"),
        vix_20_ago=_ago(values["vix"]),
        us_10y_yield_pct=last("us_10y_yield_pct"),
        us_10y_2y_spread_pct=last("us_10y_2y_spread_pct"),
        us_high_yield_spread_pct=last("us_high_yield_spread_pct"),
        us_high_yield_spread_20_ago=_ago(values["us_high_yield_spread_pct"]),
    )
