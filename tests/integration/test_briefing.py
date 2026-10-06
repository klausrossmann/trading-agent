"""Briefing and /status assembled from a real database."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from trading_agent import backtest, jobs
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.db import trades as trades_repo
from trading_agent.domain.market import Bar, BarSeries, EarningsEvent, Instrument, Observation
from trading_agent.domain.trading import Trade
from trading_agent.risk.config import load_risk_config
from trading_agent.settings import load_fees

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
async def book(sessions: Sessions) -> jobs.BookContext:
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
        base = {
            "book": "baseline_sim",
            "strategy": "pullback_uptrend",
            "instrument_id": ids["AAA"],
            "yahoo_symbol": "AAA",
            "market": "US",
            "sector": None,
            "signal_date": date(2026, 9, 30),
            "entry_date": date(2026, 10, 1),
            "quantity": 10,
            "risk_eur": 16,
            "fees": 0.8,
            "fees_eur": 0.64,
        }
        await trades_repo.replace_book(
            s,
            "baseline_sim",
            [
                Trade.model_validate(base | {"entry_price": 129.9, "stop": 127.9, "target": 133.9}),
                Trade.model_validate(
                    base
                    | {
                        "entry_price": 120.0,
                        "stop": 118.0,
                        "target": 124.0,
                        "exit_date": date(2026, 10, 2),
                        "exit_price": 124.0,
                        "exit_reason": "target",
                        "pnl_net_eur": 30.0,
                        "r_multiple": 1.9,
                        "holding_sessions": 1,
                    }
                ),
            ],
        )
    cfg = backtest.BacktestConfig(baseline_book=backtest.BaselineBook(start=date(2026, 10, 1)))
    return jobs.BookContext(
        sessions, cfg, load_risk_config(ROOT / "config"), load_fees(ROOT / "config"), {"US": "SPY"}
    )


async def test_briefing_from_database(book: jobs.BookContext) -> None:
    state = jobs.RuntimeState(mode="paper", started_at=NOW, blocked={"US": []})
    b = await jobs.build_briefing(book, state, TODAY)
    assert [(m.name, m.last_date) for m in b.markets] == [("SPY", date(2026, 10, 5))]
    assert b.markets[0].change_pct == pytest.approx((259.5 / 259.0 - 1) * 100)
    assert b.markets[0].trend == {"daily": "up", "weekly": "up"}
    assert b.eur_usd == 1.25
    assert [(e.symbol, e.date) for e in b.earnings] == [("AAA", date(2026, 10, 8))]
    assert b.book is not None
    us = next(s for s in b.book if s.label == "US")
    assert (us.closed_trades, us.closed_pnl_eur, us.budget_eur) == (1, 30.0, 10000)
    (position,) = us.open
    # last close 131.9: (131.9 - 129.9) / 2 = +1 R; 10 x 2 USD / 1.25 - 0.64 fees
    assert position.r_now == pytest.approx(1.0)
    assert position.pnl_eur == pytest.approx(16 - 0.64)
    assert b.last_bars == {"US": date(2026, 10, 5), "EU": None}
    assert b.blocked == []

    text = await jobs.briefing_text(book, state, TODAY)
    assert "AAA Thu 8 Oct amc" in text


async def test_status_from_database(book: jobs.BookContext) -> None:
    state = jobs.RuntimeState(mode="paper", started_at=NOW - timedelta(minutes=30))
    text = await jobs.status_text(state, book.sessions, now=NOW)
    assert "Agent running (paper), up 30 min" in text
    assert "Heartbeat: not configured" in text
    assert "last bars US 05 Oct, EU -" in text
    assert "not checked since start" in text
