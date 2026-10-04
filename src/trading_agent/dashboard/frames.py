"""Pure table transforms for the dashboard pages (no Streamlit, no DB)."""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

import pandas as pd

from trading_agent.domain.trading import Trade
from trading_agent.evaluation import weekly
from trading_agent.evaluation.kpi import trade_stats


def open_positions(
    trades: Sequence[Trade], closes: dict[int, tuple[date, float]], usd_per_eur: float | None
) -> pd.DataFrame:
    """Open trades marked to the last stored close; P&L in EUR after the fees paid so far."""
    rows: list[dict[str, object]] = []
    for t in trades:
        if t.exit_date is not None:
            continue
        last_date, close = closes.get(t.instrument_id, (None, None))
        rate = usd_per_eur if t.market == "US" else 1.0
        risk = t.entry_price - t.stop
        rows.append(
            {
                "book": t.book,
                "symbol": t.yahoo_symbol,
                "market": t.market,
                "entry_date": t.entry_date,
                "quantity": t.quantity,
                "entry": t.entry_price,
                "stop": t.stop,
                "target": t.target,
                "last_date": last_date,
                "close": close,
                "r_now": (close - t.entry_price) / risk if close is not None and risk > 0 else None,
                "pnl_eur": (
                    t.quantity * (close - t.entry_price) / rate - t.fees_eur
                    if close is not None and rate
                    else None
                ),
                "risk_eur": t.risk_eur,
            }
        )
    columns = [
        "book",
        "symbol",
        "market",
        "entry_date",
        "quantity",
        "entry",
        "stop",
        "target",
        "last_date",
        "close",
        "r_now",
        "pnl_eur",
        "risk_eur",
    ]
    return pd.DataFrame(rows, columns=columns)


def closed_trades(trades: Sequence[Trade]) -> pd.DataFrame:
    """Closed trades, latest exit first."""
    closed = [t.model_dump() for t in trades if t.exit_date is not None]
    frame = pd.DataFrame(closed, columns=list(Trade.model_fields))
    return frame.sort_values(["exit_date", "entry_date"], ascending=False, ignore_index=True)


def kpis(trades: Sequence[Trade]) -> pd.DataFrame:
    """Trade statistics per book and market."""
    groups: dict[tuple[str, str], list[Trade]] = {}
    for t in trades:
        groups.setdefault((t.book, t.market), []).append(t)
    rows: list[dict[str, object]] = []
    for (book, market), items in sorted(groups.items()):
        st = trade_stats(items)
        rows.append(
            {
                "book": book,
                "market": market,
                "open": sum(1 for t in items if t.exit_date is None),
                "closed": st.trades,
                "win_rate_pct": st.win_rate_pct,
                "avg_r": st.avg_r,
                "profit_factor": st.profit_factor,
                "pnl_eur": st.total_pnl_eur,
                "fees_eur": st.fees_eur,
            }
        )
    columns = [
        "book",
        "market",
        "open",
        "closed",
        "win_rate_pct",
        "avg_r",
        "profit_factor",
        "pnl_eur",
        "fees_eur",
    ]
    return pd.DataFrame(rows, columns=columns)


def realized_pnl_curve(trades: Sequence[Trade]) -> pd.DataFrame:
    """Cumulative realized P&L in EUR by exit date, one column per book and market."""
    closed = [t for t in trades if t.exit_date is not None and t.pnl_net_eur is not None]
    if not closed:
        return pd.DataFrame()
    frame = pd.DataFrame(
        {
            "date": [t.exit_date for t in closed],
            "series": [f"{t.book} {t.market}" for t in closed],
            "pnl": [t.pnl_net_eur for t in closed],
        }
    )
    daily = frame.pivot_table(index="date", columns="series", values="pnl", aggfunc="sum")
    return daily.fillna(0.0).cumsum()


def monthly_costs(calls: pd.DataFrame) -> pd.DataFrame:
    """LLM spend per month, role and model, latest month first."""
    if calls.empty:
        return pd.DataFrame(columns=["month", "role", "model", "calls", "errors", "cost_usd"])
    frame = calls.assign(month=pd.to_datetime(calls["day"]).dt.strftime("%Y-%m"))
    out = frame.groupby(["month", "role", "model"], as_index=False)[
        ["calls", "errors", "cost_usd"]
    ].sum()
    return out.sort_values(["month", "cost_usd"], ascending=[False, False], ignore_index=True)


# --- M9: risk and evaluation ---


def _budget(limits: Mapping[str, Any], sleeve: str) -> float:
    capital = limits["capital"]
    return float(capital.get("paper_budget_eur", {}).get(sleeve, capital["agent_budget_eur"]))


def limit_usage(
    equity: pd.DataFrame,
    brackets: pd.DataFrame,
    limits: Mapping[str, Any],
    usd_per_eur: float | None,
    book: str = "agent_paper",
) -> pd.DataFrame:
    """How close each sleeve of `book` is to its risk.yaml limits, as of the last close."""
    rows: list[dict[str, object]] = []
    mine = equity[equity["book"] == book]
    held = brackets[brackets["book"] == book]
    loss, port = limits["loss_limits"], limits["portfolio"]
    for sleeve in sorted(set(mine["sleeve"]) | set(held["market"])):
        budget = _budget(limits, sleeve)
        curve = mine[mine["sleeve"] == sleeve].sort_values("date")
        values = [float(v) for v in curve["equity_eur"]]
        days = list(curve["date"])
        last = values[-1] if values else budget
        before_week = [
            v for d, v in zip(days, values, strict=True) if days and d < _monday(days[-1])
        ]
        drawdown = max([budget, *values]) - last
        day_loss = max(0.0, (values[-2] if len(values) > 1 else budget) - last)
        week_loss = max(0.0, (before_week[-1] if before_week else budget) - last)
        sleeve_brackets = held[held["market"].isin(sleeve.split(","))]
        rate = sleeve_brackets["currency"].map(
            lambda c: (usd_per_eur or 1.0) if c == "USD" else 1.0
        )
        value = (
            sleeve_brackets["open_qty"] * sleeve_brackets["entry_price"].fillna(0.0)
            + sleeve_brackets["pending_qty"] * sleeve_brackets["entry"]
        ) / rate
        largest = float(value.groupby(sleeve_brackets["sector"]).sum().max()) if len(value) else 0.0
        for name, used, cap in (
            ("Drawdown (EUR)", drawdown, float(loss["max_drawdown_pct"]) / 100 * budget),
            ("Loss on the last day (EUR)", day_loss, float(loss["daily_loss_pct"]) / 100 * budget),
            ("Loss this week (EUR)", week_loss, float(loss["weekly_loss_pct"]) / 100 * budget),
            (
                "Positions and entries",
                float(len(sleeve_brackets)),
                float(port["max_open_positions"]),
            ),
            ("Largest sector (EUR)", largest, float(port["max_sector_pct"]) / 100 * budget),
        ):
            rows.append(
                {
                    "sleeve": sleeve,
                    "limit": name,
                    "value": used,
                    "max": cap,
                    "used_pct": used / cap * 100,
                }
            )
    return pd.DataFrame(rows, columns=["sleeve", "limit", "value", "max", "used_pct"])


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def correlation_matrix(closes: pd.DataFrame, names: Mapping[int, str]) -> pd.DataFrame:
    """Correlation of the last 60 daily returns, labelled by symbol."""
    if closes.shape[1] < 2:
        return pd.DataFrame()
    returns = closes.sort_index().pct_change().iloc[1:].tail(60)
    matrix = returns.corr()
    labels = [names.get(int(c), str(c)) for c in matrix.columns]
    matrix.index, matrix.columns = labels, labels
    return matrix


def outcomes(trades: Sequence[Trade], proposals: pd.DataFrame) -> list[weekly.Outcome]:
    """Closed trades of the agent sample with the proposal's confidence and your label."""
    by_key: dict[tuple[str, date], tuple[float | None, Any]] = {}
    for row in proposals.to_dict("records"):
        confidence = row["confidence"]
        by_key[(row["symbol"], row["as_of"])] = (
            None if confidence is None or confidence != confidence else float(confidence),
            row["label"] if isinstance(row["label"], str) else None,
        )
    sample = weekly.agent_sample(
        [t for t in trades if t.book == "agent_paper"],
        [t for t in trades if t.book == "agent_shadow"],
    )
    out: list[weekly.Outcome] = []
    for t in sample:
        if t.pnl_net_eur is None:
            continue
        confidence, label = by_key.get((t.yahoo_symbol, t.signal_date), (None, None))
        out.append(weekly.Outcome(confidence=confidence, label=label, pnl_eur=t.pnl_net_eur))
    return out


def gate(trades: Sequence[Trade], llm_eur_total: float, start: date, today: date) -> pd.DataFrame:
    books = {
        b: [t for t in trades if t.book == b]
        for b in ("agent_paper", "agent_shadow", "baseline_sim")
    }
    w = weekly.WeeklyInput(
        week_end=today,
        start=start,
        books=books,
        outcomes=[],
        llm_eur_week=0.0,
        llm_eur_total=llm_eur_total,
        benchmarks={},
        drawdown_pct={},
    )
    checks = weekly.gate(w)
    return pd.DataFrame(
        [{"check": c.name, "met": c.passed, "detail": c.detail} for c in checks],
        columns=["check", "met", "detail"],
    )


def calibration(items: Sequence[weekly.Outcome]) -> pd.DataFrame:
    rows = weekly.calibration(items)
    return pd.DataFrame(rows, columns=["confidence", "trades", "hit_rate_pct"])
