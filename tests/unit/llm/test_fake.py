import pytest

from trading_agent.llm.fake import answer

LEVELS = [
    {"name": "close", "price": 100.0, "kind": "price"},
    {"name": "swing_low", "price": 96.0, "kind": "support"},
    {"name": "far_low", "price": 80.0, "kind": "support"},
    {"name": "r1", "price": 104.0, "kind": "resistance"},
    {"name": "r2", "price": 109.0, "kind": "resistance"},
]
RULES = {"min_risk_reward": 2.0, "stop_atr_min": 1.0, "stop_atr_max": 4.0}


def test_plan_follows_the_rules() -> None:
    inp = {"close": 100.0, "levels": LEVELS, "rules": RULES, "indicators": {"atr14": 2.0}}
    out = answer(inp, {"rating"})
    assert (out["rating"], out["entry_ref"], out["stop_ref"], out["target_ref"]) == (
        "buy",
        "close",
        "swing_low",
        "r2",
    )
    flat = answer({**inp, "indicators": {"atr14": 0.5}}, {"rating"})  # no stop within 4 ATR
    assert (flat["rating"], flat["stop_ref"]) == ("neutral", None)


def test_proposer_waits_for_earnings() -> None:
    inp = {"symbol": "AAA", "close": 100.0, "levels": LEVELS, "rules": RULES, "atr14": 2.0}
    assert answer(inp, {"decision"})["decision"] == "propose"
    wait = answer({**inp, "earnings": {"stance": "wait_until_after"}}, {"decision"})
    assert (wait["decision"], wait["entry_ref"]) == ("no_trade", None)
    assert answer({**inp, "unknown": ["earnings"]}, {"decision"})["data_gaps"] == ["earnings"]


def test_other_schemas() -> None:
    assert answer({"event_in_window": True}, {"stance"})["stance"] == "wait_until_after"
    assert answer({}, {"objections"})["severity"] == "minor"
    ranked = answer({"candidates": [{"symbol": "B"}, {"symbol": "A"}]}, {"ranking"})
    assert [r["symbol"] for r in ranked["ranking"]] == ["B", "A"]
    assert answer({"items": [{"ref": "n1"}]}, {"items"})["items"][0]["ref"] == "n1"
    assert answer({}, {"verdict"})["verdict"] == "hold"
    with pytest.raises(ValueError, match="unknown output schema"):
        answer({}, {"something"})
