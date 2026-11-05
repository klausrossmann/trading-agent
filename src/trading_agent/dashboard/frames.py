"""Pure table transforms for the dashboard pages (no Streamlit, no DB)."""

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_agent.data import calendars
from trading_agent.domain.trading import Trade
from trading_agent.evaluation.kpi import trade_stats

BERLIN = ZoneInfo("Europe/Berlin")

# --- day view: one row per stock, one column per phase ---

PHASES = ("screen", "technical", "earnings", "proposer", "critic", "rank", "risk", "order", "fill")
COLUMNS = ("news", *PHASES, "position", "exit")
SCREEN = COLUMNS.index("screen")
COLUMN_OF = {
    **{p: p for p in PHASES},
    "news": "news",
    "broker": "order",
    "cancel": "fill",
    "stop": "position",
    "exit": "exit",
}
ICONS = {"passed": "✓", "stopped": "✗", "error": "⚠", "info": "·"}
CELL_CHARS = 80


def _cell(step: Mapping[str, Any]) -> str:
    text = f"{ICONS[step['status']]} {step['summary']}"
    return text if len(text) <= CELL_CHARS else text[: CELL_CHARS - 1] + "…"


def day_table(steps: pd.DataFrame) -> pd.DataFrame:
    """One row per stock of the day: each phase's outcome, how far it got and where it stopped.
    A phase shows its last step; an `info` step never hides a decision or an error."""
    rows: list[dict[str, Any]] = []
    for (symbol, market), group in steps.groupby(["symbol", "market"], sort=False):
        cells: dict[str, Any] = {}
        for step in group.sort_values(["ts", "id"]).to_dict("records"):
            column = COLUMN_OF[str(step["step"])]
            known = cells.get(column)
            if known is None or step["status"] != "info" or known["status"] == "info":
                cells[column] = step
        reached = max(COLUMNS.index(c) for c in cells)
        last = cells[COLUMNS[reached]]
        ids = [str(p) for p in group["proposal_id"] if p is not None and p == p]  # NaN != NaN
        rows.append(
            {
                "symbol": symbol,
                "market": market,
                **{c: _cell(cells[c]) if c in cells else "" for c in COLUMNS},
                "reached": COLUMNS[reached],
                "stopped_at": COLUMNS[reached] if last["status"] in ("stopped", "error") else "",
                "first": min(COLUMNS.index(c) for c in cells),
                "proposal_id": ids[0] if ids else None,
            }
        )
    columns = ["symbol", "market", *COLUMNS, "reached", "stopped_at", "first", "proposal_id"]
    frame = pd.DataFrame(rows, columns=columns)
    order = frame["reached"].map(COLUMNS.index)
    return frame.assign(_order=-order).sort_values(["_order", "symbol"]).drop(columns="_order")


def funnel(table: pd.DataFrame) -> pd.DataFrame:
    """How many of the day's stocks got past each phase. A later step counts as past; a stock
    that started later that day (e.g. only a fill) counts only from its first phase on. Only
    screened stocks count as stocks: a news step alone (a held stock's news) doesn't."""
    reached = table["reached"].map(COLUMNS.index)
    rows = [{"phase": "stocks", "count": int((table["screen"] != "").sum())}]
    for phase in PHASES:
        i = COLUMNS.index(phase)
        passed = table[phase].str.startswith((ICONS["passed"], ICONS["info"]))
        past = (table["first"] <= i) & (passed | (reached > i))
        rows.append({"phase": phase, "count": int(past.sum())})
    return pd.DataFrame(rows, columns=["phase", "count"])


ROW_FILTERS = (
    "all stocks",
    "past the screen",
    "bought",
    *(f"stopped at {p}" for p in PHASES),
)


def select_rows(table: pd.DataFrame, choice: str) -> pd.DataFrame:
    if choice == "past the screen":
        later = (table["screen"] == "") & (table["reached"].map(COLUMNS.index) > SCREEN)
        return table[later | table["screen"].str.startswith(ICONS["passed"])]
    if choice == "bought":
        return table[table["fill"].str.startswith(ICONS["passed"])]
    if choice.startswith("stopped at "):
        return table[table["stopped_at"] == choice.removeprefix("stopped at ")]
    return table


# --- portfolio ---

EXIT_WHY = {
    "target": "target reached",
    "stop": "stop hit",
    "time": "time stop",
    "manual": "/exit (you)",
    "news": "news",
}


def exit_why(reason: str | None, entry: float, exit_price: float | None) -> str:
    if reason == "stop" and exit_price is not None and exit_price >= entry:
        return "stop hit after it was raised (breakeven or trailing)"
    return EXIT_WHY.get(reason or "", reason or "-")


def _day(ts: Any) -> date | None:
    """Berlin date of a stored timestamp; None if there is none."""
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).tz_convert(BERLIN).date()


def open_positions(
    positions: pd.DataFrame, usd_per_eur: float | None, time_stop_sessions: int, today: date
) -> pd.DataFrame:
    """Held positions marked to the last stored close; P&L in EUR before fees."""
    rows: list[dict[str, Any]] = []
    for p in positions[positions["open_qty"] > 0].to_dict("records"):
        cal = calendars.CALENDAR_BY_MARKET[p["market"]]
        entered = _day(p["entered"])
        entry = float(p["entry_price"])  # a held position always has its fill price
        close = None if pd.isna(p["last_close"]) else float(p["last_close"])
        risk = entry - float(p["initial_stop"])
        rate = (usd_per_eur or math.nan) if p["currency"] == "USD" else 1.0
        rows.append(
            {
                "symbol": p["symbol"],
                "market": p["market"],
                "state": "selling" if p["state"] == "exiting" else "held",
                "quantity": p["open_qty"],
                "entry_price": entry,
                "stop": p["stop"],
                "stop_moved": "yes" if p["stop"] > p["initial_stop"] else "no",
                "target": p["target"],
                "last_close": close,
                "r_now": (close - entry) / risk if close is not None and risk > 0 else None,
                "pnl_eur": p["open_qty"] * (close - entry) / rate if close is not None else None,
                "bought": entered,
                "sessions_held": len(calendars.sessions(cal, entered, today)) - 1
                if entered
                else None,
                "time_stop": calendars.session_offset(cal, entered, time_stop_sessions)
                if entered
                else None,
                "strategy": p["strategy"],
                "id": p["id"],
            }
        )
    columns = [
        "symbol",
        "market",
        "state",
        "quantity",
        "entry_price",
        "stop",
        "stop_moved",
        "target",
        "last_close",
        "r_now",
        "pnl_eur",
        "bought",
        "sessions_held",
        "time_stop",
        "strategy",
        "id",
    ]
    return pd.DataFrame(rows, columns=columns)


def pending_entries(positions: pd.DataFrame) -> pd.DataFrame:
    """Buy limits at IBKR that haven't filled yet."""
    columns = ["symbol", "market", "state", "pending_qty", "limit", "stop", "target", "expires"]
    waiting = positions[positions["pending_qty"] > 0]
    return waiting[[*columns, "id"]].reset_index(drop=True)


def sold_trades(sold: pd.DataFrame) -> pd.DataFrame:
    """Closed trades with the reason for each sale in words."""
    why = [
        exit_why(r["exit_reason"], r["entry_price"], r["exit_price"])
        for r in sold.to_dict("records")
    ]
    columns = [
        "exit_date",
        "symbol",
        "market",
        "why",
        "entry_date",
        "entry_price",
        "exit_price",
        "quantity",
        "holding_sessions",
        "r_multiple",
        "pnl_net_eur",
        "fees_eur",
        "id",
    ]
    return sold.assign(why=why)[columns].reset_index(drop=True)


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


# --- risk ---


def _budget(limits: Mapping[str, Any]) -> float:
    capital = limits["capital"]
    return float(capital.get("paper_budget_eur") or capital["agent_budget_eur"])


def limit_usage(
    equity: pd.DataFrame,
    brackets: pd.DataFrame,
    limits: Mapping[str, Any],
    usd_per_eur: float | None,
    book: str = "agent_paper",
) -> pd.DataFrame:
    """How close the account of `book` (both markets, one budget) is to its risk.yaml limits,
    as of the last close."""
    columns = ["limit", "value", "max", "used_pct"]
    mine = equity[equity["book"] == book].sort_values("date")
    held = brackets[brackets["book"] == book]
    if mine.empty and held.empty:
        return pd.DataFrame(columns=columns)
    if not mine.empty:  # older snapshots may carry per-market keys; the latest key is the account
        mine = mine[mine["sleeve"] == mine["sleeve"].iloc[-1]]
    budget = _budget(limits)
    loss, port = limits["loss_limits"], limits["portfolio"]
    values = [float(v) for v in mine["equity_eur"]]
    days = list(mine["date"])
    last = values[-1] if values else budget
    before_week = [v for d, v in zip(days, values, strict=True) if days and d < _monday(days[-1])]
    drawdown = max([budget, *values]) - last
    day_loss = max(0.0, (values[-2] if len(values) > 1 else budget) - last)
    week_loss = max(0.0, (before_week[-1] if before_week else budget) - last)
    rate = held["currency"].map(lambda c: (usd_per_eur or 1.0) if c == "USD" else 1.0)
    value = (
        held["open_qty"] * held["entry_price"].fillna(0.0) + held["pending_qty"] * held["entry"]
    ) / rate
    largest = float(value.groupby(held["sector"]).sum().max()) if len(value) else 0.0
    rows = [
        {"limit": name, "value": used, "max": cap, "used_pct": used / cap * 100}
        for name, used, cap in (
            ("Drawdown (EUR)", drawdown, float(loss["max_drawdown_pct"]) / 100 * budget),
            ("Loss on the last day (EUR)", day_loss, float(loss["daily_loss_pct"]) / 100 * budget),
            ("Loss this week (EUR)", week_loss, float(loss["weekly_loss_pct"]) / 100 * budget),
            ("Positions and entries", float(len(held)), float(port["max_open_positions"])),
            ("Largest sector (EUR)", largest, float(port["max_sector_pct"]) / 100 * budget),
        )
    ]
    return pd.DataFrame(rows, columns=columns)


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


def _content(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False)


def transcript(messages: Sequence[Mapping[str, Any]] | None) -> str:
    """One LLM call's stored PydanticAI messages as plain text; instructions once."""
    if not messages:
        return "No messages stored (calls before migration 0011)."
    lines: list[str] = []
    shown_instructions: str | None = None
    for m in messages:
        if (instructions := m.get("instructions")) and instructions != shown_instructions:
            lines.append(f"[instructions]\n{instructions}")
            shown_instructions = instructions
        for p in m.get("parts", []):
            kind = p.get("part_kind")
            if kind == "tool-call":
                lines.append(f"[model → {p.get('tool_name')}]\n{_content(p.get('args'))}")
            elif kind in ("tool-return", "retry-prompt"):
                lines.append(f"[{kind}]\n{_content(p.get('content'))}")
            elif kind == "text":
                lines.append(f"[model]\n{p.get('content')}")
            elif kind in ("user-prompt", "system-prompt"):
                lines.append(f"[{kind.removesuffix('-prompt')}]\n{_content(p.get('content'))}")
            elif kind is not None and kind != "thinking":
                lines.append(f"[{kind}]")
    return "\n\n".join(lines)
