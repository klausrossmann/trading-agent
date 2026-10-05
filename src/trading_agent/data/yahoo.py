"""Yahoo Finance via yfinance (unofficial; Phase 0-1 only, replaced by IBKR bars in M6)."""

import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from trading_agent.domain.market import (
    Bar,
    BarSeries,
    EarningsEvent,
    EarningsTiming,
    Instrument,
    Market,
)
from trading_agent.domain.numbers import to_decimal

SOURCE = "yahoo"

# Regular trading hours, used to classify announcement times.
_HOURS: dict[Market, tuple[ZoneInfo, time, time]] = {
    "US": (ZoneInfo("America/New_York"), time(9, 30), time(16, 0)),
    "EU": (ZoneInfo("Europe/Berlin"), time(9, 0), time(17, 30)),
}


def _dec(value: Any, places: int = 4) -> Decimal | None:
    if value is None or pd.isna(value) or math.isinf(float(value)):
        return None
    return to_decimal(float(value), places)


def bars_from_history(frame: pd.DataFrame) -> tuple[Bar, ...]:
    """Convert `Ticker.history(auto_adjust=False)` output; index dates are exchange-local."""
    bars: dict[date, Bar] = {}
    for ts, row in frame.iterrows():
        prices = [_dec(row[c]) for c in ("Open", "High", "Low", "Close")]
        if any(p is None for p in prices) or pd.isna(row["Volume"]):
            continue
        day = pd.Timestamp(ts).date()  # pyright: ignore[reportArgumentType]
        o, h, lo, c = (p for p in prices if p is not None)
        bars.setdefault(
            day, Bar(date=day, open=o, high=h, low=lo, close=c, volume=int(row["Volume"]))
        )
    return tuple(bars[d] for d in sorted(bars))


def _timing(local: datetime, opens: time, closes: time) -> EarningsTiming:
    t = local.time()
    if t == time(0):  # Yahoo uses midnight when the time is unknown
        return "unknown"
    if t < opens:
        return "bmo"
    if t >= closes:
        return "amc"
    return "during"


def earnings_from_frame(frame: pd.DataFrame, market: Market) -> tuple[EarningsEvent, ...]:
    """Convert `Ticker.get_earnings_dates()` output (tz-aware index, newest first)."""
    tz, opens, closes = _HOURS[market]
    events: dict[date, EarningsEvent] = {}
    for ts, row in frame.iterrows():
        local = pd.Timestamp(ts).tz_convert(tz).to_pydatetime()  # pyright: ignore[reportArgumentType]
        events.setdefault(
            local.date(),
            EarningsEvent(
                date=local.date(),
                ts=local.astimezone(UTC),
                timing=_timing(local, opens, closes),
                eps_estimate=_dec(row.get("EPS Estimate")),
                eps_actual=_dec(row.get("Reported EPS")),
            ),
        )
    return tuple(sorted(events.values(), key=lambda e: e.date))


class YahooProvider:
    source = SOURCE

    def daily_bars(self, instrument: Instrument, start: date, end: date) -> BarSeries:
        frame = yf.Ticker(instrument.yahoo_symbol).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # end is exclusive
            auto_adjust=False,
            actions=False,
            raise_errors=True,
        )
        return BarSeries(
            yahoo_symbol=instrument.yahoo_symbol,
            source=SOURCE,
            fetched_at=datetime.now(UTC),
            bars=bars_from_history(frame),
        )

    def earnings(self, instrument: Instrument, limit: int) -> tuple[EarningsEvent, ...]:
        frame = yf.Ticker(instrument.yahoo_symbol).get_earnings_dates(limit=limit)
        if frame is None or frame.empty:
            return ()
        events = earnings_from_frame(frame, instrument.market)
        return events[-limit:]
