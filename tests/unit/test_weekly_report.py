"""Weekly report: agent sample, net expectancy, gate, calibration, labels, rendering."""

import math
from datetime import date, timedelta

from trading_agent.domain.trading import Trade
from trading_agent.evaluation import weekly as w

START = date(2026, 10, 5)
END = date(2027, 1, 9)


def trade(book: str, inst: int, signal: date, pnl: float | None, exit_: date | None = END) -> Trade:
    return Trade.model_validate(
        {
            "book": book,
            "strategy": "s",
            "instrument_id": inst,
            "yahoo_symbol": f"S{inst}",
            "market": "US",
            "sector": None,
            "signal_date": signal,
            "entry_date": signal + timedelta(days=1),
            "entry_price": 100,
            "quantity": 3,
            "stop": 96,
            "target": 108,
            "risk_eur": 10,
            "fees": 1,
            "fees_eur": 0.9,
            "exit_date": exit_ if pnl is not None else None,
            "exit_price": 101 if pnl is not None else None,
            "exit_reason": "target" if pnl is not None else None,
            "pnl_net_eur": pnl,
            "r_multiple": pnl / 10 if pnl is not None else None,
        }
    )


def test_agent_sample_prefers_the_executed_trade() -> None:
    paper = [trade("agent_paper", 1, START, 5.0)]
    shadow = [trade("agent_shadow", 1, START, 6.0), trade("agent_shadow", 2, START, -3.0)]
    sample = w.agent_sample(paper, shadow)
    assert [(t.book, t.instrument_id) for t in sample] == [("agent_paper", 1), ("agent_shadow", 2)]
    assert w.net_expectancy(sample, llm_eur=1.0) == 0.5  # (5 - 3 - 1) / 2
    assert math.isnan(w.net_expectancy([], 0.0))


def inputs(**kw: object) -> w.WeeklyInput:
    values: dict[str, object] = {
        "week_end": END,
        "start": START,
        "books": {
            "agent_paper": [
                trade("agent_paper", 1, START, 5.0),
                trade("agent_paper", 3, END, None),
            ],
            "agent_shadow": [trade("agent_shadow", 2, START, 3.0)],
            "baseline_sim": [trade("baseline_sim", 4, START, 1.0)],
        },
        "outcomes": [
            w.Outcome(0.75, "agree", 5.0),
            w.Outcome(0.72, "disagree", 3.0),
            w.Outcome(0.55, None, -2.0),
            w.Outcome(None, "disagree", -1.0),
        ],
        "llm_eur_week": 1.5,
        "llm_eur_total": 2.0,
        "benchmarks": {"SPY": 4.2},
        "drawdown_pct": {"US": -3.5},
    }
    return w.WeeklyInput(**(values | kw))  # pyright: ignore[reportArgumentType]


def test_gate() -> None:
    checks = w.gate(inputs())
    assert [(c.passed, c.detail) for c in checks] == [
        (True, "96 of 91 days"),
        (False, "2 of 50"),
        (True, "EUR 3.00 per trade"),  # (5 + 3 - 2) / 2
        (True, "EUR 3.00 vs 1.00 per trade"),
    ]
    early = w.gate(inputs(week_end=START + timedelta(days=30), books={}))
    assert [c.passed for c in early] == [False, False, False, False]
    assert early[2].detail == "EUR - per trade"


def test_calibration_and_labels() -> None:
    o = inputs().outcomes
    assert w.calibration(o) == [("0.5-0.6", 1, 0.0), ("0.7-0.8", 2, 100.0)]
    assert w.label_accuracy(o) == (3, 2 / 3 * 100)  # agree+win, disagree+win (wrong), disagree+loss
    n, accuracy = w.label_accuracy([])
    assert n == 0
    assert math.isnan(accuracy)


def test_render_and_summary() -> None:
    text = w.render(inputs())
    assert text.startswith("# Weekly report, week ending 2027-01-09")
    assert "| agent_paper | 1 | 100 % | 0.50 | 5.00 | inf | 5.00 | 0.90 |" in text
    assert "Open positions: agent_paper 1, agent_shadow 0, baseline_sim 0" in text
    assert "agent_paper max drawdown: US -3.5 %" in text
    assert "Buy and hold since 2026-10-05: SPY +4.2 %" in text
    assert "| 0.7-0.8 | 2 | 100 % |" in text
    assert "3 closed trades labelled, 67 % right" in text
    assert "- [ ] 50 closed trades (paper + shadow): 2 of 50" in text
    summary = w.summary(inputs())
    assert summary.splitlines()[:2] == [
        "📈 Weekly report, week ending 2027-01-09",
        "agent_paper: week 1 closed, EUR +5.00; total 1 closed, avg R 0.50",
    ]
    assert "Go-live gate: 3 of 4 met" in summary
    plain = w.render(inputs(drawdown_pct={}, benchmarks={}))
    assert "max drawdown" not in plain
    assert "Buy and hold" not in plain
