"""Ingestion: idempotent, incremental loads from providers into the database."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_agent.data import calendars
from trading_agent.data.quality import QualityIssue, check_bars
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.db.market import UpsertCount
from trading_agent.domain.market import (
    Bar,
    BarSeries,
    EarningsEvent,
    Instrument,
    Market,
    Observation,
)
from trading_agent.settings import PricesConfig, QualityConfig

log = structlog.get_logger(__name__)

Sessions = async_sessionmaker[AsyncSession]


class PriceProvider(Protocol):
    source: str

    def daily_bars(self, instrument: Instrument, start: date, end: date) -> BarSeries: ...


@runtime_checkable
class AsyncPriceProvider(Protocol):
    source: str

    async def daily_bars_async(
        self, instrument: Instrument, start: date, end: date
    ) -> BarSeries: ...


class EarningsProvider(Protocol):
    source: str

    def earnings(self, instrument: Instrument, limit: int) -> tuple[EarningsEvent, ...]: ...


class SeriesProvider(Protocol):
    source: str

    async def series(self, key: str, start: date) -> list[Observation]: ...


@dataclass
class IngestResult:
    job: str
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    up_to_date: int = 0
    restated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def add(self, count: UpsertCount) -> None:
        self.inserted += count.inserted
        self.updated += count.updated

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated or self.removed or self.restated)


def describe_error(exc: BaseException) -> str:
    """Short error text that never contains request URLs (they may carry API keys)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"[:300]


def _years_before(day: date, years: int) -> date:
    return day - timedelta(days=round(365.25 * years))


def restated(stored: Sequence[Bar], fresh: Sequence[Bar], tolerance_pct: float) -> bool:
    """True if the source changed past closes beyond rounding, e.g. a split adjustment."""
    old = {b.date: b.close for b in stored}
    limit = Decimal(str(tolerance_pct))
    return any(abs(b.close / old[b.date] - 1) * 100 > limit for b in fresh if old.get(b.date))


async def sync_universe(sessions: Sessions, universe: Universe) -> IngestResult:
    result = IngestResult("universe")
    async with sessions.begin() as s:
        count, deactivated = await repo.sync_instruments(s, universe.instruments)
    result.add(count)
    result.removed = deactivated
    return result


async def ingest_prices(
    sessions: Sessions,
    provider: PriceProvider | AsyncPriceProvider,
    cfg: PricesConfig,
    *,
    now: datetime,
    market: Market | None = None,
) -> IngestResult:
    result = IngestResult(f"prices_{market or 'all'}".lower())
    async with sessions() as s:
        instruments = await repo.active_instruments(s, market)
        last_dates = await repo.last_bar_dates(s)

    for inst_id, inst in instruments.items():
        cal = calendars.CALENDAR_BY_MARKET[inst.market]
        end = calendars.last_closed_session(cal, now)
        full_start = _years_before(end, cfg.backfill_years)
        last = last_dates.get(inst_id)
        if last is not None and last >= end:
            result.up_to_date += 1
            continue
        start = (
            full_start
            if last is None
            else calendars.session_offset(cal, last, -cfg.overlap_sessions)
        )
        try:
            series = await _fetch(provider, inst, start, end)
            if last is not None:
                async with sessions() as s:
                    stored = await repo.bars(s, inst_id, start)
                if restated(stored, series.bars, cfg.restatement_tolerance_pct):
                    full = await _fetch(provider, inst, full_start, end)
                    async with sessions.begin() as s:
                        result.inserted += await repo.replace_bars(s, inst_id, full)
                    result.restated.append(inst.yahoo_symbol)
                    log.warning("prices.restated", symbol=inst.yahoo_symbol, bars=len(full.bars))
                    continue
            async with sessions.begin() as s:
                result.add(await repo.upsert_bars(s, inst_id, series))
        except Exception as exc:
            result.failed[inst.yahoo_symbol] = describe_error(exc)
    return result


async def _fetch(
    provider: PriceProvider | AsyncPriceProvider, inst: Instrument, start: date, end: date
) -> BarSeries:
    if isinstance(provider, AsyncPriceProvider):
        series = await provider.daily_bars_async(inst, start, end)
    else:
        series = await asyncio.to_thread(provider.daily_bars, inst, start, end)
    # Never store a bar for a session that hasn't closed yet (it would be partial).
    return series.model_copy(update={"bars": tuple(b for b in series.bars if b.date <= end)})


async def ingest_fx(
    sessions: Sessions,
    provider: SeriesProvider,
    series_by_quote: dict[str, str],
    *,
    now: datetime,
    backfill_years: int,
) -> IngestResult:
    result = IngestResult("fx")
    today = now.astimezone(UTC).date()
    for quote, key in series_by_quote.items():
        try:
            async with sessions() as s:
                last = await repo.last_fx_date(s, quote)
            start = (
                _years_before(today, backfill_years) if last is None else last + timedelta(days=1)
            )
            if start > today:
                result.up_to_date += 1
                continue
            observations = await provider.series(key, start)
            async with sessions.begin() as s:
                result.add(
                    await repo.upsert_fx(
                        s, quote, observations, source=provider.source, fetched_at=datetime.now(UTC)
                    )
                )
        except Exception as exc:
            result.failed[quote] = describe_error(exc)
    return result


async def ingest_macro(
    sessions: Sessions,
    providers: Sequence[tuple[SeriesProvider | None, dict[str, str]]],
    *,
    now: datetime,
    backfill_years: int,
    revision_days: int = 120,
) -> IngestResult:
    """`providers`: (provider or None if not configured, {series_id: provider key})."""
    result = IngestResult("macro")
    today = now.astimezone(UTC).date()
    for provider, series in providers:
        for series_id, key in series.items():
            if provider is None:
                result.skipped.append(series_id)
                continue
            try:
                async with sessions() as s:
                    last = await repo.last_macro_date(s, series_id)
                start = (
                    _years_before(today, backfill_years)
                    if last is None
                    else last - timedelta(days=revision_days)  # monthly data gets revised
                )
                observations = await provider.series(key, start)
                async with sessions.begin() as s:
                    result.add(
                        await repo.upsert_macro(
                            s,
                            series_id,
                            observations,
                            source=provider.source,
                            fetched_at=datetime.now(UTC),
                        )
                    )
            except Exception as exc:
                result.failed[series_id] = describe_error(exc)
    if result.skipped:
        log.warning("macro.skipped", series=result.skipped, reason="provider not configured")
    return result


async def ingest_earnings(
    sessions: Sessions, provider: EarningsProvider, *, now: datetime, limit: int
) -> IngestResult:
    result = IngestResult("earnings")
    async with sessions() as s:
        stocks = {k: i for k, i in (await repo.active_instruments(s)).items() if i.kind == "stock"}
    today = now.astimezone(UTC).date()
    for inst_id, inst in stocks.items():
        try:
            events = await asyncio.to_thread(provider.earnings, inst, limit)
            async with sessions.begin() as s:
                count, removed = await repo.upsert_earnings(
                    s,
                    inst_id,
                    events,
                    today=today,
                    source=provider.source,
                    fetched_at=datetime.now(UTC),
                )
            result.add(count)
            result.removed += removed
        except Exception as exc:
            result.failed[inst.yahoo_symbol] = describe_error(exc)
    return result


async def run_quality(
    sessions: Sessions, cfg: QualityConfig, *, now: datetime, market: Market | None = None
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    async with sessions() as s:
        for inst_id, inst in (await repo.active_instruments(s, market)).items():
            cal = calendars.CALENDAR_BY_MARKET[inst.market]
            end = calendars.last_closed_session(cal, now)
            bars = await repo.bars(s, inst_id)
            expected = calendars.sessions(cal, bars[0].date, end) if bars else []
            issues += check_bars(inst.yahoo_symbol, bars, expected, cfg)
    return issues
