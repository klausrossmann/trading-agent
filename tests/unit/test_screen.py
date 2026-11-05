"""The screen: every stock's verdict at its latest close, and the selection for the agents."""

import math
from datetime import date

import pandas as pd

from trading_agent import screen
from trading_agent.domain.market import Instrument
from trading_agent.strategies.pullback import PullbackParams

P = PullbackParams()
DAYS = pd.bdate_range("2026-10-01", periods=3)  # Thu 1, Fri 2, Mon 5 October
PASSING = {
    "close": 100.0,
    "ema20": 99.0,
    "sma50": 95.0,
    "sma200": 80.0,
    "rsi": 48.0,
    "atr": 2.0,
    "avg_dollar_volume": 50e6,
    "sessions_since_high": 5.0,
    "rs": 0.1,
}


def stock(
    n: int, days: pd.DatetimeIndex = DAYS, earnings: tuple[date, ...] = (), **overrides: float
) -> screen.InstrumentData:
    inst = Instrument(
        id=n,
        symbol=f"S{n}",
        yahoo_symbol=f"S{n}",
        name=f"S{n}",
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
        indices=("SP100",),
    )
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6}, index=days
    )
    ind = pd.DataFrame([PASSING | overrides] * len(days), index=days)
    return screen.InstrumentData(n, inst, frame, ind, list(earnings))


def test_every_stock_gets_a_verdict() -> None:
    xs = [
        stock(1),
        stock(2, rs=0.3),
        stock(3, rsi=61.0),
        stock(4, days=DAYS[:-1]),
        stock(5, earnings=(date(2026, 10, 6),)),
        stock(6, sma200=float("nan")),
    ]
    data = screen.MarketData({x.id: x for x in xs})
    found = screen.check(data, P, "US")
    assert [(r.symbol, r.passed, r.reason) for r in found] == [
        ("S2", True, ""),
        ("S1", True, ""),
        ("S3", False, "RSI 61.0, needs 40-55"),
        ("S4", False, "stale: last bar 2026-10-02, market has 2026-10-05"),
        ("S5", False, "earnings report within 3 sessions"),
        ("S6", False, "close 100.00 not above SMA200 nan"),
    ]
    assert found[0].as_of == date(2026, 10, 5)
    assert found[0].values["rsi"] == 48.0
    assert found[-1].values["sma200"] is None
    assert screen.check(data, P, "EU") == []


def test_select_drops_excluded_and_held_then_keeps_the_top() -> None:
    def passed(symbol: str, rs: float) -> screen.ScreenResult:
        return screen.ScreenResult(0, symbol, "US", date(2026, 10, 5), True, "", {}, rs)

    failed = screen.ScreenResult(0, "EEE", "US", None, False, "no bars", {}, -math.inf)
    results = [
        passed("CCC", 5),
        passed("BBB", 4),
        passed("AAA", 3),
        passed("DDD", 2),
        passed("FFF", 1),
        failed,
    ]
    chosen = screen.select(results, {"CCC": "blacklisted in risk.yaml"}, {"BBB"}, top=1)
    assert [(r.symbol, r.passed, r.rank, r.reason) for r in chosen] == [
        ("CCC", False, None, "blacklisted in risk.yaml"),
        ("BBB", False, None, "already held"),
        ("AAA", True, 1, ""),
        ("DDD", False, None, "rank 2 by relative strength, only the top 1 are analysed"),
        ("FFF", False, None, "rank 3 by relative strength, only the top 1 are analysed"),
        ("EEE", False, None, "no bars"),
    ]


def test_news_candidates_need_only_the_basic_rules() -> None:
    xs = [
        stock(1, rsi=61.0),  # fails the pullback rules, passes the basic ones
        stock(2, rsi=61.0, avg_dollar_volume=5e6),
        stock(3),
        stock(4, earnings=(date(2026, 10, 6),)),
        stock(5, days=DAYS[:0]),
    ]
    data = screen.MarketData({x.id: x for x in xs})
    signals = {1: 0.7, 2: 0.9, 4: 0.8, 5: 0.6}
    news = screen.news_check(data, P, signals, "US")
    assert [(r.symbol, r.passed, r.source, r.reason) for r in news] == [
        ("S1", True, "news", ""),
        ("S2", False, "news", "avg traded value 5.0M below 20M"),
        ("S4", False, "news", "earnings report within 3 sessions"),
        ("S5", False, "news", "no bars"),
    ]
    assert news[0].values["news_confidence"] == 0.7
    pulled = screen.select(screen.check(data, P, "US"), {}, (), top=5)
    merged = screen.merge(pulled, screen.select(news, {}, (), top=1))
    verdicts = {r.symbol: (r.passed, r.source) for r in merged}
    assert verdicts == {
        "S1": (True, "news"),
        "S2": (False, "news"),
        "S3": (True, "pullback_uptrend"),
        "S4": (False, "news"),
        "S5": (False, "news"),
    }
    late = screen.select([news[0], news[0]], {}, (), top=1)[1]
    assert late.reason == "rank 2 by news confidence, only the top 1 are analysed"
