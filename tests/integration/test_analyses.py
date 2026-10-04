"""Analyses and LLM calls in a real database; a scan with a test model end to end."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from pydantic_ai.models.test import TestModel

from trading_agent import jobs
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.analysis import AnalysisRecord, LlmCall, ValidationIssue
from trading_agent.domain.market import Bar, BarSeries, Instrument
from trading_agent.llm.runner import LlmRunner
from trading_agent.settings import Settings

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 6, 6, 30, tzinfo=UTC)


def _inst(symbol: str, kind: str = "stock", indices: tuple[str, ...] = ("SP100",)) -> Instrument:
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


def _series(symbol: str, slope: float) -> BarSeries:
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


def _call(model: str, cost: float, error: str | None = None) -> LlmCall:
    return LlmCall(
        role="analysis",
        model=model,
        module="technical",
        tokens_in=1000,
        tokens_out=200,
        requests=1,
        cost_usd=cost,
        latency_ms=850,
        error=error,
    )


async def test_store_round_trip(sessions: Sessions) -> None:
    store = DbAnalysisStore(sessions)
    record = AnalysisRecord(
        module="technical",
        prompt_version=1,
        model="google:m",
        instrument_id=None,
        as_of=date(2026, 10, 2),
        input_hash="h1",
        input={"x": 1},
        output={"rating": "buy"},
        issues=(ValidationIssue(code="ungrounded_number", message="m", severity="warning"),),
        status="ok",
        cost_usd=0.0012345,
    )
    assert await store.cached("h1") is None
    analysis_id = await store.save(record, [_call("google:m", 0.001), _call("google:m", 0.0002345)])
    assert analysis_id is not None
    assert await store.save(None, [_call("google:x", 0.5, "HTTP 429")]) is None

    hit = await store.cached("h1")
    assert hit is not None
    assert (hit.id, hit.output, hit.status) == (analysis_id, {"rating": "buy"}, "ok")
    assert hit.issues == record.issues
    since = datetime(2026, 1, 1, tzinfo=UTC)
    assert await store.spent_usd(since) == pytest.approx(0.5012345)
    assert await store.spent_usd(datetime(2999, 1, 1, tzinfo=UTC)) == 0
    by_model = await store.spend_by_model(since)
    assert [(m, n) for m, n, _ in by_model] == [("google:x", 1), ("google:m", 2)]


async def test_scan_with_test_model(sessions: Sessions, settings: Settings) -> None:
    universe = Universe(
        generated=date(2026, 10, 6),
        benchmarks={"US": "SPY"},
        instruments=[_inst("SPY", "etf", ()), _inst("AAA")],
    )
    await ingest.sync_universe(sessions, universe)
    async with sessions.begin() as s:
        ids = {i.yahoo_symbol: k for k, i in (await repo.active_instruments(s)).items()}
        await repo.upsert_bars(s, ids["SPY"], _series("SPY", 0.5))
        await repo.upsert_bars(s, ids["AAA"], _series("AAA", 0.1))

    cfg = settings.model_copy(
        update={"config_dir": ROOT / "config", "prompts_dir": ROOT / "prompts"}
    )
    ctx = jobs.AnalysisContext.build(cfg, sessions, universe)
    ctx.runner = LlmRunner(ctx.models, DbAnalysisStore(sessions), lambda _: TestModel())

    result = await jobs.analyse(ctx, ["AAA", "NOPE"], today=date(2026, 10, 6))
    assert result.missing == ["NOPE"]
    (item,) = result.items
    assert item.technical_input.as_of == date(2026, 10, 5)
    assert item.earnings_input.unknown[:2] == ["history", "next_report"]
    assert all(o.analysis_id is not None and not o.cached for o in item.outcomes)
    assert result.cost_usd > 0

    again = await jobs.analyse(ctx, ["AAA"], today=date(2026, 10, 6))
    assert all(o.cached for o in again.items[0].outcomes)
    assert again.cost_usd == 0

    text = await jobs.budget_text(ctx)
    assert text.startswith("💸 LLM budget")
    assert "google:gemini-3.8-flash: " in text
