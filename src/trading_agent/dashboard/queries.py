"""Read-only queries for the dashboard (IMPLEMENTATION.md 13): plain SELECTs, no write paths."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, get_args
from uuid import UUID

import pandas as pd
from sqlalchemy import URL, and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from trading_agent.core.tables import (
    AnalysisRow,
    BarDailyRow,
    BracketRow,
    EquityDailyRow,
    FillRow,
    FxDailyRow,
    InstrumentRow,
    KillSwitchRow,
    LlmCallRow,
    MacroSeriesRow,
    OrderRow,
    ProposalRow,
    RiskDecisionRow,
    StepRow,
    TradeRow,
)
from trading_agent.portfolio.types import Book, Trade

TZ = "Europe/Berlin"
OPEN_STATES = ("approved", "submitted", "working", "filled", "exiting")


def read_only_engine(url: URL) -> AsyncEngine:
    # Second guard next to the role's SELECT-only grants.
    return create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )


def run[T](url: URL, query: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one query from Streamlit's synchronous script thread, on its own event loop."""

    async def go() -> T:
        engine = read_only_engine(url)
        try:
            async with AsyncSession(engine) as s:
                return await query(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


BOOKS = get_args(Book)  # older rows of other books (sim_archive, baseline_sim, ...) are skipped


async def trades(s: AsyncSession) -> list[Trade]:
    stmt = (
        select(TradeRow, InstrumentRow)
        .join(InstrumentRow, InstrumentRow.id == TradeRow.instrument_id)
        .where(TradeRow.book.in_(BOOKS))
        .order_by(TradeRow.entry_date, TradeRow.id)
    )
    return [
        Trade(
            book=t.book,  # pyright: ignore[reportArgumentType]  # written from Book values
            strategy=t.strategy,
            instrument_id=t.instrument_id,
            yahoo_symbol=i.yahoo_symbol,
            market=i.market,  # pyright: ignore[reportArgumentType]
            sector=i.sector,
            signal_date=t.signal_date,
            entry_date=t.entry_date,
            entry_price=float(t.entry_price),
            quantity=float(t.quantity),
            stop=float(t.stop),
            target=float(t.target),
            risk_eur=float(t.risk_eur),
            fees=float(t.fees),
            fees_eur=float(t.fees_eur),
            exit_date=t.exit_date,
            exit_price=_float(t.exit_price),
            exit_reason=t.exit_reason,
            pnl_net_eur=_float(t.pnl_net_eur),
            r_multiple=_float(t.r_multiple),
            holding_sessions=t.holding_sessions,
        )
        for t, i in await s.execute(stmt)
    ]


async def usd_per_eur(s: AsyncSession) -> float | None:
    stmt = (
        select(FxDailyRow.rate)
        .where(FxDailyRow.quote == "USD")
        .order_by(FxDailyRow.date.desc())
        .limit(1)
    )
    return _float(await s.scalar(stmt))


async def last_closes(s: AsyncSession, instrument_ids: list[int]) -> dict[int, tuple[date, float]]:
    """Last stored close per instrument."""
    stmt = (
        select(BarDailyRow.instrument_id, BarDailyRow.date, BarDailyRow.close)
        .where(BarDailyRow.instrument_id.in_(instrument_ids))
        .ext(distinct_on(BarDailyRow.instrument_id))
        .order_by(BarDailyRow.instrument_id, BarDailyRow.date.desc())
    )
    return {i: (d, float(c)) for i, d, c in await s.execute(stmt)}


async def data_status(s: AsyncSession) -> pd.DataFrame:
    """Latest date per dataset; for bars also how many active symbols lag the market's latest."""
    last = (
        select(BarDailyRow.instrument_id, func.max(BarDailyRow.date).label("last"))
        .group_by(BarDailyRow.instrument_id)
        .subquery()
    )
    bar_rows = await s.execute(
        select(InstrumentRow.market, last.c.last)
        .join(last, last.c.instrument_id == InstrumentRow.id)
        .where(InstrumentRow.active)
    )
    by_market: dict[str, list[date]] = {}
    for market, d in bar_rows:
        by_market.setdefault(market, []).append(d)
    rows: list[dict[str, Any]] = [
        {
            "dataset": f"Bars {market}",
            "last_date": max(dates),
            "detail": f"{len(dates)} symbols, {sum(d < max(dates) for d in dates)} behind",
        }
        for market, dates in sorted(by_market.items())
    ]
    fx = await s.execute(
        select(FxDailyRow.quote, func.max(FxDailyRow.date)).group_by(FxDailyRow.quote)
    )
    rows += [{"dataset": f"EUR/{q}", "last_date": d, "detail": "ECB"} for q, d in fx]
    macro = await s.execute(
        select(MacroSeriesRow.series_id, func.max(MacroSeriesRow.date), MacroSeriesRow.source)
        .group_by(MacroSeriesRow.series_id, MacroSeriesRow.source)
        .order_by(MacroSeriesRow.series_id)
    )
    rows += [{"dataset": sid, "last_date": d, "detail": src} for sid, d, src in macro]
    return pd.DataFrame(rows, columns=["dataset", "last_date", "detail"])


# --- step log: what happened to each stock ---

STEP_COLUMNS = [
    "id",
    "ts",
    "day",
    "market",
    "symbol",
    "step",
    "status",
    "summary",
    "input",
    "output",
    "error",
    "analysis_id",
    "proposal_id",
    "order_ref",
]


async def step_days(s: AsyncSession) -> list[date]:
    """Days with recorded steps, latest first."""
    return list(await s.scalars(select(StepRow.day).distinct().order_by(StepRow.day.desc())))


async def steps(
    s: AsyncSession, day: date | None = None, proposal_id: UUID | None = None
) -> pd.DataFrame:
    """Steps of one day, or every step of one trade (proposal), oldest first."""
    stmt = select(StepRow).order_by(StepRow.ts, StepRow.id)
    if day is not None:
        stmt = stmt.where(StepRow.day == day)
    if proposal_id is not None:
        stmt = stmt.where(StepRow.proposal_id == proposal_id)
    rows = [{c: getattr(r, c) for c in STEP_COLUMNS} for r in await s.scalars(stmt)]
    return pd.DataFrame(rows, columns=STEP_COLUMNS)


async def analysis(s: AsyncSession, analysis_id: int) -> dict[str, Any] | None:
    """One stored agent answer with its full input."""
    a = await s.get(AnalysisRow, analysis_id)
    if a is None:
        return None
    return {
        "module": a.module,
        "prompt_version": a.prompt_version,
        "model": a.model,
        "status": a.status,
        "cost_usd": float(a.cost_usd),
        "input": a.input,
        "output": a.output,
        "issues": a.issues,
        "trace_id": a.trace_id,
    }


async def llm_calls(s: AsyncSession, analysis_id: int) -> list[dict[str, Any]]:
    """The attempts behind one analysis, oldest first, with their messages."""
    stmt = select(LlmCallRow).where(LlmCallRow.analysis_id == analysis_id).order_by(LlmCallRow.id)
    return [
        {
            "attempt": c.attempt,
            "kind": c.kind,
            "model": c.model,
            "tokens_in": c.tokens_in,
            "tokens_out": c.tokens_out,
            "requests": c.requests,
            "latency_ms": c.latency_ms,
            "cost_usd": float(c.cost_usd),
            "error": c.error,
            "finish_reason": c.finish_reason,
            "messages": c.messages,
        }
        for c in await s.scalars(stmt)
    ]


# --- portfolio ---

POSITION_COLUMNS = [
    "id",
    "book",
    "symbol",
    "market",
    "currency",
    "state",
    "strategy",
    "quantity",
    "open_qty",
    "pending_qty",
    "limit",
    "entry_price",
    "initial_stop",
    "stop",
    "target",
    "expires",
    "entered",
    "last_date",
    "last_close",
]


async def positions(s: AsyncSession) -> pd.DataFrame:
    """The agent's open positions and pending entries at IBKR, with the first entry fill and
    the last stored close."""
    entered = (
        select(OrderRow.bracket_id, func.min(FillRow.ts).label("entered"))
        .join(FillRow, FillRow.order_ref == OrderRow.order_ref)
        .where(OrderRow.kind == "entry")
        .group_by(OrderRow.bracket_id)
        .subquery()
    )
    stmt = (
        select(BracketRow, InstrumentRow, ProposalRow.strategy, entered.c.entered)
        .join(InstrumentRow, InstrumentRow.id == BracketRow.instrument_id)
        .join(ProposalRow, ProposalRow.id == BracketRow.id)
        .outerjoin(entered, entered.c.bracket_id == BracketRow.id)
        .where(BracketRow.book.in_(BOOKS), BracketRow.state.in_(OPEN_STATES))
        .order_by(BracketRow.created_at)
    )
    found = (await s.execute(stmt)).all()
    marks = await last_closes(s, [b.instrument_id for b, *_ in found])
    rows: list[dict[str, Any]] = []
    for b, i, strategy, first_fill in found:
        last_date, last_close = marks.get(b.instrument_id, (None, None))
        rows.append(
            {
                "id": str(b.id),
                "book": b.book,
                "symbol": i.yahoo_symbol,
                "market": i.market,
                "currency": i.currency,
                "state": b.state,
                "strategy": strategy,
                "quantity": float(b.quantity),
                "open_qty": float(b.filled_qty - b.exit_qty),
                "pending_qty": float(b.quantity - b.filled_qty)
                if b.state in ("approved", "submitted", "working")
                else 0.0,
                "limit": float(b.entry),
                "entry_price": _float(b.entry_price),
                "initial_stop": float(b.initial_stop),
                "stop": float(b.stop),
                "target": float(b.target),
                "expires": b.expires,
                "entered": first_fill,
                "last_date": last_date,
                "last_close": last_close,
            }
        )
    return pd.DataFrame(rows, columns=POSITION_COLUMNS)


SOLD_COLUMNS = [
    "id",
    "book",
    "symbol",
    "market",
    "strategy",
    "entry_date",
    "entry_price",
    "quantity",
    "initial_stop",
    "target",
    "exit_date",
    "exit_price",
    "exit_reason",
    "holding_sessions",
    "r_multiple",
    "pnl_net_eur",
    "fees_eur",
]


async def sold(s: AsyncSession) -> pd.DataFrame:
    """Closed trades of the agent's books, latest exit first, with the proposal behind each."""
    stmt = (
        select(TradeRow, InstrumentRow.yahoo_symbol, InstrumentRow.market, ProposalRow.id)
        .join(InstrumentRow, InstrumentRow.id == TradeRow.instrument_id)
        .outerjoin(
            ProposalRow,
            and_(
                ProposalRow.instrument_id == TradeRow.instrument_id,
                ProposalRow.as_of == TradeRow.signal_date,
                ProposalRow.source == "agent",
            ),
        )
        .where(TradeRow.book.in_(BOOKS), TradeRow.exit_date.is_not(None))
        .order_by(TradeRow.exit_date.desc(), TradeRow.id.desc())
    )
    rows = [
        {
            "id": str(proposal_id) if proposal_id else None,
            "book": t.book,
            "symbol": symbol,
            "market": market,
            "strategy": t.strategy,
            "entry_date": t.entry_date,
            "entry_price": float(t.entry_price),
            "quantity": float(t.quantity),
            "initial_stop": float(t.stop),
            "target": float(t.target),
            "exit_date": t.exit_date,
            "exit_price": _float(t.exit_price),
            "exit_reason": t.exit_reason,
            "holding_sessions": t.holding_sessions,
            "r_multiple": _float(t.r_multiple),
            "pnl_net_eur": _float(t.pnl_net_eur),
            "fees_eur": float(t.fees_eur),
        }
        for t, symbol, market, proposal_id in await s.execute(stmt)
    ]
    return pd.DataFrame(rows, columns=SOLD_COLUMNS)


async def llm_costs(s: AsyncSession) -> pd.DataFrame:
    """LLM calls per local day, role, model and module."""
    day = func.date(func.timezone(TZ, LlmCallRow.ts)).label("day")
    stmt = (
        select(
            day,
            LlmCallRow.role,
            LlmCallRow.model,
            LlmCallRow.module,
            func.count().label("calls"),
            func.sum(LlmCallRow.tokens_in).label("tokens_in"),
            func.sum(LlmCallRow.tokens_out).label("tokens_out"),
            func.sum(LlmCallRow.cost_usd).label("cost_usd"),
            func.count(LlmCallRow.error).label("errors"),
        )
        .group_by(day, LlmCallRow.role, LlmCallRow.model, LlmCallRow.module)
        .order_by(day)
    )
    result = await s.execute(stmt)
    frame = pd.DataFrame(result.all(), columns=list(result.keys()))
    frame["cost_usd"] = frame["cost_usd"].astype(float)
    return frame


# --- risk ---


async def equity(s: AsyncSession) -> pd.DataFrame:
    stmt = select(EquityDailyRow).order_by(
        EquityDailyRow.book, EquityDailyRow.sleeve, EquityDailyRow.date
    )
    rows = [
        {"book": r.book, "sleeve": r.sleeve, "date": r.date, "equity_eur": float(r.equity_eur)}
        for r in await s.scalars(stmt)
    ]
    return pd.DataFrame(rows, columns=["book", "sleeve", "date", "equity_eur"])


async def kill_switch(s: AsyncSession) -> str:
    row = await s.scalar(select(KillSwitchRow))
    if row is None or row.state == "active":
        return "active"
    return f"{row.state} ({row.reason})"


async def open_brackets(s: AsyncSession) -> pd.DataFrame:
    """Brackets with a position or a pending entry."""
    stmt = (
        select(BracketRow, InstrumentRow)
        .join(InstrumentRow, InstrumentRow.id == BracketRow.instrument_id)
        .where(BracketRow.state.in_(OPEN_STATES))
        .order_by(BracketRow.created_at)
    )
    rows = [
        {
            "book": b.book,
            "instrument_id": b.instrument_id,
            "symbol": i.yahoo_symbol,
            "market": i.market,
            "currency": i.currency,
            "sector": i.sector or "-",
            "state": b.state,
            "open_qty": float(b.filled_qty - b.exit_qty),
            "pending_qty": float(b.quantity - b.filled_qty)
            if b.state in ("approved", "submitted", "working")
            else 0.0,
            "entry": float(b.entry),
            "entry_price": _float(b.entry_price),
        }
        for b, i in await s.execute(stmt)
    ]
    columns = [
        "book",
        "instrument_id",
        "symbol",
        "market",
        "currency",
        "sector",
        "state",
        "open_qty",
        "pending_qty",
        "entry",
        "entry_price",
    ]
    return pd.DataFrame(rows, columns=columns)


async def closes(s: AsyncSession, instrument_ids: list[int], since: date) -> pd.DataFrame:
    """Daily closes, one column per instrument id."""
    stmt = select(BarDailyRow.instrument_id, BarDailyRow.date, BarDailyRow.close).where(
        BarDailyRow.instrument_id.in_(instrument_ids), BarDailyRow.date >= since
    )
    frame = pd.DataFrame(
        [(i, d, float(c)) for i, d, c in await s.execute(stmt)], columns=["id", "date", "close"]
    )
    if frame.empty:
        return pd.DataFrame()
    return frame.pivot_table(index="date", columns="id", values="close")


async def rejections(s: AsyncSession, since: date) -> pd.DataFrame:
    """Risk-engine rejections: scan day, symbol, first failed check."""
    stmt = (
        select(ProposalRow.as_of, InstrumentRow.yahoo_symbol, RiskDecisionRow.checks)
        .join(ProposalRow, ProposalRow.id == RiskDecisionRow.proposal_id)
        .join(InstrumentRow, InstrumentRow.id == ProposalRow.instrument_id)
        .where(~RiskDecisionRow.approved, ProposalRow.as_of >= since)
        .order_by(ProposalRow.as_of.desc())
    )
    rows: list[dict[str, Any]] = []
    for as_of, symbol, checks in await s.execute(stmt):
        failed = next((c for c in checks if c["outcome"] == "fail"), {"name": "-", "detail": ""})
        rows.append(
            {"as_of": as_of, "symbol": symbol, "check": failed["name"], "detail": failed["detail"]}
        )
    return pd.DataFrame(rows, columns=["as_of", "symbol", "check", "detail"])
