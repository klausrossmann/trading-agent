"""Pure table transforms for the dashboard pages (no Streamlit, no DB)."""

from collections.abc import Sequence
from datetime import date

import pandas as pd

from trading_agent.domain.trading import Trade
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
