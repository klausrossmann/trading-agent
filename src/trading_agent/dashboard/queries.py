"""Read-only queries for the dashboard (IMPLEMENTATION.md 13): plain SELECTs, no write paths."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import URL, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from trading_agent.db.models import (
    AnalysisRow,
    BarDailyRow,
    BracketRow,
    EquityDailyRow,
    FxDailyRow,
    InstrumentRow,
    KillSwitchRow,
    LlmCallRow,
    MacroSeriesRow,
    ProposalRow,
    ReportRow,
    RiskDecisionRow,
    TradeRow,
    UserLabelRow,
)
from trading_agent.domain.trading import Trade

TZ = "Europe/Berlin"


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


async def trades(s: AsyncSession) -> list[Trade]:
    stmt = (
        select(TradeRow, InstrumentRow)
        .join(InstrumentRow, InstrumentRow.id == TradeRow.instrument_id)
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


async def open_trade_closes(s: AsyncSession) -> dict[int, tuple[date, float]]:
    """Last stored close per instrument with an open trade."""
    open_ids = select(TradeRow.instrument_id).where(TradeRow.exit_date.is_(None))
    stmt = (
        select(BarDailyRow.instrument_id, BarDailyRow.date, BarDailyRow.close)
        .where(BarDailyRow.instrument_id.in_(open_ids))
        .ext(distinct_on(BarDailyRow.instrument_id))
        .order_by(BarDailyRow.instrument_id, BarDailyRow.date.desc())
    )
    return {i: (d, float(c)) for i, d, c in await s.execute(stmt)}


async def usd_per_eur(s: AsyncSession) -> float | None:
    stmt = (
        select(FxDailyRow.rate)
        .where(FxDailyRow.quote == "USD")
        .order_by(FxDailyRow.date.desc())
        .limit(1)
    )
    return _float(await s.scalar(stmt))


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


async def analyses(s: AsyncSession, limit: int = 500) -> pd.DataFrame:
    stmt = (
        select(AnalysisRow, InstrumentRow.yahoo_symbol)
        .outerjoin(InstrumentRow, InstrumentRow.id == AnalysisRow.instrument_id)
        .order_by(AnalysisRow.id.desc())
        .limit(limit)
    )
    rows = [
        {
            "id": a.id,
            "created_at": a.created_at,
            "symbol": symbol,
            "module": a.module,
            "as_of": a.as_of,
            "status": a.status,
            "verdict": a.output.get("rating") or a.output.get("stance"),
            "score": a.output.get("setup_quality", a.output.get("confidence")),
            "model": a.model,
            "prompt_version": a.prompt_version,
            "cost_usd": float(a.cost_usd),
            "issues": a.issues,
            "output": a.output,
            "trace_id": a.trace_id,
        }
        for a, symbol in await s.execute(stmt)
    ]
    columns = [
        "id",
        "created_at",
        "symbol",
        "module",
        "as_of",
        "status",
        "verdict",
        "score",
        "model",
        "prompt_version",
        "cost_usd",
        "issues",
        "output",
        "trace_id",
    ]
    return pd.DataFrame(rows, columns=columns)


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


async def proposals(s: AsyncSession, limit: int = 1000) -> pd.DataFrame:
    """Agent proposals with your label, latest scan first."""
    stmt = (
        select(ProposalRow, InstrumentRow.yahoo_symbol, InstrumentRow.market, UserLabelRow)
        .join(InstrumentRow, InstrumentRow.id == ProposalRow.instrument_id)
        .outerjoin(UserLabelRow, UserLabelRow.proposal_id == ProposalRow.id)
        .order_by(
            ProposalRow.as_of.desc(), ProposalRow.rank.nulls_last(), InstrumentRow.yahoo_symbol
        )
        .limit(limit)
    )
    rows: list[dict[str, Any]] = []
    found = (await s.execute(stmt)).all()
    proposer_ids = {int(i) for p, *_ in found if (i := p.analyses.get("proposer")) is not None}
    trace_rows = await s.execute(
        select(AnalysisRow.id, AnalysisRow.trace_id).where(AnalysisRow.id.in_(proposer_ids))
    )
    traces: dict[int, str | None] = {i: t for i, t in trace_rows}
    for p, symbol, market, label in found:
        entry, stop, target = _float(p.entry), _float(p.stop), _float(p.target)
        rows.append(
            {
                "as_of": p.as_of,
                "symbol": symbol,
                "market": market,
                "status": p.status,
                "rank": p.rank,
                "confidence": _float(p.confidence),
                "entry": entry,
                "stop": stop,
                "target": target,
                "risk_reward": (target - entry) / (entry - stop)
                if entry is not None and stop is not None and target is not None and entry > stop
                else None,
                "critic": p.critic_severity,
                "label": label.label if label else None,
                "reason": label.reason if label else None,
                "thesis": p.thesis,
                "invalidation": p.invalidation,
                "critic_summary": p.critic_summary,
                "payload": p.payload,
                "trace_id": traces.get(p.analyses.get("proposer", -1)),
            }
        )
    columns = [
        "as_of",
        "symbol",
        "market",
        "status",
        "rank",
        "confidence",
        "entry",
        "stop",
        "target",
        "risk_reward",
        "critic",
        "label",
        "reason",
        "thesis",
        "invalidation",
        "critic_summary",
        "payload",
        "trace_id",
    ]
    return pd.DataFrame(rows, columns=columns)


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


# --- M9: risk, evaluation, reports ---


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
        .where(BracketRow.state.in_(["approved", "submitted", "working", "filled", "exiting"]))
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


async def reports(s: AsyncSession, kind: str = "weekly") -> list[tuple[date, str]]:
    stmt = (
        select(ReportRow.period_end, ReportRow.body)
        .where(ReportRow.kind == kind)
        .order_by(ReportRow.period_end.desc())
    )
    return [(d, b) for d, b in await s.execute(stmt)]
