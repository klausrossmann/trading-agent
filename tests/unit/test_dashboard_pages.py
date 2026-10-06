"""Every dashboard page renders, with data and with an empty database (queries stubbed)."""

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest
import yaml
from streamlit.testing.v1 import AppTest

from trading_agent.dashboard import frames, pages
from trading_agent.domain.trading import Trade

PAGES = [
    "overview",
    "positions",
    "proposals",
    "journal",
    "analyses",
    "costs",
    "risk",
    "evaluation",
    "reports",
]
ROOT = Path(__file__).resolve().parents[2]

OPEN = Trade(
    book="baseline_sim",
    strategy="pullback_uptrend",
    instrument_id=1,
    yahoo_symbol="AAA",
    market="US",
    sector=None,
    signal_date=date(2026, 10, 5),
    entry_date=date(2026, 10, 6),
    entry_price=100.0,
    quantity=3,
    stop=95.0,
    target=110.0,
    risk_eur=13.0,
    fees=2.0,
    fees_eur=1.8,
)
CLOSED = OPEN.model_copy(
    update={
        "exit_date": date(2026, 10, 8),
        "exit_price": 110.0,
        "exit_reason": "target",
        "pnl_net_eur": 22.0,
        "r_multiple": 1.7,
        "holding_sessions": 2,
    }
)


def _data(empty: bool) -> dict[str, object]:
    trades = [] if empty else [OPEN, CLOSED]
    analyses = pd.DataFrame(
        [
            {
                "id": 7,
                "created_at": datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
                "symbol": "AAA",
                "module": "technical",
                "as_of": date(2026, 10, 2),
                "status": "rejected",
                "verdict": "buy",
                "score": 0.6,
                "model": "google:gemini-3.8-flash",
                "prompt_version": 1,
                "cost_usd": 0.0012,
                "issues": [
                    {"code": "risk_reward", "message": "R:R 1.5 < 2.0", "severity": "error"}
                ],
                "output": {"rating": "buy"},
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            }
        ]
    )
    calls = [
        {
            "attempt": 1,
            "kind": "initial",
            "model": "google:gemini-3.8-flash",
            "tokens_in": 900,
            "tokens_out": 120,
            "requests": 1,
            "latency_ms": 1500,
            "cost_usd": 0.0011,
            "error": None,
            "finish_reason": "stop",
            "messages": [
                {
                    "kind": "request",
                    "instructions": "Rate the setup.",
                    "parts": [{"part_kind": "user-prompt", "content": 'Input (JSON):\n{"x":1}'}],
                },
                {
                    "kind": "response",
                    "parts": [
                        {"part_kind": "tool-call", "tool_name": "final_result", "args": {"a": 1}}
                    ],
                },
            ],
        },
        {
            "attempt": None,
            "kind": None,
            "model": "google:gemini-3.8-flash",
            "tokens_in": 900,
            "tokens_out": 120,
            "requests": 1,
            "latency_ms": 1500,
            "cost_usd": 0.0011,
            "error": None,
            "finish_reason": None,
            "messages": None,
        },
    ]
    costs = pd.DataFrame(
        [
            {
                "day": date(2026, 10, 5),
                "role": "analysis",
                "model": "google:gemini-3.8-flash",
                "module": "technical",
                "calls": 2,
                "tokens_in": 1000,
                "tokens_out": 300,
                "cost_usd": 0.0024,
                "errors": 0,
            }
        ]
    )
    status = pd.DataFrame(
        [{"dataset": "Bars US", "last_date": date(2026, 10, 2), "detail": "101 symbols, 0 behind"}]
    )
    proposals = pd.DataFrame(
        [
            {
                "as_of": date(2026, 10, 2),
                "symbol": "AAA",
                "market": "US",
                "status": "proposed",
                "rank": 1,
                "confidence": 0.35,
                "entry": 100.0,
                "stop": 96.0,
                "target": 108.0,
                "risk_reward": 2.0,
                "critic": "minor",
                "label": "agree",
                "reason": None,
                "thesis": "[click](https://example.com) pullback",
                "invalidation": "Below the stop.",
                "critic_summary": "Fine.",
                "payload": {
                    "critic": {"objections": [{"severity": "minor", "point": "Thin."}]},
                    "portfolio_manager": {"note": "Only one.", "rationale": "r"},
                },
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            }
        ]
    )
    shadow = CLOSED.model_copy(update={"book": "agent_shadow", "signal_date": date(2026, 10, 2)})
    equity = pd.DataFrame(
        [
            {
                "book": "agent_paper",
                "sleeve": "US",
                "date": date(2026, 10, 5),
                "equity_eur": 1000.0,
            },
            {"book": "agent_paper", "sleeve": "US", "date": date(2026, 10, 6), "equity_eur": 990.0},
        ]
    )
    brackets = pd.DataFrame(
        [
            {
                "book": "agent_paper",
                "instrument_id": i,
                "symbol": s,
                "market": "US",
                "currency": "USD",
                "sector": "Tech",
                "state": "filled",
                "open_qty": 3,
                "pending_qty": 0,
                "entry": 100.0,
                "entry_price": 100.0,
            }
            for i, s in ((1, "AAA"), (2, "BBB"))
        ]
    )
    days = pd.date_range("2026-07-01", periods=70).date
    closes = pd.DataFrame(
        {1: [100 + i % 5 for i in range(70)], 2: [50 + i % 3 for i in range(70)]}, index=days
    )
    rejected = pd.DataFrame(
        [{"as_of": date(2026, 10, 2), "symbol": "CCC", "check": "portfolio", "detail": "4 open"}]
    )
    config = {
        n: yaml.safe_load((ROOT / "config" / n).read_text())
        for n in ("risk.yaml", "strategies.yaml")
    }
    return {
        "_trades": [] if empty else [*trades, shadow],
        "_positions": frames.open_positions(trades, {1: (date(2026, 10, 7), 104.0)}, 1.17),
        "_data_status": status.iloc[0:0] if empty else status,
        "_analyses": analyses.iloc[0:0] if empty else analyses,
        "_proposals": proposals.iloc[0:0] if empty else proposals,
        "_llm_costs": costs.iloc[0:0] if empty else costs,
        "_equity": equity.iloc[0:0] if empty else equity,
        "_brackets": brackets.iloc[0:0] if empty else brackets,
        "_kill_switch": "active",
        "_usd_per_eur": None if empty else 1.1,
        "_rejections": rejected.iloc[0:0] if empty else rejected,
        "_closes": closes.iloc[0:0] if empty else closes,
        "_reports": [] if empty else [(date(2026, 10, 10), "# Weekly report")],
        "_config": config,
        "_llm_calls": [] if empty else calls,
        "_settings": SimpleNamespace(trace_ui_url="https://cloud.langfuse.com/project/p1"),
    }


@pytest.fixture(params=[False, True], ids=["data", "empty"])
def stubbed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _data(request.param).items():
        if name == "_config":
            config = cast(dict[str, Any], value)
            monkeypatch.setattr(pages, name, lambda n, config=config: config[n])
        elif name in ("_closes", "_llm_calls"):
            monkeypatch.setattr(pages, name, lambda _, value=value: value)
        else:
            monkeypatch.setattr(pages, name, lambda value=value: value)


def _page_script(name: str) -> None:
    from trading_agent.dashboard import pages

    getattr(pages, name)()


@pytest.mark.usefixtures("stubbed")
@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page: str) -> None:
    at = AppTest.from_function(_page_script, args=(page,)).run()
    assert not at.exception, at.exception


def test_analysis_details_show_the_calls_and_the_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _data(empty=False).items():
        if name in ("_closes", "_llm_calls"):
            monkeypatch.setattr(pages, name, lambda _, value=value: value)
        elif name != "_config":
            monkeypatch.setattr(pages, name, lambda value=value: value)
    at = AppTest.from_function(_page_script, args=("analyses",)).run()
    assert not at.exception, at.exception
    (first, second) = at.expander
    assert first.label == "Attempt 1 (initial)"
    text = first.text[0].value
    assert "[instructions]\nRate the setup." in text
    assert "[model → final_result]" in text
    assert "No messages stored" in second.text[0].value
    (link,) = at.get("link_button")
    assert link.proto.url == (
        "https://cloud.langfuse.com/project/p1/traces/4bf92f3577b34da6a3ce929d0e0e4736"
    )


def _main_script() -> None:
    from trading_agent.dashboard import pages

    pages.main()


@pytest.mark.usefixtures("stubbed")
def test_navigation_opens_overview() -> None:
    at = AppTest.from_function(_main_script).run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Overview"
