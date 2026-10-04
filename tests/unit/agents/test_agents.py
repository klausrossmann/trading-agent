from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_agent.agents import critic, portfolio, proposer
from trading_agent.agents.critic import (
    CriticAgent,
    CriticInput,
    CriticVerdict,
    Objection,
    PlanView,
    max_severity,
)
from trading_agent.agents.portfolio import (
    Candidate,
    PortfolioInput,
    PortfolioManagerAgent,
    Ranking,
    ranking_type,
)
from trading_agent.agents.proposer import (
    FABRICATION_CAP,
    Holding,
    ProposerAgent,
    ProposerInput,
    ProposerOutput,
    proposer_output_type,
)
from trading_agent.llm.prompts import load_prompt
from trading_agent.modules.earnings import EarningsAssessment
from trading_agent.modules.technical import Level, PlanRules, TechnicalAssessment

ROOT = Path(__file__).resolve().parents[3]
LEVELS = [
    Level(name="close", price=100.0, kind="price"),
    Level(name="ema20", price=99.0, kind="ma"),
    Level(name="atr_stop_2x", price=96.0, kind="atr_stop"),
    Level(name="atr_target_4x", price=108.0, kind="atr_target"),
    Level(name="resistance_1", price=103.0, kind="resistance"),
]
TECH = TechnicalAssessment(
    trend_summary="Up on daily and weekly.",
    momentum_summary="RSI neutral.",
    volume_summary="Light selling.",
    rating="buy",
    setup_quality=0.6,
    entry_ref="close",
    stop_ref="atr_stop_2x",
    target_ref="atr_target_4x",
    reasoning="Pullback to ema20 in an uptrend.",
    invalidation="Close below atr_stop_2x.",
    data_gaps=[],
)
EARN = EarningsAssessment(
    summary="No report in the window.",
    track_record="Mostly beats.",
    reaction_pattern="Small moves.",
    event_risk="none",
    stance="no_event_in_window",
    bull_case="b",
    bear_case="b",
    confidence=0.7,
    data_gaps=[],
)


def _input(**kw: object) -> ProposerInput:
    values: dict[str, object] = {
        "symbol": "AAA",
        "name": "Triple A",
        "market": "US",
        "currency": "USD",
        "sector": "Industrials",
        "as_of": date(2026, 10, 2),
        "close": 100.0,
        "candidate_source": "pullback_uptrend",
        "atr14": 2.0,
        "levels": LEVELS,
        "rules": PlanRules(min_risk_reward=2.0),
        "technical": TECH,
        "earnings": EARN,
        "earnings_event_in_window": False,
        "next_earnings_date": None,
        "holdings": [Holding(symbol="BBB", market="US", sector="Energy")],
        "unknown": [],
    }
    return ProposerInput.model_validate(values | kw)


def _proposal(**kw: object) -> ProposerOutput:
    values: dict[str, object] = {
        "decision": "propose",
        "entry_ref": "close",
        "stop_ref": "atr_stop_2x",
        "target_ref": "atr_target_4x",
        "confidence": 0.4,
        "thesis": "Pullback to ema20 in an uptrend with light selling.",
        "invalidation": "A close below atr_stop_2x.",
        "no_trade_reason": None,
        "data_gaps": [],
    }
    return ProposerOutput.model_validate(values | kw)


@pytest.fixture
def agent() -> ProposerAgent:
    return ProposerAgent(load_prompt(ROOT / "prompts", proposer.NAME, proposer.PROMPT_VERSION))


def test_prompts_load() -> None:
    prompts = ROOT / "prompts"
    assert load_prompt(prompts, critic.NAME, critic.PROMPT_VERSION).role == "critic"
    assert load_prompt(prompts, portfolio.NAME, portfolio.PROMPT_VERSION).role == "proposer"


def test_valid_proposal_passes(agent: ProposerAgent) -> None:
    assert agent.validate(_input(), _proposal()) == []


def test_output_type_only_accepts_menu_levels(agent: ProposerAgent) -> None:
    out_type = agent.output_type(_input())
    assert out_type is proposer_output_type(tuple(lv.name for lv in LEVELS))
    with pytest.raises(ValidationError):
        out_type.model_validate(_proposal(entry_ref="sma50").model_dump())


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"target_ref": "resistance_1"}, "risk_reward"),
        ({"stop_ref": None}, "missing_plan"),
        ({"stop_ref": "ema20", "target_ref": "resistance_1"}, "stop_distance"),
    ],
)
def test_plan_errors(agent: ProposerAgent, change: dict[str, object], code: str) -> None:
    issues = agent.validate(_input(), _proposal(**change))
    assert code in {i.code for i in issues if i.severity == "error"}


def test_wait_until_after_forbids_a_proposal(agent: ProposerAgent) -> None:
    earn = EARN.model_copy(update={"stance": "wait_until_after", "event_risk": "high"})
    issues = agent.validate(_input(earnings=earn), _proposal())
    assert [i.code for i in issues] == ["earnings_stance"]


def test_no_trade_needs_a_reason(agent: ProposerAgent) -> None:
    passed = _proposal(decision="no_trade", entry_ref=None, stop_ref=None, target_ref=None)
    assert [i.code for i in agent.validate(_input(), passed)] == ["missing_reason"]
    ok = passed.model_copy(update={"no_trade_reason": "Momentum is fading."})
    assert agent.validate(_input(), ok) == []


def test_unknown_inputs_need_data_gaps_and_numbers_are_grounded(agent: ProposerAgent) -> None:
    out = _proposal(thesis="Target 123.45 is in reach.", confidence=0.8)
    issues = agent.validate(_input(unknown=["earnings"]), out)
    assert {i.code for i in issues} == {"missing_data_gaps", "ungrounded_number"}
    assert agent.finalize(_input(), out, issues).confidence == FABRICATION_CAP
    assert agent.finalize(_input(), out, []).confidence == 0.8


def _plan() -> PlanView:
    return PlanView(
        entry_ref="close",
        entry=100.0,
        stop_ref="atr_stop_2x",
        stop=96.0,
        target_ref="atr_target_4x",
        target=108.0,
        risk_reward=2.0,
        stop_atr_multiple=2.0,
        confidence=0.4,
        thesis="t",
        invalidation="i",
    )


def test_critic_severity_must_match_objections() -> None:
    agent = CriticAgent(load_prompt(ROOT / "prompts", critic.NAME, critic.PROMPT_VERSION))
    inp = CriticInput(facts=_input(), proposal=_plan())
    objections = [
        Objection(point="Resistance_1 sits below the target.", severity="major"),
        Objection(point="Light volume.", severity="minor"),
    ]
    verdict = CriticVerdict(
        objections=objections, severity="minor", confidence_delta=-0.1, summary="Weak target."
    )
    assert [i.code for i in agent.validate(inp, verdict)] == ["severity"]
    fixed = verdict.model_copy(update={"severity": "major"})
    assert agent.validate(inp, fixed) == []
    assert max_severity([]) == "none"
    with pytest.raises(ValidationError):
        CriticVerdict(objections=[], severity="none", confidence_delta=0.3, summary="s")


def test_portfolio_ranking_covers_every_candidate_once() -> None:
    agent = PortfolioManagerAgent(
        load_prompt(ROOT / "prompts", portfolio.NAME, portfolio.PROMPT_VERSION)
    )
    candidates = [
        Candidate(
            symbol=s,
            market="US",
            sector=None,
            confidence=0.4,
            risk_reward=2.0,
            thesis="t",
            critic_severity="none",
            critic_summary="ok",
        )
        for s in ("AAA", "CCC")
    ]
    inp = PortfolioInput(
        as_of=date(2026, 10, 2),
        candidates=candidates,
        holdings=[],
        free_slots=4,
        budget_mode="normal",
    )
    out_type = agent.output_type(inp)
    assert out_type is ranking_type(("AAA", "CCC"))
    with pytest.raises(ValidationError):
        out_type.model_validate({"ranking": [{"symbol": "ZZZ", "note": "n"}], "rationale": "r"})
    bad = Ranking.model_validate(
        {"ranking": [{"symbol": "AAA", "note": "n"}] * 2, "rationale": "r"}
    )
    (issue,) = agent.validate(inp, bad)
    assert issue.message.endswith("missing: CCC, repeated: AAA")
    good = Ranking.model_validate(
        {"ranking": [{"symbol": s, "note": "n"} for s in ("CCC", "AAA")], "rationale": "r"}
    )
    assert agent.validate(inp, good) == []
