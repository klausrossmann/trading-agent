"""Streamlit pages (IMPLEMENTATION.md 13). Read-only: no buttons that change state.

Agent and broker text is always shown as plain text (st.text, st.json), never rendered as
Markdown, so a crafted answer or news item can't inject links or images.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core.settings import Settings
from trading_agent.dashboard import frames, queries
from trading_agent.llm.types import trace_url
from trading_agent.portfolio.types import Trade

CACHE_TTL_S = 60
ALL_MARKETS = "US and EU"


@st.cache_resource
def _settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # db_password comes from secrets


def _load[T](query: Callable[[AsyncSession], Awaitable[T]]) -> T:
    return queries.run(_settings().database_url, query)


def _today() -> date:
    return datetime.now(ZoneInfo(queries.TZ)).date()


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _trades() -> list[Trade]:
    return _load(queries.trades)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _data_status() -> pd.DataFrame:
    return _load(queries.data_status)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _step_days() -> list[date]:
    return _load(queries.step_days)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _steps_on(day: date) -> pd.DataFrame:
    return _load(lambda s: queries.steps(s, day=day))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _trade_steps(proposal_id: str) -> pd.DataFrame:
    return _load(lambda s: queries.steps(s, proposal_id=UUID(proposal_id)))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _analysis(analysis_id: int) -> dict[str, Any] | None:
    return _load(lambda s: queries.analysis(s, analysis_id))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _llm_calls(analysis_id: int) -> list[dict[str, Any]]:
    return _load(lambda s: queries.llm_calls(s, analysis_id))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _positions() -> pd.DataFrame:
    return _load(queries.positions)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _sold() -> pd.DataFrame:
    return _load(queries.sold)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _llm_costs() -> pd.DataFrame:
    return _load(queries.llm_costs)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _equity() -> pd.DataFrame:
    return _load(queries.equity)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _brackets() -> pd.DataFrame:
    return _load(queries.open_brackets)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _kill_switch() -> str:
    return _load(queries.kill_switch)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _usd_per_eur() -> float | None:
    return _load(queries.usd_per_eur)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _rejections() -> pd.DataFrame:
    return _load(lambda s: queries.rejections(s, _today() - timedelta(days=30)))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _closes(ids: tuple[int, ...]) -> pd.DataFrame:
    return _load(lambda s: queries.closes(s, list(ids), _today() - timedelta(days=120)))


@st.cache_data(show_spinner=False)
def _config(name: str) -> dict[str, Any]:
    """A config/*.yaml file as plain data (risk.yaml, strategies.yaml)."""
    path = _settings().config_dir / name
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _table(frame: pd.DataFrame, **kwargs: Any) -> None:
    st.dataframe(frame, hide_index=True, width="stretch", **kwargs)


def _trace_link(trace_id: Any) -> None:
    url = trace_url(_settings().trace_ui_url, trace_id if isinstance(trace_id, str) else None)
    if url:
        st.link_button("Open trace", url)


EUR = st.column_config.NumberColumn(format="%.2f €")
PRICE = st.column_config.NumberColumn(format="%.2f")
R = st.column_config.NumberColumn(format="%.2f R")
PCT = st.column_config.NumberColumn(format="%.1f %%")
USD4 = st.column_config.NumberColumn(format="$%.4f")


# --- what happened, step by step ---


def _agent_details(analysis_id: int) -> None:
    a = _analysis(analysis_id)
    if a is None:
        st.text("The agent's answer is no longer stored.")
        return
    st.caption(
        f"Agent {a['module']}, prompt v{a['prompt_version']}, {a['model']}, "
        f"status {a['status']}, ${a['cost_usd']:.4f}"
    )
    _trace_link(a["trace_id"])
    if a["issues"]:
        st.text("\n".join(f"{i['severity']} {i['code']}: {i['message']}" for i in a["issues"]))
    st.caption("Agent input")
    st.json(a["input"], expanded=False)
    st.caption("Agent output")
    st.json(a["output"], expanded=False)
    for c in _llm_calls(analysis_id):
        error = f", error {c['error']}" if c["error"] else ""
        st.caption(
            f"Attempt {c['attempt'] or '-'} ({c['kind'] or 'initial'}), {c['model']}, "
            f"{c['tokens_in']} + {c['tokens_out']} tokens, {c['latency_ms']} ms{error}"
        )
        st.text(frames.transcript(c["messages"]))


def _step_details(step: Mapping[Any, Any]) -> None:
    st.text(step["summary"])
    if step["error"]:
        st.text(f"Error: {step['error']}")
    if step["order_ref"]:
        st.text(f"IBKR order reference: {step['order_ref']}")
    if step["input"] is not None:
        st.caption("Input")
        st.json(step["input"], expanded=False)
    if step["output"] is not None:
        st.caption("Output")
        st.json(step["output"], expanded=False)
    if pd.notna(step["analysis_id"]):
        _agent_details(int(step["analysis_id"]))


def _timeline(steps: pd.DataFrame) -> None:
    for step in steps.sort_values(["ts", "id"]).to_dict("records"):
        when = pd.Timestamp(step["ts"]).tz_convert(queries.TZ).strftime("%a %d %b %H:%M")
        label = f"{frames.ICONS[step['status']]} {when} · {step['step']}: {step['summary']}"
        with st.expander(label if len(label) <= 160 else label[:159] + "…"):
            _step_details(step)


def day() -> None:
    st.title("Day")
    days = _step_days()
    c1, c2, c3 = st.columns(3)
    picked = c1.date_input("Day", value=days[0] if days else _today(), max_value=_today())
    market = c2.selectbox("Market", [ALL_MARKETS, "US", "EU"])
    show = c3.selectbox("Show", frames.ROW_FILTERS)
    with st.expander("Data freshness"):
        _table(_data_status())
    on = picked if isinstance(picked, date) else _today()
    steps = _steps_on(on)
    if market != ALL_MARKETS:
        steps = steps[steps["market"] == market]
    if steps.empty:
        recent = ", ".join(str(d) for d in days[:5]) or "none yet"
        st.info(f"Nothing recorded on {on}. Latest days with steps: {recent}.")
        return
    table = frames.day_table(steps)
    st.subheader("Stocks past each phase")
    counts = frames.funnel(table)
    chart = (
        alt.Chart(counts)
        .mark_bar()
        .encode(
            x=alt.X("count:Q", title="stocks"),
            y=alt.Y("phase:N", sort=None, title=None),
            tooltip=["phase", "count"],
        )
    )
    st.altair_chart(chart)
    rows = frames.select_rows(table, show)
    st.subheader(f"Stocks ({len(rows)})")
    clicked = st.dataframe(
        rows.drop(columns=["first", "proposal_id"]),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
    )
    st.caption(
        "✓ passed · ✗ stopped · ⚠ error · · information. Click a row (or pick below) for every "
        "step of that stock with its input and output."
    )
    if rows.empty:
        return
    picked_rows = clicked.selection.rows if clicked is not None else []
    symbol = st.selectbox(
        "What happened to", list(rows["symbol"]), index=picked_rows[0] if picked_rows else 0
    )
    if symbol is None:
        return
    _timeline(steps[steps["symbol"] == symbol])
    proposal_id = rows[rows["symbol"] == symbol]["proposal_id"].iloc[0]
    if isinstance(proposal_id, str) and st.toggle("Follow this trade over every day"):
        _timeline(_trade_steps(proposal_id))


# --- portfolio ---


def portfolio() -> None:
    st.title("Portfolio")
    positions = _positions()
    time_stop = int(_config("strategies.yaml")["pullback"]["time_stop_sessions"])
    held = frames.open_positions(positions, _usd_per_eur(), time_stop, _today())
    pending = frames.pending_entries(positions)
    sold = frames.sold_trades(_sold())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Held", len(held))
    c2.metric("Buy orders waiting", len(pending))
    c3.metric("Unrealized P&L", f"{held['pnl_eur'].sum():.2f} €")
    c4.metric("Realized P&L (net)", f"{sold['pnl_net_eur'].sum():.2f} €")

    st.subheader("Held at IBKR")
    if held.empty:
        st.info("No open positions.")
    else:
        _table(
            held.drop(columns=["id"]),
            column_config={
                "entry_price": PRICE,
                "stop": PRICE,
                "target": PRICE,
                "last_close": PRICE,
                "r_now": R,
                "pnl_eur": EUR,
            },
        )
        st.caption(
            "Marked to the last stored close, before fees. Stop and target sit at IBKR. The stop "
            "moves up to the entry price once the high reaches +1R; on the time-stop day the "
            "position is sold at the next open."
        )
    st.subheader("Buy orders waiting at IBKR")
    if pending.empty:
        st.info("None.")
    else:
        _table(
            pending.drop(columns=["id"]),
            column_config={"limit": PRICE, "stop": PRICE, "target": PRICE},
        )
    st.subheader("Sold")
    if sold.empty:
        st.info("Nothing sold yet.")
    else:
        _table(
            sold.drop(columns=["id"]),
            column_config={
                "entry_price": PRICE,
                "exit_price": PRICE,
                "r_multiple": R,
                "pnl_net_eur": EUR,
                "fees_eur": EUR,
            },
        )
    trades = _trades()
    kpi = frames.kpis(trades)
    if not kpi.empty:
        st.subheader("Results")
        _table(
            kpi,
            column_config={
                "win_rate_pct": PCT,
                "avg_r": R,
                "profit_factor": PRICE,
                "pnl_eur": EUR,
                "fees_eur": EUR,
            },
        )
        curve = frames.realized_pnl_curve(trades)
        if not curve.empty:
            st.caption("Cumulative realized P&L (EUR, after fees)")
            st.line_chart(curve)

    choices = [
        *((f"held: {r['symbol']}", r["id"]) for r in held.to_dict("records")),
        *((f"waiting: {r['symbol']}", r["id"]) for r in pending.to_dict("records")),
        *(
            (f"sold {r['exit_date']}: {r['symbol']} ({r['why']})", r["id"])
            for r in sold.to_dict("records")
            if isinstance(r["id"], str)
        ),
    ]
    if choices:
        st.subheader("A trade, step by step")
        picked = st.selectbox("Trade", range(len(choices)), format_func=lambda i: choices[i][0])
        _timeline(_trade_steps(choices[picked or 0][1]))


def costs() -> None:
    st.title("Costs")
    calls = _llm_costs()
    if calls.empty:
        st.info("No LLM calls yet.")
    else:
        _table(frames.monthly_costs(calls), column_config={"cost_usd": USD4})
        daily = calls.pivot_table(index="day", columns="role", values="cost_usd", aggfunc="sum")
        st.caption("LLM spend per day and role (USD)")
        st.bar_chart(daily.fillna(0.0))
    trades = _trades()
    if trades:
        fees = pd.DataFrame(
            {"book": [t.book for t in trades], "fees_eur": [t.fees_eur for t in trades]}
        )
        st.subheader("Broker fees")
        _table(fees.groupby("book", as_index=False).sum(), column_config={"fees_eur": EUR})


def _refresh() -> None:
    st.cache_data.clear()


def risk() -> None:
    st.title("Risk")
    st.metric("Kill switch", _kill_switch())
    usage = frames.limit_usage(_equity(), _brackets(), _config("risk.yaml"), _usd_per_eur())
    st.subheader("Limits, agent_paper")
    if usage.empty:
        st.info("No equity snapshots or open brackets yet.")
    else:
        _table(
            usage,
            column_config={
                "value": PRICE,
                "max": PRICE,
                "used_pct": st.column_config.ProgressColumn(
                    "used", format="%.0f %%", min_value=0, max_value=100
                ),
            },
        )
        st.caption("As of the last close; positions at their entry price.")
    brackets = _brackets()
    held = brackets[brackets["book"] == "agent_paper"]
    names = dict(zip(held["instrument_id"], held["symbol"], strict=True))
    matrix = frames.correlation_matrix(_closes(tuple(sorted(names))), names)
    st.subheader("Correlation of holdings (60 sessions)")
    if matrix.empty:
        st.info("Needs at least two positions or pending entries.")
    else:
        long = matrix.reset_index(names="a").melt(id_vars="a", var_name="b", value_name="rho")
        chart = (
            alt.Chart(long)
            .mark_rect()
            .encode(
                x="a:N",
                y="b:N",
                color=alt.Color("rho:Q", scale=alt.Scale(domain=[-1, 1], scheme="redblue")),
                tooltip=["a", "b", alt.Tooltip("rho:Q", format=".2f")],
            )
        )
        st.altair_chart(chart)
    st.subheader("Rejections by the risk engine, last 30 days")
    rejected = _rejections()
    if rejected.empty:
        st.info("None.")
    else:
        st.bar_chart(rejected["check"].value_counts())
        _table(rejected)


def main() -> None:
    st.set_page_config(page_title="Trading agent", layout="wide")
    page = st.navigation(
        [
            st.Page(day, title="Day", default=True),
            st.Page(portfolio, title="Portfolio"),
            st.Page(costs, title="Costs"),
            st.Page(risk, title="Risk"),
        ]
    )
    st.sidebar.button("Reload data", on_click=_refresh)
    st.sidebar.caption(f"Read-only, data cached for {CACHE_TTL_S} s")
    page.run()
