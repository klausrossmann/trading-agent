from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_agent.domain.market import EarningsEvent, EarningsTiming, Instrument
from trading_agent.llm.prompts import load_prompt
from trading_agent.modules.earnings import (
    EarningsAssessment,
    EarningsInput,
    EarningsModule,
    compute,
)

ROOT = Path(__file__).resolve().parents[3]
INST = Instrument(
    symbol="AAA",
    yahoo_symbol="AAA",
    name="Triple A Inc.",
    market="US",
    exchange="SMART",
    currency="USD",
    kind="stock",
    indices=("SP100",),
)
LAST = date(2026, 10, 2)  # a Friday


def _frame() -> pd.DataFrame:
    index = pd.bdate_range(end=pd.Timestamp(LAST), periods=300)
    close = 100 + np.arange(300) * 0.1
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6},
        index=index,
    )


def _event(
    day: date, timing: EarningsTiming = "amc", est: str | None = "1.00", act: str | None = None
) -> EarningsEvent:
    return EarningsEvent(
        date=day,
        ts=None,
        timing=timing,
        eps_estimate=Decimal(est) if est else None,
        eps_actual=Decimal(act) if act else None,
    )


def _past() -> list[EarningsEvent]:
    return [
        _event(date(2026, 7, 30), act="1.10"),
        _event(date(2026, 4, 30), "bmo", act="0.95"),
        _event(date(2026, 1, 29), act="1.00"),
    ]


def test_history_and_stats() -> None:
    frame = _frame()
    frame.loc[pd.Timestamp("2026-07-31"), ["open", "close"]] *= [1.05, 1.08]  # amc: next day
    inp = compute(INST, frame, [*_past(), _event(date(2026, 11, 30))], holding_sessions=17)
    assert [h.date for h in inp.history] == [
        date(2026, 7, 30),
        date(2026, 4, 30),
        date(2026, 1, 29),
    ]
    latest = inp.history[0]
    assert latest.surprise_pct == 10.0
    assert latest.gap_pct is not None
    assert latest.gap_pct > 4.9
    assert latest.move_pct is not None
    assert latest.move_pct > 7.9
    assert inp.stats["beats"] == 1
    assert inp.stats["misses"] == 1
    assert inp.stats["reports_with_estimate"] == 3
    assert inp.next_report is not None
    assert inp.next_report.date == date(2026, 11, 30)
    assert not inp.event_in_window
    assert inp.unknown == []


@pytest.mark.parametrize(
    ("timing", "upcoming", "until", "in_window"),
    [
        ("amc", True, 0, True),  # after today's close: reacts on the next session
        ("unknown", True, 0, True),
        ("bmo", False, None, False),  # already in today's bar
    ],
)
def test_report_on_the_last_bar_day(
    timing: EarningsTiming, upcoming: bool, until: int | None, in_window: bool
) -> None:
    inp = compute(INST, _frame(), [_event(LAST, timing, act="1.2")], holding_sessions=17)
    assert (inp.next_report is not None) == upcoming
    if inp.next_report is not None:
        assert inp.next_report.sessions_until == until
    else:
        assert inp.history[0].date == LAST
    assert inp.event_in_window == in_window


def test_holding_window_edge_and_no_look_ahead() -> None:
    # 2026-10-05 .. 2026-10-27 holds 17 NYSE sessions; a bmo report on the 17th is in the window.
    bmo = compute(INST, _frame(), [_event(date(2026, 10, 27), "bmo", act="123.45")], 17)
    assert bmo.next_report is not None
    assert bmo.next_report.sessions_until == 17
    assert bmo.event_in_window
    assert "123.45" not in bmo.model_dump_json()  # the future actual never reaches the prompt
    amc = compute(INST, _frame(), [_event(date(2026, 10, 27), "amc")], 17)
    assert not amc.event_in_window  # the reaction is on session 18


def test_unknowns() -> None:
    inp = compute(INST, _frame(), [_event(date(2026, 10, 20), "unknown", est=None)], 17)
    assert inp.unknown == [
        "history",
        "next_report.eps_estimate",
        "next_report.timing",
        "stats.avg_abs_move_pct",
        "stats.max_abs_move_pct",
    ]
    assert compute(INST, _frame(), [], 17).unknown[:2] == ["history", "next_report"]


def _out(**fields: object) -> EarningsAssessment:
    base: dict[str, object] = {
        "summary": "No report before the exit.",
        "track_record": "One beat, one miss.",
        "reaction_pattern": "Moves were small.",
        "event_risk": "none",
        "stance": "no_event_in_window",
        "bull_case": "Beat.",
        "bear_case": "Miss.",
        "confidence": 0.7,
        "data_gaps": [],
    }
    return EarningsAssessment.model_validate(base | fields)


@pytest.fixture
def module() -> EarningsModule:
    return EarningsModule(load_prompt(ROOT / "prompts", "earnings", 1))


@pytest.fixture
def in_window() -> EarningsInput:
    return compute(INST, _frame(), [*_past(), _event(date(2026, 10, 8))], 17)


def test_stance_must_match_the_window(module: EarningsModule, in_window: EarningsInput) -> None:
    assert in_window.event_in_window
    codes = [i.code for i in module.validate(in_window, _out())]
    assert codes == ["stance", "event_risk"]
    ok = _out(stance="exit_before", event_risk="medium")
    assert module.validate(in_window, ok) == []

    outside = compute(INST, _frame(), [*_past(), _event(date(2026, 12, 8))], 17)
    assert [i.code for i in module.validate(outside, ok)] == ["stance"]
    assert module.validate(outside, _out()) == []


def test_fabricated_numbers_cap_confidence(
    module: EarningsModule, in_window: EarningsInput
) -> None:
    out = _out(stance="hold_through", event_risk="low", bull_case="EPS could reach 1.37.")
    issues = module.validate(in_window, out)
    assert [i.code for i in issues] == ["ungrounded_number"]
    assert module.finalize(in_window, out, issues).confidence == 0.5
