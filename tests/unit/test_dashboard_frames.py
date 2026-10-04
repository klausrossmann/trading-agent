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
