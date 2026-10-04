"""Every dashboard page renders, with data and with an empty database (queries stubbed)."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from trading_agent.dashboard import frames, pages
from trading_agent.domain.trading import Trade

PAGES = ["overview", "positions", "journal", "analyses", "costs"]

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
            }
        ]
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
    return {
        "_trades": trades,
        "_positions": frames.open_positions(trades, {1: (date(2026, 10, 7), 104.0)}, 1.17),
        "_data_status": status.iloc[0:0] if empty else status,
        "_analyses": analyses.iloc[0:0] if empty else analyses,
        "_llm_costs": costs.iloc[0:0] if empty else costs,
    }


@pytest.fixture(params=[False, True], ids=["data", "empty"])
def stubbed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _data(request.param).items():
        monkeypatch.setattr(pages, name, lambda value=value: value)


def _page_script(name: str) -> None:
    from trading_agent.dashboard import pages

    getattr(pages, name)()


@pytest.mark.usefixtures("stubbed")
@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page: str) -> None:
    at = AppTest.from_function(_page_script, args=(page,)).run()
    assert not at.exception, at.exception


def _main_script() -> None:
    from trading_agent.dashboard import pages

    pages.main()


@pytest.mark.usefixtures("stubbed")
def test_navigation_opens_overview() -> None:
    at = AppTest.from_function(_main_script).run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Overview"
