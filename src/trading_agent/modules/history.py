"""Data a module run needs for one instrument, cut off at `as_of` (no look-ahead)."""

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_agent.calc.indicators import bars_to_frame
from trading_agent.db import market as repo
from trading_agent.domain.market import EarningsEvent, Instrument

HISTORY_DAYS = 730  # covers the 52-week range, SMA200 and the 40-week SMA


@dataclass(frozen=True)
class History:
    instrument_id: int
    instrument: Instrument
    as_of: date  # last bar date
    frame: pd.DataFrame  # open/high/low/close/volume up to as_of
    benchmark_close: pd.Series | None
    events: list[EarningsEvent]


async def load_history(
    sessions: async_sessionmaker[AsyncSession],
    instrument_id: int,
    as_of: date,
    benchmark: str | None,
) -> History | None:
    start = as_of - timedelta(days=HISTORY_DAYS)
    async with sessions() as s:
        instruments = await repo.active_instruments(s)
        inst = instruments.get(instrument_id)
        if inst is None:
            return None
        bars = [b for b in await repo.bars(s, instrument_id, start) if b.date <= as_of]
        events = await repo.earnings_events(s, instrument_id)
        bench_id = next((k for k, i in instruments.items() if i.yahoo_symbol == benchmark), None)
        bench = (
            [b for b in await repo.bars(s, bench_id, start) if b.date <= as_of] if bench_id else []
        )
    if not bars:
        return None
    return History(
        instrument_id=instrument_id,
        instrument=inst,
        as_of=bars[-1].date,
        frame=bars_to_frame(bars),
        benchmark_close=bars_to_frame(bench)["close"] if bench else None,
        events=events,
    )
