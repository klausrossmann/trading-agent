"""Streamlit pages (IMPLEMENTATION.md 13). Read-only: no buttons that change state."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.dashboard import frames, queries
from trading_agent.domain.trading import Trade
from trading_agent.settings import Settings

CACHE_TTL_S = 60


@st.cache_resource
def _settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # db_password comes from secrets


def _load[T](query: Callable[[AsyncSession], Awaitable[T]]) -> T:
    return queries.run(_settings().database_url, query)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _trades() -> list[Trade]:
    return _load(queries.trades)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _positions() -> pd.DataFrame:
    async def q(s: AsyncSession) -> pd.DataFrame:
        closes = await queries.open_trade_closes(s)
        rate = await queries.usd_per_eur(s)
        return frames.open_positions(await queries.trades(s), closes, rate)

    return _load(q)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _data_status() -> pd.DataFrame:
    return _load(queries.data_status)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _analyses() -> pd.DataFrame:
    return _load(queries.analyses)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _proposals() -> pd.DataFrame:
    return _load(queries.proposals)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _llm_costs() -> pd.DataFrame:
    return _load(queries.llm_costs)


def _local(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.tz_convert(queries.TZ).dt.strftime("%Y-%m-%d %H:%M")


def _table(frame: pd.DataFrame, **kwargs: Any) -> None:
    st.dataframe(frame, hide_index=True, width="stretch", **kwargs)


EUR = st.column_config.NumberColumn(format="%.2f €")
PRICE = st.column_config.NumberColumn(format="%.2f")
R = st.column_config.NumberColumn(format="%.2f R")
PCT = st.column_config.NumberColumn(format="%.1f %%")


def overview() -> None:
    st.title("Overview")
    trades = _trades()
    positions = _positions()
    costs = _llm_costs()
    month = datetime.now(ZoneInfo(queries.TZ)).date().replace(day=1)
    spent = float(costs.loc[pd.to_datetime(costs["day"]).dt.date >= month, "cost_usd"].sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open positions", len(positions))
    c2.metric("Closed trades", sum(1 for t in trades if t.exit_date is not None))
    c3.metric("Unrealized P&L", f"{positions['pnl_eur'].sum():.2f} €")
    c4.metric("LLM spend this month", f"${spent:.2f}" if spent >= 1 else f"${spent:.4f}")

    st.subheader("Books")
    kpi = frames.kpis(trades)
    if kpi.empty:
        st.info("No trades yet. The baseline_sim book fills from its start date.")
    else:
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

    st.subheader("Data")
    _table(_data_status())


def positions() -> None:
    st.title("Positions")
    frame = _positions()
    if frame.empty:
        st.info("No open positions.")
        return
    _table(
        frame,
        column_config={
            "entry": PRICE,
            "stop": PRICE,
            "target": PRICE,
            "close": PRICE,
            "r_now": R,
            "pnl_eur": EUR,
            "risk_eur": EUR,
        },
    )
    st.caption("Marked to the last stored daily close; P&L after the fees paid so far.")


def journal() -> None:
    st.title("Journal")
    frame = frames.closed_trades(_trades())
    if frame.empty:
        st.info("No closed trades yet.")
        return
    c1, c2, c3 = st.columns(3)
    books = c1.multiselect("Book", sorted(frame["book"].unique()))
    markets = c2.multiselect("Market", sorted(frame["market"].unique()))
    symbol = c3.text_input("Symbol").strip().upper()
    if books:
        frame = frame[frame["book"].isin(books)]
    if markets:
        frame = frame[frame["market"].isin(markets)]
    if symbol:
        frame = frame[frame["yahoo_symbol"].str.contains(symbol, regex=False)]
    columns = [
        "book",
        "yahoo_symbol",
        "market",
        "strategy",
        "entry_date",
        "entry_price",
        "quantity",
        "stop",
        "target",
        "exit_date",
        "exit_price",
        "exit_reason",
        "holding_sessions",
        "r_multiple",
        "pnl_net_eur",
        "fees_eur",
    ]
    _table(
        frame[columns],
        column_config={
            "entry_price": PRICE,
            "stop": PRICE,
            "target": PRICE,
            "exit_price": PRICE,
            "r_multiple": R,
            "pnl_net_eur": EUR,
            "fees_eur": EUR,
        },
    )
    st.caption(f"{len(frame)} trades, {frame['pnl_net_eur'].sum():.2f} € net")


def analyses() -> None:
    st.title("Analyses")
    frame = _analyses()
    if frame.empty:
        st.info("No LLM analyses yet (`trading-agent analyse`).")
        return
    c1, c2, c3 = st.columns(3)
    modules = c1.multiselect("Module", sorted(frame["module"].unique()))
    statuses = c2.multiselect("Status", sorted(frame["status"].unique()))
    symbol = c3.text_input("Symbol", key="analysis_symbol").strip().upper()
    if modules:
        frame = frame[frame["module"].isin(modules)]
    if statuses:
        frame = frame[frame["status"].isin(statuses)]
    if symbol:
        frame = frame[frame["symbol"].fillna("").str.contains(symbol, regex=False)]
    shown = frame.assign(created_at=_local(frame["created_at"]))
    _table(
        shown.drop(columns=["issues", "output"]),
        column_config={"cost_usd": st.column_config.NumberColumn(format="$%.4f")},
    )
    if frame.empty:
        return
    records = frame.to_dict("records")
    labels = [f"#{r['id']} {r['symbol'] or '-'} {r['module']} {r['as_of']}" for r in records]
    picked = st.selectbox("Details", range(len(records)), format_func=labels.__getitem__)
    row = records[picked or 0]
    if row["issues"]:
        lines = [f"- {i['severity']} `{i['code']}`: {i['message']}" for i in row["issues"]]
        st.warning("\n".join(lines))
    st.json(row["output"])


def proposals() -> None:
    st.title("Proposals")
    frame = _proposals()
    if frame.empty:
        st.info("No agent proposals yet (`trading-agent propose` or the scheduled scans).")
        return
    c1, c2, c3 = st.columns(3)
    statuses = c1.multiselect("Status", sorted(frame["status"].unique()))
    labels = c2.multiselect("Label", ["agree", "disagree", "none"])
    symbol = c3.text_input("Symbol", key="proposal_symbol").strip().upper()
    if statuses:
        frame = frame[frame["status"].isin(statuses)]
    if labels:
        frame = frame[frame["label"].fillna("none").isin(labels)]
    if symbol:
        frame = frame[frame["symbol"].str.contains(symbol, regex=False)]
    _table(
        frame.drop(columns=["thesis", "invalidation", "critic_summary", "payload"]),
        column_config={
            "entry": PRICE,
            "stop": PRICE,
            "target": PRICE,
            "risk_reward": PRICE,
            "confidence": PRICE,
        },
    )
    if frame.empty:
        return
    records = frame.to_dict("records")
    names = [f"{r['as_of']} {r['symbol']} ({r['status']})" for r in records]
    picked = st.selectbox("Details", range(len(records)), format_func=names.__getitem__)
    row = records[picked or 0]
    # LLM text is shown as plain text, never rendered as Markdown (links, images).
    lines = [f"Thesis: {row['thesis']}", f"Invalidation: {row['invalidation']}"]
    critic = row["payload"].get("critic", {})
    if row["critic_summary"]:
        lines.append(f"Critic ({row['critic']}): {row['critic_summary']}")
        lines += [f"  - {o['severity']}: {o['point']}" for o in critic.get("objections", [])]
    if pm := row["payload"].get("portfolio_manager"):
        lines.append(f"Ranking: {pm['note']}")
    if row["label"]:
        lines.append(f"Your label: {row['label']} {row['reason'] or ''}")
    st.text("\n".join(lines))
    with st.expander("Agent outputs"):
        st.json(row["payload"])


def costs() -> None:
    st.title("Costs")
    calls = _llm_costs()
    if calls.empty:
        st.info("No LLM calls yet.")
    else:
        _table(
            frames.monthly_costs(calls),
            column_config={"cost_usd": st.column_config.NumberColumn(format="$%.4f")},
        )
        daily = calls.pivot_table(index="day", columns="role", values="cost_usd", aggfunc="sum")
        st.caption("LLM spend per day and role (USD)")
        st.bar_chart(daily.fillna(0.0))
    trades = _trades()
    if trades:
        fees = pd.DataFrame(
            {"book": [t.book for t in trades], "fees_eur": [t.fees_eur for t in trades]}
        )
        st.subheader("Broker fees")
        _table(
            fees.groupby("book", as_index=False).sum(),
            column_config={"fees_eur": EUR},
        )


def _refresh() -> None:
    st.cache_data.clear()


def main() -> None:
    st.set_page_config(page_title="Trading agent", layout="wide")
    page = st.navigation(
        [
            st.Page(overview, title="Overview", default=True),
            st.Page(positions, title="Positions"),
            st.Page(proposals, title="Proposals"),
            st.Page(journal, title="Journal"),
            st.Page(analyses, title="Analyses"),
            st.Page(costs, title="Costs"),
        ]
    )
    st.sidebar.button("Reload data", on_click=_refresh)
    st.sidebar.caption(f"Read-only, data cached for {CACHE_TTL_S} s")
    page.run()
