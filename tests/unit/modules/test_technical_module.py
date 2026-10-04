from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from trading_agent.domain.market import Instrument
from trading_agent.llm.models import ModelsConfig
from trading_agent.llm.prompts import load_prompt
from trading_agent.llm.runner import LlmRunner
from trading_agent.llm.store import MemoryStore
from trading_agent.modules.technical import (
    FABRICATION_CAP,
    PlanRules,
    TechnicalAssessment,
    TechnicalInput,
    TechnicalModule,
    compute,
    technical_output_type,
)

ROOT = Path(__file__).resolve().parents[3]
RULES = PlanRules(min_risk_reward=2.0, stop_atr_min=1.0, stop_atr_max=4.0)
INST = Instrument(
    symbol="AAA",
    yahoo_symbol="AAA",
    name="Triple A Inc.",
    market="US",
    exchange="SMART",
    currency="USD",
    kind="stock",
    sector="Industrials",
    indices=("SP100",),
)


def _frame(n: int, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 50 + 0.1 * t + 3 * np.sin(t / 20 * 2 * np.pi) + rng.normal(0, 0.3, n)
    spread = 0.5 + rng.random(n)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": rng.integers(1_000_000, 2_000_000, n).astype(float),
        },
        index=pd.bdate_range("2025-01-01", periods=n),
    )


@pytest.fixture
def module() -> TechnicalModule:
    return TechnicalModule(load_prompt(ROOT / "prompts", "technical", 1))


@pytest.fixture
def inp() -> TechnicalInput:
    frame = _frame(320)
    return compute(INST, frame, frame["close"] * 0.5, RULES)


def _assessment(inp: TechnicalInput, **fields: object) -> TechnicalAssessment:
    base: dict[str, object] = {
        "trend_summary": "Daily and weekly trend up.",
        "momentum_summary": "RSI neutral.",
        "volume_summary": "Balanced.",
        "rating": "neutral",
        "setup_quality": 0.6,
        "entry_ref": None,
        "stop_ref": None,
        "target_ref": None,
        "reasoning": "Holding above sma50.",
        "invalidation": "A close below support_1.",
        "data_gaps": [],
    }
    out_type = technical_output_type(tuple(lv.name for lv in inp.levels))
    return out_type.model_validate(base | fields)


def test_compute_long_history_has_no_unknowns(inp: TechnicalInput) -> None:
    assert inp.unknown == []
    assert inp.symbol == "AAA"
    assert inp.currency == "USD"
    assert inp.close == round(inp.close, 2)
    assert set(inp.change_pct) == {"1d", "5d", "20d", "63d"}
    assert set(inp.trend) == {"daily", "weekly"}
    assert 0 < (inp.indicators["rsi14"] or 0) < 100
    assert inp.indicators["rs_vs_benchmark_63d_pct"] == pytest.approx(0, abs=0.01)
    names = {lv.name for lv in inp.levels}
    assert {"close", "ema20", "sma200", "atr_stop_2x", "atr_target_4x"} <= names


def test_compute_short_history_lists_unknowns() -> None:
    frame = _frame(40)
    inp = compute(INST, frame, None, RULES)
    assert "indicators.rs_vs_benchmark_63d_pct" in inp.unknown
    assert "indicators.close_vs_sma200_pct" in inp.unknown
    assert "change_pct.63d" in inp.unknown
    assert "trend.daily" in inp.unknown
    assert not {lv.name for lv in inp.levels} & {"sma50", "sma200"}


def test_refs_are_restricted_to_the_level_menu(inp: TechnicalInput) -> None:
    with pytest.raises(ValidationError):
        _assessment(inp, rating="buy", entry_ref="close", stop_ref="made_up", target_ref="close")
    out = _assessment(inp, rating="buy", entry_ref="close", stop_ref="sma50", target_ref="close")
    assert out.stop_ref == "sma50"


def _valid_plan(inp: TechnicalInput) -> tuple[str, str, str]:
    price = {lv.name: lv.price for lv in inp.levels}
    atr = inp.indicators["atr14"] or 0
    for stop in ("atr_stop_1_5x", "atr_stop_2x", "atr_stop_3x"):
        risk = price["close"] - price[stop]
        for target in ("atr_target_3x", "atr_target_4x", "atr_target_6x"):
            if price[target] - price["close"] >= 2 * risk and 1 <= risk / atr <= 4:
                return "close", stop, target
    raise AssertionError("no valid plan in the menu")


def test_validate_buy_plan(module: TechnicalModule, inp: TechnicalInput) -> None:
    entry, stop, target = _valid_plan(inp)
    good = _assessment(inp, rating="buy", entry_ref=entry, stop_ref=stop, target_ref=target)
    assert module.validate(inp, good) == []

    inverted = _assessment(inp, rating="buy", entry_ref=entry, stop_ref=target, target_ref=stop)
    assert [i.code for i in module.validate(inp, inverted)] == ["level_order"]
    missing = _assessment(inp, rating="strong_buy", entry_ref=entry, stop_ref=stop)
    assert [i.code for i in module.validate(inp, missing)] == ["missing_plan"]
    # Neutral ratings need no plan.
    assert module.validate(inp, _assessment(inp)) == []


def test_validate_data_gaps_and_fabrication(module: TechnicalModule) -> None:
    frame = _frame(40)
    short = compute(INST, frame, None, RULES)
    out = _assessment(short)
    assert [i.code for i in module.validate(short, out)] == ["missing_data_gaps"]
    out = _assessment(short, data_gaps=["no benchmark"], reasoning="Upside to 999.99.")
    issues = module.validate(short, out)
    assert [i.code for i in issues] == ["ungrounded_number"]
    assert module.finalize(short, out, issues).setup_quality == FABRICATION_CAP


async def test_runs_through_pydantic_ai_with_the_dynamic_schema(
    module: TechnicalModule, inp: TechnicalInput
) -> None:
    cfg = ModelsConfig.model_validate(
        {
            "roles": {"analysis": "google:m"},
            "prices": {"google:m": [{"since": "2026-01-01", "input": 1, "output": 1}]},
            "budget": {"monthly_usd": 10, "lean_mode_at": 0.8, "hard_stop_at": 1.0},
            "scan": {"candidates": 10, "candidates_lean": 5},
        }
    )
    store = MemoryStore()
    runner = LlmRunner(
        cfg, store, lambda _: TestModel(), clock=lambda: datetime(2026, 10, 4, tzinfo=UTC)
    )
    out = await runner.run(module, inp, instrument_id=1, as_of=inp.as_of)
    assert out.output is not None
    names = {lv.name for lv in inp.levels}
    for ref in (out.output.entry_ref, out.output.stop_ref, out.output.target_ref):
        assert ref is None or ref in names
    assert store.analyses[1].input["symbol"] == "AAA"
