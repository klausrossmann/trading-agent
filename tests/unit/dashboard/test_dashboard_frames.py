import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from trading_agent.dashboard import frames
from trading_agent.portfolio.types import Trade

BASE = Trade(
    book="agent_paper",
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


T0 = datetime(2026, 10, 7, 6, 15, tzinfo=UTC)


def step(
    symbol: str, name: str, status: str, summary: str, minute: int, **kw: object
) -> dict[str, Any]:
    return {
        "id": minute,
        "ts": T0 + timedelta(minutes=minute),
        "day": T0.date(),
        "market": kw.pop("market", "US"),
        "symbol": symbol,
        "step": name,
        "status": status,
        "summary": summary,
        "input": None,
        "output": None,
        "error": None,
        "analysis_id": None,
        "proposal_id": kw.pop("proposal_id", None),
        "order_ref": None,
    }


def day_steps() -> pd.DataFrame:
    pid = "9b0c6f0e-0000-4000-8000-000000000001"
    bought = [
        step("AAA", n, "passed", f"{n} ok", i, proposal_id=pid)
        for i, n in enumerate(frames.PHASES[:-1])
    ]
    return pd.DataFrame(
        [
            *bought,
            step("AAA", "broker", "info", "entry order pending -> working", 20, proposal_id=pid),
            step("AAA", "fill", "passed", "bought 3 at 100", 30, proposal_id=pid),
            step("BBB", "screen", "stopped", "RSI 61.0, needs 40-55", 1),
            step("CCC", "screen", "passed", "rank 2", 1),
            step("CCC", "technical", "stopped", "rating neutral, needs buy", 2),
            step("DDD", "screen", "passed", "rank 3", 1),
            step("DDD", "technical", "passed", "rating buy", 2),
            step("DDD", "earnings", "error", "earnings failed, proposer runs without it", 3),
            step("DDD", "proposer", "stopped", "no trade: weak volume", 4),
            step("EEE", "exit", "passed", "sold 2 at 108 (target)", 40, market="EU"),
        ]
    )


def test_day_table_one_row_per_stock() -> None:
    table = frames.day_table(day_steps()).set_index("symbol")
    assert list(table.index) == ["EEE", "AAA", "DDD", "CCC", "BBB"]  # furthest first
    aaa = table.loc["AAA"].to_dict()
    assert (aaa["reached"], aaa["stopped_at"]) == ("fill", "")
    assert aaa["order"] == "✓ order ok"  # the broker's info doesn't hide the order
    assert aaa["fill"] == "✓ bought 3 at 100"
    assert aaa["proposal_id"] == "9b0c6f0e-0000-4000-8000-000000000001"
    assert table.loc["BBB", "screen"] == "✗ RSI 61.0, needs 40-55"
    assert (table.loc["DDD", "earnings"], table.loc["DDD", "stopped_at"]) == (
        "⚠ earnings failed, proposer runs without it",
        "proposer",
    )
    assert (table.loc["EEE", "reached"], table.loc["EEE", "screen"]) == ("exit", "")


def test_funnel_and_filters() -> None:
    table = frames.day_table(day_steps())
    counts = dict(frames.funnel(table).itertuples(index=False))
    assert counts == {
        "stocks": 4,  # EEE only sold today; it wasn't screened
        "screen": 3,
        "technical": 2,
        "earnings": 2,  # an earnings error doesn't stop a stock
        "proposer": 1,
        "critic": 1,
        "rank": 1,
        "risk": 1,
        "order": 1,
        "fill": 1,
    }
    pick = frames.select_rows
    assert list(pick(table, "past the screen")["symbol"]) == ["EEE", "AAA", "DDD", "CCC"]
    assert list(pick(table, "bought")["symbol"]) == ["AAA"]
    assert list(pick(table, "stopped at screen")["symbol"]) == ["BBB"]
    assert list(pick(table, "stopped at proposer")["symbol"]) == ["DDD"]
    assert len(pick(table, "all stocks")) == 5


def test_news_steps_have_their_own_column() -> None:
    steps = pd.concat(
        [
            day_steps(),
            pd.DataFrame(
                [
                    step("FFF", "news", "info", "hold, confidence 0.40: no change", 50),
                    step("GGG", "news", "passed", "buy, confidence 0.80: raised outlook", 0),
                    step("GGG", "screen", "passed", "news candidate 1", 1),
                ]
            ),
        ]
    )
    table = frames.day_table(steps).set_index("symbol")
    assert table.loc["FFF", "news"] == "· hold, confidence 0.40: no change"
    assert str(table.loc["GGG", "news"]).startswith("✓ buy")
    counts = dict(frames.funnel(table.reset_index()).itertuples(index=False))
    assert (counts["stocks"], counts["screen"]) == (5, 4)  # FFF only had news: not screened
    past = set(frames.select_rows(table.reset_index(), "past the screen")["symbol"])
    assert "GGG" in past
    assert "FFF" not in past


def test_long_cells_are_cut() -> None:
    long = pd.DataFrame([step("AAA", "screen", "stopped", "x" * 200, 1)])
    cell = frames.day_table(long)["screen"].iloc[0]
    assert len(cell) == frames.CELL_CHARS
    assert cell.endswith("…")


def positions() -> pd.DataFrame:
    common = {"book": "agent_paper", "strategy": "pullback_uptrend", "quantity": 3.0}
    return pd.DataFrame(
        [
            common
            | {
                "id": "a",
                "symbol": "AAA",
                "market": "US",
                "currency": "USD",
                "state": "filled",
                "open_qty": 3.0,
                "pending_qty": 0.0,
                "limit": 100.0,
                "entry_price": 100.0,
                "initial_stop": 96.0,
                "stop": 100.0,
                "target": 108.0,
                "expires": T0,
                "entered": datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
                "last_date": date(2026, 10, 7),
                "last_close": 105.0,
            },
            common
            | {
                "id": "b",
                "symbol": "SAP.DE",
                "market": "EU",
                "currency": "EUR",
                "state": "exiting",
                "open_qty": 2.0,
                "pending_qty": 0.0,
                "limit": 200.0,
                "entry_price": 200.0,
                "initial_stop": 190.0,
                "stop": 190.0,
                "target": 220.0,
                "expires": T0,
                "entered": datetime(2026, 10, 6, 8, 0, tzinfo=UTC),
                "last_date": None,
                "last_close": None,
            },
            common
            | {
                "id": "c",
                "symbol": "BBB",
                "market": "US",
                "currency": "USD",
                "state": "working",
                "open_qty": 0.0,
                "pending_qty": 3.0,
                "limit": 50.0,
                "entry_price": None,
                "initial_stop": 48.0,
                "stop": 48.0,
                "target": 54.0,
                "expires": T0,
                "entered": None,
                "last_date": None,
                "last_close": None,
            },
        ]
    )


def test_open_positions_and_pending_entries() -> None:
    held = frames.open_positions(positions(), 1.25, 15, date(2026, 10, 7)).set_index("symbol")
    aaa = held.loc["AAA"].to_dict()
    assert (aaa["state"], aaa["stop_moved"], aaa["bought"]) == ("held", "yes", date(2026, 10, 5))
    assert aaa["r_now"] == pytest.approx(1.25)
    assert aaa["pnl_eur"] == pytest.approx(3 * 5 / 1.25)
    assert aaa["sessions_held"] == 2
    assert aaa["time_stop"] == date(2026, 10, 26)  # 15 NYSE sessions after 5 Oct
    sap = held.loc["SAP.DE"].to_dict()
    assert (sap["state"], sap["stop_moved"]) == ("selling", "no")
    assert sap["last_close"] is None or math.isnan(sap["last_close"])
    (pending,) = frames.pending_entries(positions()).to_dict("records")
    assert (pending["symbol"], pending["pending_qty"], pending["limit"]) == ("BBB", 3.0, 50.0)


def test_sold_trades_say_why() -> None:
    sold = pd.DataFrame(
        [
            {"exit_reason": reason, "entry_price": 100.0, "exit_price": price}
            for reason, price in (("target", 108.0), ("stop", 96.0), ("stop", 100.2), ("x", 1.0))
        ]
    )
    full = sold.assign(
        **{
            c: None
            for c in (
                "exit_date",
                "symbol",
                "market",
                "entry_date",
                "quantity",
                "holding_sessions",
                "r_multiple",
                "pnl_net_eur",
                "fees_eur",
                "id",
            )
        }
    )
    assert list(frames.sold_trades(full)["why"]) == [
        "target reached",
        "stop hit",
        "stop hit after it was raised (breakeven or trailing)",
        "x",
    ]
    assert frames.exit_why("time", 100, 99) == "time stop"
    assert frames.exit_why(None, 100, None) == "-"


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
    assert list(curve.columns) == ["agent_paper EU", "agent_paper US"]
    assert curve["agent_paper US"].tolist() == [6, 6, 9]
    assert curve["agent_paper EU"].tolist() == [0, 2, 2]
    assert frames.realized_pnl_curve([BASE]).empty


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
    "capital": {"agent_budget_eur": 1000, "paper_budget_eur": 5000},
    "loss_limits": {"daily_loss_pct": 3, "weekly_loss_pct": 6, "max_drawdown_pct": 15},
    "portfolio": {"max_open_positions": 4, "max_sector_pct": 60},
}


def test_limit_usage_of_the_one_account() -> None:
    equity = pd.DataFrame(
        {
            "book": ["agent_paper"] * 4 + ["agent_live"],
            "sleeve": ["US"] + ["US,EU"] * 3 + ["US"],
            "date": [
                date(2026, 9, 30),
                date(2026, 10, 2),
                date(2026, 10, 6),
                date(2026, 10, 7),
                date(2026, 10, 7),
            ],
            "equity_eur": [9999.0, 5020.0, 5010.0, 4990.0, 1.0],
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
    out = frames.limit_usage(equity, brackets, LIMITS, usd_per_eur=1.1).set_index("limit")
    assert out.loc["Drawdown (EUR)", "value"] == 30  # peak 5020; the old per-market key is ignored
    assert out.loc["Drawdown (EUR)", "max"] == 750  # 15 % of the EUR 5,000 paper budget
    assert out.loc["Drawdown (EUR)", "used_pct"] == pytest.approx(4)
    assert out.loc["Loss on the last day (EUR)", "value"] == 20
    assert out.loc["Loss this week (EUR)", "value"] == 30  # vs Friday 2 Oct
    assert out.loc["Positions and entries", "value"] == 3  # US and EU together
    assert out.loc["Largest sector (EUR)", "value"] == 400  # Energy beats Tech (370 USD)
    assert frames.limit_usage(equity.iloc[0:0], brackets.iloc[0:0], LIMITS, None).empty


def test_correlation_matrix() -> None:
    closes = pd.DataFrame({1: [1.0, 2.0, 3.0, 2.0], 2: [2.0, 4.0, 6.0, 4.0]})
    m = frames.correlation_matrix(closes, {1: "A", 2: "B"})
    assert list(m.columns) == ["A", "B"]
    assert m.loc["A", "B"] == pytest.approx(1.0)
    assert frames.correlation_matrix(closes[[1]], {1: "A"}).empty
