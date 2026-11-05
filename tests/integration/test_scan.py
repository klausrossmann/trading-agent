"""The orchestrator's scan end to end against a real database, with a scripted model per
agent."""

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

from trading_agent import orchestrator
from trading_agent.contexts import AnalysisContext
from trading_agent.core import step_store as steps_repo
from trading_agent.core.db import Sessions
from trading_agent.core.settings import Settings
from trading_agent.core.steps import Step
from trading_agent.core.tables import AnalysisRow
from trading_agent.llm.fake import fake_model
from trading_agent.llm.runner import LlmRunner
from trading_agent.llm.store import DbAnalysisStore
from trading_agent.marketdata import ingest
from trading_agent.marketdata import store as repo
from trading_agent.marketdata.types import Bar, BarSeries, Instrument, Observation
from trading_agent.marketdata.universe import Universe
from trading_agent.proposer import stage as proposer_stage
from trading_agent.proposer import store as proposals_repo

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
                "confidence": 0.5 if inp["symbol"] == "BBB" else 0.4,
                "thesis": f"{inp['symbol']} pulls back in an uptrend.",
                "invalidation": "A close below atr_stop_2x.",
                "no_trade_reason": None,
                "data_gaps": gaps,
                **PLAN,
            }
        else:
            kind = "critic"
            blocking = inp["facts"]["symbol"] in self.block
            severity = "blocking" if blocking else "minor"
            args = {
                "objections": [{"point": "Thin evidence.", "severity": severity}],
                "severity": severity,
                "confidence_delta": -0.05,
                "summary": "Acceptable." if not blocking else "Don't.",
            }
        self.calls.append(kind)
        return ModelResponse(parts=[ToolCallPart(tool.name, args)])


async def _context(
    sessions: Sessions, settings: Settings, script: Agents
) -> tuple[AnalysisContext, Universe]:
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
    ctx = AnalysisContext.build(cfg, sessions, universe)

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
    ctx, _ = await _context(sessions, settings, script)
    run = await orchestrator.scan(ctx, symbols=["AAA", "BBB", "CCC"], today=date(2026, 10, 6))

    assert run.as_of == date(2026, 10, 5)
    assert run.skipped == {}
    assert script.calls.count("critic") == 3
    assert script.calls[-1] == "critic"  # ranking is code, no LLM call
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
    assert set(aaa.analyses) == {"technical", "earnings", "proposer", "critic"}

    async with sessions() as s:
        steps = await steps_repo.steps(s, day=date(2026, 10, 6))
    by = {(x.symbol, x.step): x for x in steps}
    assert [x.step for x in steps if x.symbol == "AAA"] == [
        "technical",
        "earnings",
        "proposer",
        "critic",
        "rank",
    ]
    assert all(x.proposal_id == aaa.id for x in steps if x.symbol == "AAA")
    assert by[("AAA", "technical")].analysis_id == aaa.analyses["technical"]
    assert by[("AAA", "technical")].summary == "rating buy, setup quality 0.60"
    assert by[("AAA", "earnings")].summary == "no_event_in_window, event risk low"
    assert by[("AAA", "proposer")].summary.startswith("buy at close ")
    assert by[("AAA", "critic")].summary == "minor, confidence -0.05: Acceptable."
    assert by[("AAA", "rank")].summary.startswith("rank 2: confidence 0.35 x R:R 2.0")
    assert (by[("CCC", "critic")].status, ("CCC", "rank") in by) == ("stopped", False)

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
    again = await orchestrator.scan(ctx, symbols=["AAA", "BBB", "CCC"], today=date(2026, 10, 6))
    assert len(script.calls) == calls
    assert again.cost_usd == 0
    assert {p.id for p in again.proposals} == {p.id for p in run.proposals}
    async with sessions() as s:
        assert await proposals_repo.labels(s, [aaa.id]) == {aaa.id: ("disagree", "Extended.")}
        latest = await proposals_repo.latest_for_symbol(s, "aaa")
    assert latest is not None
    assert latest.id == aaa.id
    async with sessions() as s:
        assert len(await steps_repo.steps(s, day=date(2026, 10, 6))) == len(steps)

    text = orchestrator.summary(again, "US")
    assert text.splitlines()[1].startswith("1. BBB ")
    assert "Blocked by the critic: CCC" in text


async def test_offline_fake_model_and_macro_context(sessions: Sessions, settings: Settings) -> None:
    ctx, _ = await _context(sessions, settings, Agents(block=set()))
    ctx.runner = LlmRunner(
        ctx.models, DbAnalysisStore(sessions), lambda _: fake_model(), dev_overrides=True
    )
    run = await orchestrator.scan(ctx, symbols=["AAA", "BBB", "CCC"], today=date(2026, 10, 6))
    assert run.skipped == {}
    assert {p.status for p in run.proposals} == {"proposed"}
    assert all(p.risk_reward is not None and p.risk_reward >= 2 for p in run.proposals)

    macro = await proposer_stage.macro_context(ctx, "US", date(2026, 10, 5))
    assert macro.benchmark == "SPY"
    assert macro.benchmark_trend["daily"] == "up"
    assert macro.vix is None  # no FRED data stored


async def test_a_news_buy_signal_joins_the_scan(sessions: Sessions, settings: Settings) -> None:
    """No stock passes the pullback rules (a straight uptrend has no pullback), but CCC has a
    buy signal from the News agent: it is analysed with the basic rules and its catalyst."""
    script = Agents(block=set())
    ctx, _ = await _context(sessions, settings, script)
    async with sessions() as s:
        ids = {i.yahoo_symbol: k for k, i in (await repo.active_instruments(s)).items()}
    signal = Step(
        day=date(2026, 10, 5),
        market="US",
        instrument_id=ids["CCC"],
        symbol="CCC",
        step="news",
        status="passed",
        summary="buy, confidence 0.80: Raised outlook.",
        input={"headlines": ["CCC raises its outlook"]},
        output={"action": "buy", "confidence": 0.8, "reason": "Raised outlook."},
    )
    async with sessions.begin() as s:
        await steps_repo.add_steps(s, [signal])

    run = await orchestrator.scan(ctx, "US", today=date(2026, 10, 6), now=NOW)
    [p] = run.proposals
    assert (p.yahoo_symbol, p.strategy, p.status) == ("CCC", "news", "proposed")
    async with sessions() as s:
        steps = await steps_repo.steps(s, day=date(2026, 10, 6))
    screened = {x.symbol: x for x in steps if x.step == "screen"}
    assert screened["CCC"].summary == "news candidate 1: price, liquidity and report date ok"
    assert screened["AAA"].status == "stopped"
    async with sessions() as s:
        analysis = await s.get(AnalysisRow, p.analyses["proposer"])
    assert analysis is not None
    assert analysis.input["candidate_source"] == "news"
    assert "CCC raises its outlook" in analysis.input["catalyst"]
