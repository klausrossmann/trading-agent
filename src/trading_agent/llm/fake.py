"""Offline stand-in for the LLM providers (`LLM_FAKE=true`, compose.dev.yaml).

Deterministic answers that follow each prompt's rules, so the whole chain (scan, agents, risk
engine, simulator) runs on a laptop without API keys. Never for real decisions.
"""

import json
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

NAME = "fake"


def _input(messages: list[ModelMessage]) -> dict[str, Any]:
    """The JSON input of the first request (feedback rounds carry no input)."""
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and str(part.content).startswith("Input"):
                    return json.loads(str(part.content).split("\n", 1)[1])
    return {}


def _plan(inp: dict[str, Any]) -> tuple[str, str, str] | None:
    """The nearest level at or below the close as entry, the closest stop within the ATR
    bounds, the nearest target with enough reward to risk; None if the menu has none."""
    rules = inp["rules"]
    atr = inp.get("atr14") or inp.get("indicators", {}).get("atr14")
    levels = sorted(inp["levels"], key=lambda lv: lv["price"])
    below = [lv for lv in levels if lv["price"] <= inp["close"]]
    if not atr or not below:
        return None
    entry = below[-1]
    risk_bounds = (rules["stop_atr_min"] * atr, rules["stop_atr_max"] * atr)
    stops = [
        lv
        for lv in reversed(levels)
        if risk_bounds[0] <= entry["price"] - lv["price"] <= risk_bounds[1]
    ]
    if not stops:
        return None
    risk = entry["price"] - stops[0]["price"]
    targets = [
        lv for lv in levels if lv["price"] - entry["price"] >= rules["min_risk_reward"] * risk
    ]
    if not targets:
        return None
    return entry["name"], stops[0]["name"], targets[0]["name"]


def _gaps(inp: dict[str, Any]) -> list[str]:
    return list(inp.get("unknown") or inp.get("facts", {}).get("unknown") or [])


def _refs(plan: tuple[str, str, str] | None) -> dict[str, str | None]:
    entry, stop, target = plan or (None, None, None)
    return {"entry_ref": entry, "stop_ref": stop, "target_ref": target}


def answer(inp: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    if "rating" in fields:
        plan = _plan(inp)
        refs = _refs(plan)
        return {
            "trend_summary": "Fake: trend as in the input.",
            "momentum_summary": "Fake: momentum as in the input.",
            "volume_summary": "Fake: volume as in the input.",
            "rating": "buy" if plan else "neutral",
            "setup_quality": 0.5 if plan else 0.2,
            "reasoning": "Fake answer from the offline model.",
            "invalidation": f"A close below {refs['stop_ref']}." if plan else "None.",
            "data_gaps": _gaps(inp),
            **refs,
        }
    if "stance" in fields:
        window = bool(inp.get("event_in_window"))
        return {
            "summary": "Fake: report timing as in the input.",
            "track_record": "Fake.",
            "reaction_pattern": "Fake.",
            "event_risk": "medium" if window else "low",
            "stance": "wait_until_after" if window else "no_event_in_window",
            "bull_case": "Fake.",
            "bear_case": "Fake.",
            "confidence": 0.5,
            "data_gaps": _gaps(inp),
        }
    if "decision" in fields:
        plan = _plan(inp)
        wait = (inp.get("earnings") or {}).get("stance") == "wait_until_after"
        go = plan is not None and not wait
        refs = _refs(plan if go else None)
        return {
            "decision": "propose" if go else "no_trade",
            "confidence": 0.4 if go else 0.2,
            "thesis": f"Fake: {inp['symbol']} pulls back in an uptrend.",
            "invalidation": f"A close below {refs['stop_ref']}." if go else "None.",
            "no_trade_reason": None if go else "Fake: no valid plan or an earnings wait.",
            "data_gaps": _gaps(inp),
            **refs,
        }
    if "objections" in fields:
        return {
            "objections": [{"point": "Fake objection.", "severity": "minor"}],
            "severity": "minor",
            "confidence_delta": -0.05,
            "summary": "Fake critique.",
        }
    if "ranking" in fields:
        return {
            "ranking": [{"symbol": c["symbol"], "note": "Fake."} for c in inp["candidates"]],
            "rationale": "Fake: the input order.",
        }
    if "items" in fields:
        return {
            "items": [{"ref": h["ref"], "relevance": "low", "note": "Fake."} for h in inp["items"]]
        }
    if "verdict" in fields:
        return {
            "thesis_intact": True,
            "verdict": "hold",
            "reasons": "Fake: the thesis still holds.",
            "confidence": 0.5,
        }
    raise ValueError(f"fake model: unknown output schema {sorted(fields)}")


def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    tool = info.output_tools[0]
    fields = set(tool.parameters_json_schema["properties"])
    return ModelResponse(parts=[ToolCallPart(tool.name, answer(_input(messages), fields))])


def fake_model() -> FunctionModel:
    return FunctionModel(_respond, model_name=NAME)
