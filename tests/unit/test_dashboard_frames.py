import math
from datetime import date

import pandas as pd
import pytest

from trading_agent.dashboard import frames
from trading_agent.domain.trading import Trade

BASE = Trade(
    book="baseline_sim",
    strategy="pullback_uptrend",
    instrument_id=1,
    yahoo_symbol="AAA",
    market="US",
    sector="Tech",
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


def _closed(pnl: float, exit_day: int, **kw: object) -> Trade:
    return BASE.model_copy(
        update={
            "exit_date": date(2026, 10, exit_day),
            "exit_price": 101.0,
            "exit_reason": "time",
            "pnl_net_eur": pnl,
            "r_multiple": pnl / 13,
            "holding_sessions": 3,
            **kw,
        }
    )


def test_open_positions_converts_usd_and_subtracts_fees() -> None:
    eu = BASE.model_copy(update={"instrument_id": 2, "yahoo_symbol": "SAP.DE", "market": "EU"})
    frame = frames.open_positions(
        [BASE, eu, _closed(5, 9)],
        {1: (date(2026, 10, 7), 104.0), 2: (date(2026, 10, 7), 90.0)},
        1.25,
    )
    us_row, eu_row = frame.to_dict("records")
    assert len(frame) == 2
    assert us_row["r_now"] == pytest.approx(0.8)
    assert us_row["pnl_eur"] == pytest.approx(3 * 4 / 1.25 - 1.8)
    assert eu_row["r_now"] == pytest.approx(-2.0)
    assert eu_row["pnl_eur"] == pytest.approx(3 * -10 - 1.8)


def test_open_positions_without_close_or_rate() -> None:
    (row,) = frames.open_positions([BASE], {}, None).to_dict("records")
    assert row["close"] is None
    assert row["r_now"] is None
    assert row["pnl_eur"] is None


def test_kpis_per_book_and_market() -> None:
    eu = _closed(-6, 8, market="EU")
    frame = frames.kpis([BASE, _closed(10, 8), _closed(-5, 9), eu])
    (us,) = frame[frame["market"] == "US"].to_dict("records")
    assert (us["open"], us["closed"]) == (1, 2)
    assert us["win_rate_pct"] == pytest.approx(50)
    assert us["pnl_eur"] == pytest.approx(5)
    assert us["profit_factor"] == pytest.approx(2)
    (eu_row,) = frame[frame["market"] == "EU"].to_dict("records")
    assert eu_row["profit_factor"] == 0
    assert frames.kpis([]).empty


def test_kpis_without_closed_trades() -> None:
    (row,) = frames.kpis([BASE]).to_dict("records")
    assert row["closed"] == 0
    assert math.isnan(row["avg_r"])


def test_realized_pnl_curve_is_cumulative_per_series() -> None:
    curve = frames.realized_pnl_curve(
        [BASE, _closed(10, 8), _closed(-4, 8), _closed(3, 12), _closed(2, 9, market="EU")]
    )
    assert list(curve.columns) == ["baseline_sim EU", "baseline_sim US"]
    assert curve["baseline_sim US"].tolist() == [6, 6, 9]
    assert curve["baseline_sim EU"].tolist() == [0, 2, 2]
    assert frames.realized_pnl_curve([BASE]).empty


def test_closed_trades_latest_first() -> None:
    frame = frames.closed_trades([BASE, _closed(1, 8), _closed(2, 12)])
    assert frame["pnl_net_eur"].tolist() == [2, 1]
    assert frames.closed_trades([BASE]).empty


def test_monthly_costs() -> None:
    calls = pd.DataFrame(
        {
            "day": [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 2)],
            "role": ["analysis"] * 4,
            "model": ["m1", "m1", "m1", "m2"],
            "module": ["technical"] * 4,
            "calls": [1, 2, 3, 1],
            "errors": [0, 1, 0, 0],
            "cost_usd": [0.01, 0.02, 0.03, 0.5],
        }
    )
    out = frames.monthly_costs(calls)
    assert out[["month", "model", "calls", "errors"]].values.tolist() == [
        ["2026-10", "m2", 1, 0],
        ["2026-10", "m1", 5, 1],
        ["2026-09", "m1", 1, 0],
    ]
    assert out["cost_usd"].tolist() == pytest.approx([0.5, 0.05, 0.01])
    assert frames.monthly_costs(calls.iloc[0:0]).empty


LIMITS = {
    "capital": {"agent_budget_eur": 1000, "paper_budget_eur": {"EU": 5000}},
    "loss_limits": {"daily_loss_pct": 3, "weekly_loss_pct": 6, "max_drawdown_pct": 15},
    "portfolio": {"max_open_positions": 4, "max_sector_pct": 60},
}


def test_limit_usage() -> None:
    equity = pd.DataFrame(
        {
            "book": ["agent_paper"] * 3 + ["agent_live"],
            "sleeve": ["US"] * 3 + ["US"],
            "date": [date(2026, 10, 2), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 7)],
            "equity_eur": [1020.0, 1010.0, 990.0, 1.0],
        }
    )
    brackets = pd.DataFrame(
        {
            "book": ["agent_paper"] * 3,
            "instrument_id": [1, 2, 3],
            "symbol": ["A", "B", "C"],
            "market": ["US", "US", "EU"],
            "currency": ["USD", "USD", "EUR"],
            "sector": ["Tech", "Tech", "Energy"],
            "state": ["filled", "working", "filled"],
            "open_qty": [2, 0, 10],
            "pending_qty": [0, 3, 0],
            "entry": [100.0, 50.0, 40.0],
            "entry_price": [110.0, None, 40.0],
        }
    )
    out = frames.limit_usage(equity, brackets, LIMITS, usd_per_eur=1.1)
    us = out[out["sleeve"] == "US"].set_index("limit")
    assert us.loc["Drawdown (EUR)", "value"] == 30  # peak 1020
    assert us.loc["Drawdown (EUR)", "used_pct"] == pytest.approx(20)
    assert us.loc["Loss on the last day (EUR)", "value"] == 20
    assert us.loc["Loss this week (EUR)", "value"] == 30  # vs Friday 2 Oct
    assert us.loc["Positions and entries", "value"] == 2
    assert us.loc["Largest sector (EUR)", "value"] == pytest.approx((220 + 150) / 1.1)
    eu = out[out["sleeve"] == "EU"].set_index("limit")
    assert eu.loc["Drawdown (EUR)", "max"] == 750  # 15 % of the EUR 5,000 sleeve
    assert eu.loc["Largest sector (EUR)", "value"] == 400


def test_correlation_matrix() -> None:
    closes = pd.DataFrame({1: [1.0, 2.0, 3.0, 2.0], 2: [2.0, 4.0, 6.0, 4.0]})
    m = frames.correlation_matrix(closes, {1: "A", 2: "B"})
    assert list(m.columns) == ["A", "B"]
    assert m.loc["A", "B"] == pytest.approx(1.0)
    assert frames.correlation_matrix(closes[[1]], {1: "A"}).empty


def test_outcomes_gate_and_calibration() -> None:
    paper = _closed(5, 8).model_copy(update={"book": "agent_paper", "yahoo_symbol": "AAA"})
    shadow = _closed(-2, 9).model_copy(
        update={"book": "agent_shadow", "yahoo_symbol": "BBB", "instrument_id": 2}
    )
    proposals = pd.DataFrame(
        {
            "symbol": ["AAA", "BBB"],
            "as_of": [paper.signal_date, shadow.signal_date],
            "confidence": [0.75, float("nan")],
            "label": ["agree", None],
        }
    )
    items = frames.outcomes([paper, shadow, BASE], proposals)
    assert [(o.confidence, o.label, o.pnl_eur) for o in items] == [
        (0.75, "agree", 5),
        (None, None, -2),
    ]
    assert frames.calibration(items).values.tolist() == [["0.7-0.8", 1, 100.0]]
    gate = frames.gate([paper, shadow], 1.0, date(2026, 10, 1), date(2026, 10, 20))
    assert gate["met"].tolist() == [False, False, True, False]  # no baseline trades yet
    assert gate["detail"].tolist()[2] == "EUR 1.00 per trade"
