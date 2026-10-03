"""Ingestion against a real Postgres, with fake providers (no network)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from trading_agent.data import calendars, ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.db.models import BarDailyRow, EarningsEventRow
from trading_agent.domain.market import (
    Bar,
    BarSeries,
    EarningsEvent,
    Instrument,
    Observation,
)
from trading_agent.settings import PricesConfig, QualityConfig

pytestmark = pytest.mark.db

PRICES = PricesConfig(backfill_years=1, overlap_sessions=5, restatement_tolerance_pct=0.5)
QUALITY = QualityConfig(max_jump_pct=40, stale_sessions=2, block_window_sessions=20)
FRIDAY_NIGHT = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)  # after both closes
MONDAY_NIGHT = datetime(2026, 10, 5, 21, 0, tzinfo=UTC)


def inst(symbol: str, market: str = "US") -> Instrument:
    return Instrument.model_validate(
        {
            "symbol": symbol,
            "yahoo_symbol": symbol if market == "US" else f"{symbol}.DE",
            "name": symbol,
            "market": market,
            "exchange": "SMART" if market == "US" else "IBIS",
            "currency": "USD" if market == "US" else "EUR",
            "kind": "stock",
            "indices": ["SP100" if market == "US" else "DAX"],
        }
    )


UNIVERSE = Universe(
    generated=date(2026, 10, 3), benchmarks={}, instruments=[inst("AAA"), inst("BBB", "EU")]
)


class FakePrices:
    """Deterministic bars on every exchange session; `scale` simulates a split restatement."""

    source = "fake"

    def __init__(self) -> None:
        self.scale = Decimal(1)
        self.calls: list[tuple[str, date, date]] = []

    def daily_bars(self, instrument: Instrument, start: date, end: date) -> BarSeries:
        self.calls.append((instrument.yahoo_symbol, start, end))
        cal = calendars.CALENDAR_BY_MARKET[instrument.market]
        days = calendars.sessions(cal, start, end + timedelta(days=3))  # also "future" bars
        bars = []
        for d in days:
            c = (Decimal(100) + Decimal(d.toordinal() % 50) / 10) * self.scale
            bars.append(Bar(date=d, open=c, high=c + 1, low=c - 1, close=c, volume=1000))
        return BarSeries(
            yahoo_symbol=instrument.yahoo_symbol,
            source=self.source,
            fetched_at=datetime.now(UTC),
            bars=tuple(bars),
        )


async def _bar_count(sessions: Sessions) -> int:
    async with sessions() as s:
        return await s.scalar(select(func.count()).select_from(BarDailyRow)) or 0


async def test_universe_sync_is_idempotent_and_deactivates(sessions: Sessions) -> None:
    first = await ingest.sync_universe(sessions, UNIVERSE)
    again = await ingest.sync_universe(sessions, UNIVERSE)
    assert (first.inserted, again.inserted, again.updated) == (2, 0, 0)

    smaller = UNIVERSE.model_copy(update={"instruments": UNIVERSE.instruments[:1]})
    assert (await ingest.sync_universe(sessions, smaller)).removed == 1
    async with sessions() as s:
        assert [i.yahoo_symbol for i in (await repo.active_instruments(s)).values()] == ["AAA"]


async def test_prices_backfill_incremental_and_restatement(sessions: Sessions) -> None:
    await ingest.sync_universe(sessions, UNIVERSE)
    provider = FakePrices()

    first = await ingest.ingest_prices(sessions, provider, PRICES, now=FRIDAY_NIGHT)
    assert first.inserted > 400
    assert not first.failed
    assert first.updated == 0
    total = await _bar_count(sessions)
    async with sessions() as s:
        assert set((await repo.last_bar_dates(s)).values()) == {date(2026, 10, 2)}  # no future bars

    again = await ingest.ingest_prices(sessions, provider, PRICES, now=FRIDAY_NIGHT)
    assert (again.changed, again.up_to_date) == (False, 2)

    monday = await ingest.ingest_prices(sessions, provider, PRICES, now=MONDAY_NIGHT)
    assert (monday.inserted, monday.updated, monday.restated) == (2, 0, [])
    assert provider.calls[-1][1] == calendars.session_offset("XETR", date(2026, 10, 2), -5)

    provider.scale = Decimal("0.5")  # source restated history (2:1 split)
    later = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)
    split = await ingest.ingest_prices(sessions, provider, PRICES, now=later, market="US")
    assert split.restated == ["AAA"]
    async with sessions() as s:
        (aaa_id,) = (await repo.active_instruments(s, "US")).keys()
        bars = await repo.bars(s, aaa_id)
    assert bars[0].close < Decimal(60)  # whole history replaced, not just the overlap
    assert bars[-1].date == date(2026, 10, 6)
    assert await _bar_count(sessions) > total


async def test_quality_is_clean_after_ingest(sessions: Sessions) -> None:
    await ingest.sync_universe(sessions, UNIVERSE)
    await ingest.ingest_prices(sessions, FakePrices(), PRICES, now=FRIDAY_NIGHT)
    assert await ingest.run_quality(sessions, QUALITY, now=FRIDAY_NIGHT) == []
    stale = await ingest.run_quality(sessions, QUALITY, now=FRIDAY_NIGHT + timedelta(days=7))
    assert {(i.yahoo_symbol, i.check) for i in stale} == {("AAA", "stale"), ("BBB.DE", "stale")}


class FakeEarnings:
    source = "fake"

    def __init__(self, dates: list[date]) -> None:
        self.dates = dates

    def earnings(self, instrument: Instrument, limit: int) -> tuple[EarningsEvent, ...]:
        return tuple(
            EarningsEvent(
                date=d, ts=None, timing="unknown", eps_estimate=Decimal("1.5"), eps_actual=None
            )
            for d in self.dates
        )


async def test_earnings_moved_announcement_replaces_old_date(sessions: Sessions) -> None:
    await ingest.sync_universe(sessions, UNIVERSE)
    past, planned, moved = date(2026, 7, 30), date(2026, 10, 29), date(2026, 10, 27)
    first = await ingest.ingest_earnings(
        sessions, FakeEarnings([past, planned]), now=FRIDAY_NIGHT, limit=4
    )
    assert (first.inserted, first.removed) == (4, 0)
    second = await ingest.ingest_earnings(
        sessions, FakeEarnings([past, moved]), now=FRIDAY_NIGHT, limit=4
    )
    assert (second.inserted, second.updated, second.removed) == (2, 0, 2)
    async with sessions() as s:
        dates = set(await s.scalars(select(EarningsEventRow.date)))
    assert dates == {past, moved}


class FakeSeries:
    source = "fake"

    def __init__(self) -> None:
        self.starts: list[date] = []

    async def series(self, key: str, start: date) -> list[Observation]:
        self.starts.append(start)
        days = [start + timedelta(days=i) for i in range(0, (date(2026, 10, 2) - start).days + 1)]
        return [Observation(date=d, value=Decimal("1.1")) for d in days if d.weekday() < 5]


async def test_fx_and_macro_are_incremental(sessions: Sessions) -> None:
    fake = FakeSeries()
    first = await ingest.ingest_fx(sessions, fake, {"USD": "k"}, now=FRIDAY_NIGHT, backfill_years=1)
    second = await ingest.ingest_fx(
        sessions, fake, {"USD": "k"}, now=FRIDAY_NIGHT, backfill_years=1
    )
    assert first.inserted > 250
    assert not second.changed
    assert len(fake.starts) == 1  # already up to date: no request at all

    providers = [(fake, {"S1": "k"}), (None, {"NO_KEY": "k"})]
    macro1 = await ingest.ingest_macro(sessions, providers, now=FRIDAY_NIGHT, backfill_years=1)
    macro2 = await ingest.ingest_macro(sessions, providers, now=FRIDAY_NIGHT, backfill_years=1)
    assert macro1.inserted > 250
    assert macro1.skipped == ["NO_KEY"]
    assert not macro2.changed  # re-read the revision window, nothing differed
