"""The screen: every active stock of a market against the pullback rules at its latest close,
with the first rule it fails, or its rank by relative strength if it passes. Stocks with a
news buy signal are checked against the basic rules only (fresh bars, price, liquidity,
regime, earnings)."""

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

import pandas as pd

from trading_agent.core.db import Sessions
from trading_agent.marketdata import calendars
from trading_agent.marketdata import store as repo
from trading_agent.marketdata.indicators import bars_to_frame
from trading_agent.marketdata.types import Instrument, Market
from trading_agent.news import rules as news
from trading_agent.screen import rules as pullback
from trading_agent.screen.rules import PullbackParams

VIX_SERIES = "VIXCLS"
# About 410 sessions: SMA200, and as long again for the EMAs and RSI to settle.
HISTORY_DAYS = 600
BASIC_RULES = ("price", "liquidity")
INDICATORS = (
    "close",
    "ema20",
    "sma50",
    "sma200",
    "rsi",
    "atr",
    "avg_dollar_volume",
    "sessions_since_high",
    "rs",
)


@dataclass
class InstrumentData:
    id: int
    instrument: Instrument
    frame: pd.DataFrame  # open/high/low/close/volume, date index
    ind: pd.DataFrame  # pullback.indicators
    earnings: list[date]


@dataclass
class MarketData:
    instruments: dict[int, InstrumentData]
    above_sma: dict[Market, pd.Series] = field(default_factory=dict[Market, pd.Series])
    vix: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def regime_block(self, market: Market, day: date, p: PullbackParams) -> str | None:
        """Why the market regime blocks new entries at `day`'s close; unknown data never blocks."""
        ts = pd.Timestamp(day)
        trend = self.above_sma.get(market)
        if p.regime_sma is not None and trend is not None:
            known = trend.loc[:ts]
            if not known.empty and not bool(known.iloc[-1]):
                return "regime: benchmark below its SMA"
        if p.regime_max_vix is not None:
            vix = self.vix.loc[:ts]
            if not vix.empty and float(vix.iloc[-1]) > p.regime_max_vix:
                return "regime: VIX above the limit"
        return None


def prepare(
    frames: Mapping[int, pd.DataFrame],
    instruments: Mapping[int, Instrument],
    earnings: Mapping[int, list[date]],
    benchmarks: Mapping[Market, str],
    p: PullbackParams,
    vix: pd.Series | None = None,
) -> MarketData:
    by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
    bench_frames: dict[Market, pd.DataFrame] = {
        m: frames[by_symbol[s]] for m, s in benchmarks.items() if s in by_symbol
    }
    above_sma: dict[Market, pd.Series] = {}
    if p.regime_sma is not None:
        for m, f in bench_frames.items():
            avg = f["close"].rolling(p.regime_sma).mean()
            above_sma[m] = (f["close"] > avg) | avg.isna()
    data: dict[int, InstrumentData] = {}
    for inst_id, inst in instruments.items():
        frame = frames.get(inst_id)
        if frame is None or frame.empty or not inst.indices:  # benchmarks are not traded
            continue
        bench = bench_frames.get(inst.market)
        bench_close = bench["close"] if bench is not None else frame["close"] * math.nan
        ind = pullback.indicators(frame, bench_close, p)
        data[inst_id] = InstrumentData(inst_id, inst, frame, ind, sorted(earnings.get(inst_id, [])))
    return MarketData(data, above_sma, vix.sort_index() if vix is not None else pd.Series())


async def load_market_data(
    sessions: Sessions, benchmarks: Mapping[Market, str], p: PullbackParams, today: date
) -> MarketData:
    since = today - timedelta(days=HISTORY_DAYS)
    async with sessions() as s:
        instruments = await repo.active_instruments(s)
        bars = await repo.all_bars(s, since)
        earnings = await repo.earnings_dates(s)
        vix = await repo.macro_series(s, VIX_SERIES) if p.regime_max_vix is not None else []
    vix_series = pd.Series(
        [float(o.value) for o in vix],
        index=pd.DatetimeIndex([pd.Timestamp(o.date) for o in vix]),
        dtype=float,
    )
    frames = {k: bars_to_frame(v) for k, v in bars.items()}
    return prepare(frames, instruments, earnings, benchmarks, p, vix_series)


@dataclass(frozen=True)
class ScreenResult:
    instrument_id: int
    symbol: str
    market: Market
    as_of: date | None  # the bar the rules were checked on
    passed: bool
    reason: str  # the first failure; empty when passed
    values: dict[str, Any]
    rs: float
    rank: int | None = None  # among the passed ones, by relative strength (news: confidence)
    source: str = pullback.NAME  # the strategy that picked it: pullback or news

    def stop(self, reason: str) -> "ScreenResult":
        return replace(self, passed=False, reason=reason, rank=None)


def _number(value: object) -> float | None:
    number = float(value)  # pyright: ignore[reportArgumentType]
    return round(number, 4) if math.isfinite(number) else None


def _verdicts(
    data: MarketData,
    p: PullbackParams,
    market: Market | None,
    signals: Mapping[int, float] | None = None,
) -> list[ScreenResult]:
    """All rules for every stock, or only the basic ones for the stocks in `signals`."""
    latest: dict[Market, date] = {}
    for x in data.instruments.values():
        if not x.frame.empty:
            last = pd.Timestamp(x.frame.index[-1]).date()
            latest[x.instrument.market] = max(latest.get(x.instrument.market, last), last)
    out: list[ScreenResult] = []
    source = pullback.NAME if signals is None else news.NAME
    for x in data.instruments.values():
        inst = x.instrument
        if (market is not None and inst.market != market) or (
            signals is not None and x.id not in signals
        ):
            continue
        if x.frame.empty:
            out.append(
                ScreenResult(
                    x.id,
                    inst.yahoo_symbol,
                    inst.market,
                    None,
                    False,
                    "no bars",
                    {},
                    -math.inf,
                    source=source,
                )
            )
            continue
        day = pd.Timestamp(x.frame.index[-1]).date()
        last = x.ind.iloc[-1]
        values = {k: _number(last[k]) for k in INDICATORS}
        rs = values["rs"] if values["rs"] is not None else -math.inf
        if signals is not None:
            values["news_confidence"] = signals[x.id]
            rs = signals[x.id]
        result = ScreenResult(
            x.id, inst.yahoo_symbol, inst.market, day, True, "", values, rs, source=source
        )
        checked = pullback.rules(x.ind.iloc[-1:], p).iloc[-1]
        if signals is not None:
            checked = checked[list(BASIC_RULES)]
        failed = [rule for rule, ok in checked.items() if not ok]
        cal = calendars.CALENDAR_BY_MARKET[inst.market]
        if day != latest[inst.market]:
            result = result.stop(f"stale: last bar {day}, market has {latest[inst.market]}")
        elif failed:
            result = result.stop(pullback.rule_detail(str(failed[0]), last, p))
        elif regime := data.regime_block(inst.market, day, p):
            result = result.stop(regime)
        elif not pullback.earnings_clear(
            day, calendars.session_offset(cal, day, p.earnings_buffer_sessions), x.earnings
        ):
            result = result.stop(f"earnings report within {p.earnings_buffer_sessions} sessions")
        out.append(result)
    return sorted(out, key=lambda r: (not r.passed, -r.rs if r.passed else 0, r.symbol))


def check(data: MarketData, p: PullbackParams, market: Market | None = None) -> list[ScreenResult]:
    """Passed stocks first, best relative strength first; then the others by symbol."""
    return _verdicts(data, p, market)


def news_check(
    data: MarketData, p: PullbackParams, signals: Mapping[int, float], market: Market | None
) -> list[ScreenResult]:
    """The stocks with a news buy signal (instrument id -> the News agent's confidence) on
    the basic rules; passed ones first, most confident first."""
    return _verdicts(data, p, market, signals)


def select(
    results: list[ScreenResult], excluded: dict[str, str], held: Collection[str], top: int
) -> list[ScreenResult]:
    """Drops blocked, blacklisted (`excluded`: symbol -> why) and held stocks, then keeps the
    best `top` of the rest; every stock keeps its verdict."""
    out: list[ScreenResult] = []
    rank = 0
    for r in results:
        if r.passed and r.symbol in excluded:
            r = r.stop(excluded[r.symbol])
        elif r.passed and r.symbol in held:
            r = r.stop("already held")
        elif r.passed:
            rank += 1
            r = replace(r, rank=rank)
            if rank > top:
                by = "relative strength" if r.source == pullback.NAME else "news confidence"
                r = r.stop(f"rank {rank} by {by}, only the top {top} are analysed")
        out.append(r)
    return out


def merge(found: list[ScreenResult], from_news: list[ScreenResult]) -> list[ScreenResult]:
    """One verdict per stock: a news candidate replaces the stock's pullback verdict unless
    the pullback rules picked it already."""
    picked = {r.symbol for r in found if r.passed}
    extra = [r for r in from_news if r.symbol not in picked]
    replaced = {r.symbol for r in extra}
    return [r for r in found if r.symbol not in replaced] + extra
