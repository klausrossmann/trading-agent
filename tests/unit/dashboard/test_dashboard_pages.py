"""Every dashboard page renders, with data and with an empty database (queries stubbed)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest
import yaml
from streamlit.testing.v1 import AppTest

from trading_agent.dashboard import pages, queries
from trading_agent.portfolio.types import Trade

PAGES = ["day", "portfolio", "costs", "risk"]
ROOT = Path(__file__).resolve().parents[3]
DAY = date(2026, 10, 7)
T0 = datetime(2026, 10, 7, 6, 15, tzinfo=UTC)
PID = "9b0c6f0e-0000-4000-8000-000000000001"

OPEN = Trade(
    book="agent_paper",
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


def _step(symbol: str, name: str, status: str, summary: str, minute: int, **kw: Any) -> dict:
    return {
        "id": minute,
        "ts": T0 + timedelta(minutes=minute),
        "day": DAY,
        "market": "US",
        "symbol": symbol,
        "step": name,
        "status": status,
        "summary": summary,
        "input": kw.get("input"),
        "output": kw.get("output"),
        "error": kw.get("error"),
        "analysis_id": kw.get("analysis_id"),
        "proposal_id": kw.get("proposal_id"),
        "order_ref": kw.get("order_ref"),
    }


STEPS = pd.DataFrame(
    [
        _step("AAA", "screen", "passed", "rank 1 by relative strength", 0, input={"rsi": 47.0}),
        _step("AAA", "technical", "passed", "rating buy", 1, analysis_id=7, proposal_id=PID),
        _step("AAA", "risk", "passed", "approved 3 shares", 60, output={"checks": []}),
        _step(
            "AAA",
            "order",
            "error",
            "sent to IBKR: entry BUY 3 LMT 100",
            61,
            error="TA-AAA-entry-1a2b3c4d: 10243: Fractional-sized order cannot be placed",
            order_ref="TA-AAA-entry-1a2b3c4d",
            proposal_id=PID,
        ),
        _step("BBB", "screen", "stopped", "RSI 61.0, needs 40-55", 0),
    ],
    columns=queries.STEP_COLUMNS,
)
ANALYSIS = {
    "module": "technical",
    "prompt_version": 1,
    "model": "google:gemini-3.8-flash",
    "status": "ok",
    "cost_usd": 0.0012,
    "input": {"symbol": "AAA"},
    "output": {"rating": "buy"},
    "issues": [{"code": "risk_reward", "message": "R:R 1.5 < 2.0", "severity": "warning"}],
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
}
CALLS = [
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
    }
]


def _position(**kw: Any) -> dict[str, Any]:
    return {
        "id": PID,
        "book": "agent_paper",
        "symbol": "AAA",
        "market": "US",
        "currency": "USD",
        "state": "filled",
        "strategy": "pullback_uptrend",
        "quantity": 3.0,
        "open_qty": 3.0,
        "pending_qty": 0.0,
        "limit": 100.0,
        "entry_price": 100.0,
        "initial_stop": 96.0,
        "stop": 96.0,
        "target": 108.0,
        "expires": T0,
        "entered": T0,
        "last_date": DAY,
        "last_close": 104.0,
    } | kw


def _data(empty: bool) -> dict[str, object]:
    trades = [] if empty else [OPEN, CLOSED]
    positions = pd.DataFrame(
        [
            _position(),
            _position(id="x", symbol="BBB", state="working", open_qty=0.0, pending_qty=3.0),
        ],
        columns=queries.POSITION_COLUMNS,
    )
    sold = pd.DataFrame(
        [
            {
                "id": PID,
                "book": "agent_paper",
                "symbol": "CCC",
                "market": "US",
                "strategy": "pullback_uptrend",
                "entry_date": date(2026, 10, 1),
                "entry_price": 100.0,
                "quantity": 3.0,
                "initial_stop": 96.0,
                "target": 108.0,
                "exit_date": date(2026, 10, 6),
                "exit_price": 100.1,
                "exit_reason": "stop",
                "holding_sessions": 3,
                "r_multiple": 0.02,
                "pnl_net_eur": -1.5,
                "fees_eur": 1.8,
            }
        ],
        columns=queries.SOLD_COLUMNS,
    )
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
    equity = pd.DataFrame(
        [
            {"book": "agent_paper", "sleeve": "US,EU", "date": DAY, "equity_eur": 15000.0},
            {"book": "agent_paper", "sleeve": "US,EU", "date": DAY, "equity_eur": 14990.0},
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
        "_today": DAY,
        "_trades": trades,
        "_data_status": status.iloc[0:0] if empty else status,
        "_step_days": [] if empty else [DAY],
        "_steps_on": STEPS.iloc[0:0] if empty else STEPS,
        "_trade_steps": STEPS.iloc[0:0] if empty else STEPS[STEPS["symbol"] == "AAA"],
        "_analysis": None if empty else ANALYSIS,
        "_llm_calls": [] if empty else CALLS,
        "_positions": positions.iloc[0:0] if empty else positions,
        "_sold": sold.iloc[0:0] if empty else sold,
        "_llm_costs": costs.iloc[0:0] if empty else costs,
        "_equity": equity.iloc[0:0] if empty else equity,
        "_brackets": brackets.iloc[0:0] if empty else brackets,
        "_kill_switch": "active",
        "_usd_per_eur": None if empty else 1.1,
        "_rejections": rejected.iloc[0:0] if empty else rejected,
        "_closes": closes.iloc[0:0] if empty else closes,
        "_config": config,
        "_settings": SimpleNamespace(trace_ui_url="https://cloud.langfuse.com/project/p1"),
    }


TAKES_ARGUMENT = ("_steps_on", "_trade_steps", "_analysis", "_llm_calls", "_closes", "_config")


def _stub(monkeypatch: pytest.MonkeyPatch, empty: bool) -> None:
    for name, value in _data(empty).items():
        if name == "_config":
            config = cast(dict[str, Any], value)
            monkeypatch.setattr(pages, name, lambda n, config=config: config[n])
        elif name in TAKES_ARGUMENT:
            monkeypatch.setattr(pages, name, lambda _, value=value: value)
        else:
            monkeypatch.setattr(pages, name, lambda value=value: value)


@pytest.fixture(params=[False, True], ids=["data", "empty"])
def stubbed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, request.param)


def _page_script(name: str) -> None:
    from trading_agent.dashboard import pages

    getattr(pages, name)()


@pytest.mark.usefixtures("stubbed")
@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page: str) -> None:
    at = AppTest.from_function(_page_script, args=(page,)).run()
    assert not at.exception, at.exception


def test_day_shows_every_step_of_the_picked_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, empty=False)
    at = AppTest.from_function(_page_script, args=("day",)).run()
    assert not at.exception, at.exception
    table = at.dataframe[1].value  # after the data freshness table
    assert list(table["symbol"]) == ["AAA", "BBB"]
    assert table.loc[table["symbol"] == "BBB", "screen"].iloc[0] == "✗ RSI 61.0, needs 40-55"
    labels = [e.label for e in at.expander]
    assert any("order: sent to IBKR" in label for label in labels)
    texts = [t.value for t in at.text]
    assert "IBKR order reference: TA-AAA-entry-1a2b3c4d" in texts
    assert any("10243: Fractional-sized order cannot be placed" in t for t in texts)
    assert any("[instructions]\nRate the setup." in t for t in texts)
    (link,) = at.get("link_button")
    assert link.proto.url == (
        "https://cloud.langfuse.com/project/p1/traces/4bf92f3577b34da6a3ce929d0e0e4736"
    )

    at.selectbox[2].set_value("BBB").run()  # day, market, show, then the stock
    assert not at.exception, at.exception
    assert [e.label.split(" · ")[1] for e in at.expander[1:]] == ["screen: RSI 61.0, needs 40-55"]


def test_portfolio_says_why_a_stock_was_sold(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, empty=False)
    at = AppTest.from_function(_page_script, args=("portfolio",)).run()
    assert not at.exception, at.exception
    held, waiting, sold = (d.value for d in at.dataframe[:3])
    assert list(held["symbol"]) == ["AAA"]
    assert list(waiting["symbol"]) == ["BBB"]
    assert list(sold["why"]) == ["stop hit after it was raised (breakeven or trailing)"]
    assert at.selectbox[0].options[-1] == (
        "sold 2026-10-06: CCC (stop hit after it was raised (breakeven or trailing))"
    )


def _main_script() -> None:
    from trading_agent.dashboard import pages

    pages.main()


@pytest.mark.usefixtures("stubbed")
def test_navigation_opens_the_day() -> None:
    at = AppTest.from_function(_main_script).run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Day"
