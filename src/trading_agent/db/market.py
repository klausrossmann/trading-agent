"""Market-data repositories: idempotent upserts and the reads that ingestion needs."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import batched
from typing import Any, cast

from sqlalchemy import Table, delete, func, literal_column, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.db.models import (
    BarDailyRow,
    Base,
    EarningsEventRow,
    FxDailyRow,
    InstrumentRow,
    MacroSeriesRow,
)
from trading_agent.domain.market import (
    Bar,
    BarSeries,
    EarningsEvent,
    Instrument,
    Market,
    Observation,
)

_CHUNK = 1000


@dataclass
class UpsertCount:
    inserted: int = 0
    updated: int = 0

    def __add__(self, other: "UpsertCount") -> "UpsertCount":
        return UpsertCount(self.inserted + other.inserted, self.updated + other.updated)


def _table(model: type[Base]) -> Table:
    return cast(Table, model.__table__)


async def _upsert(
    session: AsyncSession,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    *,
    key: Sequence[str],
    compare: Sequence[str],
    also_set: Iterable[str] = ("source", "fetched_at"),
) -> UpsertCount:
    """Insert new rows; update existing ones only if a compared column changed."""
    table = _table(model)
    count = UpsertCount()
    for chunk in batched(rows, _CHUNK, strict=False):
        stmt = pg_insert(table).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=list(key),
            set_={c: stmt.excluded[c] for c in [*compare, *also_set]},
            where=tuple_(*(table.c[c] for c in compare)).is_distinct_from(
                tuple_(*(stmt.excluded[c] for c in compare))
            ),
        ).returning(literal_column("xmax = 0"))  # true for inserts, false for updates
        flags = (await session.execute(stmt)).scalars().all()
        inserted = sum(1 for f in flags if f)
        count += UpsertCount(inserted, len(flags) - inserted)
    return count


# --- instruments ---


def _to_instrument(row: InstrumentRow) -> Instrument:
    return Instrument.model_validate(
        {
            "id": row.id,
            "symbol": row.symbol,
            "yahoo_symbol": row.yahoo_symbol,
            "name": row.name,
            "market": row.market,
            "exchange": row.exchange,
            "currency": row.currency,
            "kind": row.kind,
            "sector": row.sector,
            "indices": tuple(row.indices),
            "conid": row.conid,
        }
    )


async def sync_instruments(
    session: AsyncSession, instruments: Sequence[Instrument]
) -> tuple[UpsertCount, int]:
    """Upsert the universe; deactivate instruments no longer in it. Returns (count, deactivated)."""
    fields = ["symbol", "name", "market", "exchange", "currency", "kind", "sector", "indices"]
    rows = [
        {**{f: getattr(i, f) for f in fields}, "yahoo_symbol": i.yahoo_symbol, "active": True}
        for i in instruments
    ]
    for row in rows:
        row["indices"] = list(row["indices"])
    count = await _upsert(
        session,
        InstrumentRow,
        rows,
        key=["yahoo_symbol"],
        compare=[*fields, "active"],
        also_set=[],
    )
    result = await session.execute(
        update(InstrumentRow)
        .where(
            InstrumentRow.active,
            InstrumentRow.yahoo_symbol.not_in([i.yahoo_symbol for i in instruments]),
        )
        .values(active=False, updated_at=func.now())
        .returning(InstrumentRow.id)
    )
    return count, len(result.all())


async def active_instruments(
    session: AsyncSession, market: Market | None = None
) -> dict[int, Instrument]:
    """Active instruments keyed by database id, ordered by Yahoo symbol."""
    stmt = select(InstrumentRow).where(InstrumentRow.active).order_by(InstrumentRow.yahoo_symbol)
    if market is not None:
        stmt = stmt.where(InstrumentRow.market == market)
    return {r.id: _to_instrument(r) for r in await session.scalars(stmt)}


# --- daily bars ---


def _bar_rows(instrument_id: int, series: BarSeries) -> list[dict[str, Any]]:
    return [
        {
            "instrument_id": instrument_id,
            **b.model_dump(),
            "source": series.source,
            "fetched_at": series.fetched_at,
        }
        for b in series.bars
    ]


async def upsert_bars(session: AsyncSession, instrument_id: int, series: BarSeries) -> UpsertCount:
    return await _upsert(
        session,
        BarDailyRow,
        _bar_rows(instrument_id, series),
        key=["instrument_id", "date"],
        compare=["open", "high", "low", "close", "volume"],
    )


async def replace_bars(session: AsyncSession, instrument_id: int, series: BarSeries) -> int:
    """Replace the full history, e.g. after the source restated it for a split."""
    await session.execute(delete(BarDailyRow).where(BarDailyRow.instrument_id == instrument_id))
    return (await upsert_bars(session, instrument_id, series)).inserted


async def last_bar_dates(session: AsyncSession) -> dict[int, date]:
    stmt = select(BarDailyRow.instrument_id, func.max(BarDailyRow.date)).group_by(
        BarDailyRow.instrument_id
    )
    return {i: d for i, d in await session.execute(stmt)}


async def bars(session: AsyncSession, instrument_id: int, start: date | None = None) -> list[Bar]:
    stmt = select(BarDailyRow).where(BarDailyRow.instrument_id == instrument_id)
    if start is not None:
        stmt = stmt.where(BarDailyRow.date >= start)
    rows = await session.scalars(stmt.order_by(BarDailyRow.date))
    return [
        Bar(date=r.date, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
        for r in rows
    ]


async def all_bars(session: AsyncSession) -> dict[int, list[Bar]]:
    """Every stored bar, grouped by instrument id, oldest first."""
    stmt = select(BarDailyRow).order_by(BarDailyRow.instrument_id, BarDailyRow.date)
    out: dict[int, list[Bar]] = {}
    for r in await session.scalars(stmt):
        out.setdefault(r.instrument_id, []).append(
            Bar(date=r.date, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
        )
    return out


async def earnings_dates(session: AsyncSession) -> dict[int, list[date]]:
    out: dict[int, list[date]] = {}
    for inst_id, day in await session.execute(
        select(EarningsEventRow.instrument_id, EarningsEventRow.date)
    ):
        out.setdefault(inst_id, []).append(day)
    return out


async def upcoming_earnings(
    session: AsyncSession, start: date, end: date
) -> list[tuple[int, date, str]]:
    """(instrument id, date, timing) for announcements between start and end, inclusive."""
    stmt = (
        select(EarningsEventRow.instrument_id, EarningsEventRow.date, EarningsEventRow.timing)
        .where(EarningsEventRow.date >= start, EarningsEventRow.date <= end)
        .order_by(EarningsEventRow.date)
    )
    return [(i, d, t) for i, d, t in await session.execute(stmt)]


async def fx_rates(session: AsyncSession, quote: str) -> list[Observation]:
    stmt = select(FxDailyRow).where(FxDailyRow.quote == quote).order_by(FxDailyRow.date)
    return [Observation(date=r.date, value=r.rate) for r in await session.scalars(stmt)]


# --- FX and macro series ---


async def last_fx_date(session: AsyncSession, quote: str) -> date | None:
    return await session.scalar(select(func.max(FxDailyRow.date)).where(FxDailyRow.quote == quote))


async def upsert_fx(
    session: AsyncSession,
    quote: str,
    observations: Sequence[Observation],
    *,
    source: str,
    fetched_at: datetime,
) -> UpsertCount:
    rows = [
        {
            "date": o.date,
            "quote": quote,
            "rate": o.value,
            "source": source,
            "fetched_at": fetched_at,
        }
        for o in observations
    ]
    return await _upsert(session, FxDailyRow, rows, key=["date", "quote"], compare=["rate"])


async def last_macro_date(session: AsyncSession, series_id: str) -> date | None:
    return await session.scalar(
        select(func.max(MacroSeriesRow.date)).where(MacroSeriesRow.series_id == series_id)
    )


async def upsert_macro(
    session: AsyncSession,
    series_id: str,
    observations: Sequence[Observation],
    *,
    source: str,
    fetched_at: datetime,
) -> UpsertCount:
    rows = [
        {
            "series_id": series_id,
            "date": o.date,
            "value": o.value,
            "source": source,
            "fetched_at": fetched_at,
        }
        for o in observations
    ]
    return await _upsert(
        session, MacroSeriesRow, rows, key=["series_id", "date"], compare=["value"]
    )


# --- earnings ---


async def upsert_earnings(
    session: AsyncSession,
    instrument_id: int,
    events: Sequence[EarningsEvent],
    *,
    today: date,
    source: str,
    fetched_at: datetime,
) -> tuple[UpsertCount, int]:
    """Upsert events and drop future dates the source no longer reports (moved announcements).

    Returns (count, removed).
    """
    rows = [
        {
            "instrument_id": instrument_id,
            **e.model_dump(),
            "source": source,
            "fetched_at": fetched_at,
        }
        for e in events
    ]
    count = await _upsert(
        session,
        EarningsEventRow,
        rows,
        key=["instrument_id", "date"],
        compare=["ts", "timing", "eps_estimate", "eps_actual"],
    )
    removed = await session.execute(
        delete(EarningsEventRow)
        .where(
            EarningsEventRow.instrument_id == instrument_id,
            EarningsEventRow.date >= today,
            EarningsEventRow.date.not_in([e.date for e in events]),
        )
        .returning(EarningsEventRow.date)
    )
    return count, len(removed.all())
