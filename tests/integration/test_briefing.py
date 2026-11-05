"""Briefing and /status assembled from a real database."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from trading_agent import briefing
from trading_agent.contexts import BookContext, RuntimeState
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.domain.market import Bar, BarSeries, EarningsEvent, Instrument, Observation
from trading_agent.risk.config import load_risk_config
from trading_agent.settings import load_fees
from trading_agent.strategies.pullback import load_params

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 6, 30, tzinfo=UTC)


def inst(symbol: str, kind: str = "stock", indices: tuple[str, ...] = ("SP100",)) -> Instrument:
    return Instrument.model_validate(
        {
            "symbol": symbol,
            "yahoo_symbol": symbol,
            "name": symbol,
            "market": "US",
            "exchange": "SMART",
            "currency": "USD",
            "kind": kind,
            "indices": indices,
        }
    )


def series(symbol: str, slope: float) -> BarSeries:
    days = pd.bdate_range(end="2026-10-05", periods=320)
    bars = tuple(
        Bar(
            date=d.date(),
            open=Decimal(str(round(100 + slope * i, 2))),
            high=Decimal(str(round(101 + slope * i, 2))),
            low=Decimal(str(round(99 + slope * i, 2))),
            close=Decimal(str(round(100 + slope * i, 2))),
            volume=1_000_000,
        )
        for i, d in enumerate(days)
    )
    return BarSeries(yahoo_symbol=symbol, source="test", fetched_at=NOW, bars=bars)


@pytest.fixture
async def book(sessions: Sessions) -> BookContext:
    universe = Universe(
        generated=TODAY,
        benchmarks={"US": "SPY"},
        instruments=[inst("SPY", "etf", ()), inst("AAA")],
    )
    await ingest.sync_universe(sessions, universe)
    async with sessions.begin() as s:
        ids = {i.yahoo_symbol: k for k, i in (await repo.active_instruments(s)).items()}
        await repo.upsert_bars(s, ids["SPY"], series("SPY", 0.5))
        await repo.upsert_bars(s, ids["AAA"], series("AAA", 0.1))
        await repo.upsert_fx(
            s, "USD", [Observation(date=TODAY, value=Decimal("1.25"))], source="t", fetched_at=NOW
        )
        await repo.upsert_earnings(
            s,
            ids["AAA"],
            [
                EarningsEvent(
                    date=date(2026, 10, 8),
                    ts=None,
                    timing="amc",
                    eps_estimate=None,
                    eps_actual=None,
                )
            ],
            today=TODAY,
            source="t",
            fetched_at=NOW,
        )
    return BookContext(
        sessions,
        load_params(ROOT / "config"),
        load_risk_config(ROOT / "config"),
        load_fees(ROOT / "config"),
        {"US": "SPY"},
    )


async def test_briefing_from_database(book: BookContext) -> None:
    state = RuntimeState(mode="paper", started_at=NOW, blocked={"US": []})
    b = await briefing.build_briefing(book, state, TODAY)
    assert [(m.name, m.last_date) for m in b.markets] == [("SPY", date(2026, 10, 5))]
    assert b.markets[0].change_pct == pytest.approx((259.5 / 259.0 - 1) * 100)
    assert b.markets[0].trend == {"daily": "up", "weekly": "up"}
    assert b.eur_usd == 1.25
    assert [(e.symbol, e.date) for e in b.earnings] == [("AAA", date(2026, 10, 8))]
    assert b.last_bars == {"US": date(2026, 10, 5), "EU": None}
    assert b.blocked == []

    text = await briefing.briefing_text(book, state, TODAY)
    assert "AAA Thu 8 Oct amc" in text


async def test_status_from_database(book: BookContext) -> None:
    state = RuntimeState(mode="paper", started_at=NOW - timedelta(minutes=30))
    text = await briefing.status_text(state, book.sessions, now=NOW)
    assert "Agent running (paper), up 30 min" in text
    assert "Heartbeat: not configured" in text
    assert "last bars US 05 Oct, EU -" in text
    assert "not checked since start" in text


async def test_status_from_database_with_links(book: BookContext) -> None:
    state = RuntimeState(
        mode="paper",
        started_at=NOW - timedelta(minutes=30),
        trace_ui_url="https://cloud.langfuse.com/project/p1",
        dashboard_ui_url="https://agent.example.com",
    )
    text = await briefing.status_text(state, book.sessions, now=NOW)
    assert "Tracing UI: https://cloud.langfuse.com/project/p1" in text
    assert "Dashboard: https://agent.example.com" in text
