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
    FxDailyRow,
    InstrumentRow,
    LlmCallRow,
    MacroSeriesRow,
    ProposalRow,
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
            quantity=t.quantity,
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
    ]
    return pd.DataFrame(rows, columns=columns)


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
    for p, symbol, market, label in await s.execute(stmt):
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
