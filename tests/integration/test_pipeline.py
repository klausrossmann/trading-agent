"""Agent pipeline end to end against a real database, with a scripted model per agent."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from trading_agent import jobs, pipeline
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.market import Bar, BarSeries, Instrument, Observation
from trading_agent.llm.runner import LlmRunner
from trading_agent.settings import Settings

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 6, 6, 30, tzinfo=UTC)
PLAN = {"entry_ref": "close", "stop_ref": "atr_stop_2x", "target_ref": "atr_target_4x"}


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
            "sector": {"AAA": "Energy", "BBB": "Utilities"}.get(symbol),
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


class Agents:
    """Answers per output schema; the critic blocks the symbols in `block`."""

    def __init__(self, block: set[str]) -> None:
        self.block = block
        self.calls: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = info.output_tools[0]
        fields = set(tool.parameters_json_schema["properties"])
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        text = next(str(p.content) for p in request.parts if isinstance(p, UserPromptPart))
        inp: dict[str, Any] = json.loads(text.split("\n", 1)[1])
        gaps = ["see unknown"] if inp.get("unknown") or inp.get("facts", {}).get("unknown") else []
        if "rating" in fields:
            kind = "technical"
            args = {
                "trend_summary": "Up.",
                "momentum_summary": "Neutral.",
                "volume_summary": "Even.",
                "rating": "buy",
                "setup_quality": 0.6,
                "reasoning": "Uptrend.",
                "invalidation": "Close below atr_stop_2x.",
                "data_gaps": gaps,
                **PLAN,
            }
        elif "stance" in fields:
            kind = "earnings"
            args = {
                "summary": "No report known.",
                "track_record": "Unknown.",
                "reaction_pattern": "Unknown.",
                "event_risk": "low",
                "stance": "no_event_in_window",
                "bull_case": "b",
                "bear_case": "b",
                "confidence": 0.5,
                "data_gaps": gaps,
            }
        elif "decision" in fields:
            kind = "proposer"
            args = {
                "decision": "propose",
                "confidence": 0.4,
                "thesis": f"{inp['symbol']} pulls back in an uptrend.",
                "invalidation": "A close below atr_stop_2x.",
                "no_trade_reason": None,
                "data_gaps": gaps,
                **PLAN,
            }
        elif "objections" in fields:
            kind = "critic"
            blocking = inp["facts"]["symbol"] in self.block
            severity = "blocking" if blocking else "minor"
            args = {
                "objections": [{"point": "Thin evidence.", "severity": severity}],
                "severity": severity,
                "confidence_delta": -0.05,
                "summary": "Acceptable." if not blocking else "Don't.",
            }
        else:
            kind = "portfolio_manager"
            symbols = sorted(c["symbol"] for c in inp["candidates"])
            args = {
                "ranking": [{"symbol": s, "note": "Fits."} for s in reversed(symbols)],
                "rationale": "Diversified.",
            }
        self.calls.append(kind)
        return ModelResponse(parts=[ToolCallPart(tool.name, args)])


async def _context(
    sessions: Sessions, settings: Settings, script: Agents
) -> tuple[jobs.AnalysisContext, Universe]:
    universe = Universe(
        generated=date(2026, 10, 6),
        benchmarks={"US": "SPY"},
        instruments=[_inst("SPY", "etf", ()), _inst("AAA"), _inst("BBB"), _inst("CCC")],
    )
    await ingest.sync_universe(sessions, universe)
    async with sessions.begin() as s:
        ids = {i.yahoo_symbol: k for k, i in (await repo.active_instruments(s)).items()}
        for symbol, slope in (("SPY", 0.05), ("AAA", 0.1), ("BBB", 0.12), ("CCC", 0.08)):
            await repo.upsert_bars(s, ids[symbol], _series(symbol, slope))
        fx = [Observation(date=date(2026, 10, 1), value=Decimal("1.17"))]
        await repo.upsert_fx(s, "USD", fx, source="test", fetched_at=NOW)
    cfg = settings.model_copy(
        update={"config_dir": ROOT / "config", "prompts_dir": ROOT / "prompts"}
    )
    ctx = jobs.AnalysisContext.build(cfg, sessions, universe)

    async def no_pause(_: float) -> None:
        return None

    ctx.runner = LlmRunner(
        ctx.models,
        DbAnalysisStore(sessions),
        lambda _: FunctionModel(script),
        dev_overrides=True,  # critic on Gemini, as in Phase 0-1
        sleep=no_pause,
    )
    return ctx, universe


async def test_pipeline_stores_ranked_proposals(sessions: Sessions, settings: Settings) -> None:
    script = Agents(block={"CCC"})
    ctx, universe = await _context(sessions, settings, script)
    run = await pipeline.propose(ctx, symbols=["AAA", "BBB", "CCC"], today=date(2026, 10, 6))

    assert run.as_of == date(2026, 10, 5)
    assert run.skipped == {}
    assert script.calls.count("critic") == 3
    assert script.calls[-1] == "portfolio_manager"
    by_symbol = {p.yahoo_symbol: p for p in run.proposals}
    assert {s: (p.status, p.rank) for s, p in by_symbol.items()} == {
        "BBB": ("proposed", 1),
        "AAA": ("proposed", 2),
        "CCC": ("blocked", None),
    }
    aaa = by_symbol["AAA"]
    assert aaa.confidence == pytest.approx(0.35)
    assert aaa.risk_reward == pytest.approx(2.0, abs=0.02)
    assert aaa.stop is not None
    assert aaa.entry is not None
    assert aaa.stop < aaa.entry
    assert set(aaa.analyses) == {
        "technical",
        "earnings",
        "proposer",
        "critic",
        "portfolio_manager",
    }
    assert aaa.payload["portfolio_manager"]["note"] == "Fits."

    async with sessions() as s:
        stored = await proposals_repo.proposals(s, start=date(2026, 10, 5))
        await proposals_repo.set_label(s, aaa.id, "disagree")
        assert await proposals_repo.set_reason(s, aaa.id, "Extended.")
        await s.commit()
    assert [(p.yahoo_symbol, p.rank) for p in stored] == [
        ("BBB", 1),
        ("AAA", 2),
        ("CCC", None),
    ]

    # Same day again: cached analyses, no new LLM calls, same rows and labels.
    calls = len(script.calls)
    again = await pipeline.propose(ctx, symbols=["AAA", "BBB", "CCC"], today=date(2026, 10, 6))
    assert len(script.calls) == calls
    assert again.cost_usd == 0
    assert {p.id for p in again.proposals} == {p.id for p in run.proposals}
    async with sessions() as s:
        assert await proposals_repo.labels(s, [aaa.id]) == {aaa.id: ("disagree", "Extended.")}
        latest = await proposals_repo.latest_for_symbol(s, "aaa")
    assert latest is not None
    assert latest.id == aaa.id

    text = pipeline.summary(again, "US")
    assert text.splitlines()[1].startswith("1. BBB ")
    assert "Blocked by the critic: CCC" in text

    book = jobs.BookContext.load(ROOT / "config", sessions, universe)
    assert await jobs.agent_book(book, today=date(2026, 10, 6)) == []  # placed the next session
