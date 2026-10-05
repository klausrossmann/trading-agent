import datetime as dt
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


class AuditLog(Base):
    """Append-only event log: every state change, decision and order event lands here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class _Sourced:
    source: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InstrumentRow(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(Text)
    yahoo_symbol: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    market: Mapped[str] = mapped_column(Text)
    exchange: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text)
    indices: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    conid: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BarDailyRow(_Sourced, Base):
    __tablename__ = "bars_daily"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    high: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    low: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    close: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    volume: Mapped[int] = mapped_column(BigInteger)


class FxDailyRow(_Sourced, Base):
    """Units of `quote` per 1 EUR (ECB reference rate)."""

    __tablename__ = "fx_daily"

    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    quote: Mapped[str] = mapped_column(Text, primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 6))


class MacroSeriesRow(_Sourced, Base):
    __tablename__ = "macro_series"

    series_id: Mapped[str] = mapped_column(Text, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 6))


class EarningsEventRow(_Sourced, Base):
    __tablename__ = "earnings_events"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timing: Mapped[str] = mapped_column(Text)
    eps_estimate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    eps_actual: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))


class TradeRow(Base):
    """Round trips per book. Prices and fees in the instrument currency, P&L in EUR."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    book: Mapped[str] = mapped_column(Text, index=True)
    strategy: Mapped[str] = mapped_column(Text)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    signal_date: Mapped[dt.date] = mapped_column(Date)
    entry_date: Mapped[dt.date] = mapped_column(Date)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    quantity: Mapped[int] = mapped_column(Integer)
    stop: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    target: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    risk_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    fees: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    fees_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    exit_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    exit_reason: Mapped[str | None] = mapped_column(Text)
    pnl_net_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    holding_sessions: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalysisRow(Base):
    """One module output per input (`input_hash` is the cache key, IMPLEMENTATION.md 7.5)."""

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    module: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(Text)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    as_of: Mapped[dt.date] = mapped_column(Date)
    input_hash: Mapped[str] = mapped_column(Text, unique=True)
    input: Mapped[dict[str, Any]]
    output: Mapped[dict[str, Any]]
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))


class LlmCallRow(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    role: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    requests: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL"), index=True
    )


class ProposalRow(Base):
    """One agent decision per instrument and as-of date; a re-run updates it in place."""

    __tablename__ = "proposals"
    __table_args__ = (
        UniqueConstraint("source", "instrument_id", "as_of", name="uq_proposals_source_day"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(Text)
    as_of: Mapped[dt.date] = mapped_column(Date, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    status: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    entry_ref: Mapped[str | None] = mapped_column(Text)
    stop_ref: Mapped[str | None] = mapped_column(Text)
    target_ref: Mapped[str | None] = mapped_column(Text)
    entry: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    stop: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    target: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    rank: Mapped[int | None] = mapped_column(Integer)
    thesis: Mapped[str] = mapped_column(Text)
    invalidation: Mapped[str] = mapped_column(Text)
    critic_severity: Mapped[str | None] = mapped_column(Text)
    critic_summary: Mapped[str | None] = mapped_column(Text)
    analyses: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class UserLabelRow(Base):
    """Your agree/disagree on a proposal (CONCEPT.md 13.1); relabelling overwrites."""

    __tablename__ = "user_labels"

    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    label: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)


class KillSwitchRow(Base):
    """The single kill-switch row (IMPLEMENTATION.md 9.3); every change also goes to audit_log."""

    __tablename__ = "kill_switch"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    state: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_code_sha256: Mapped[str | None] = mapped_column(Text)
    reset_code_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BracketRow(Base):
    """One approved proposal at the broker (IMPLEMENTATION.md 10.2); the id is the proposal's."""

    __tablename__ = "brackets"

    id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    book: Mapped[str] = mapped_column(Text, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    state: Mapped[str] = mapped_column(Text, index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    entry: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    stop: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    initial_stop: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    target: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    filled_qty: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    exit_qty: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    exit_reason: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class OrderRow(Base):
    """Each broker order as sent (spec) plus the broker's last known status."""

    __tablename__ = "orders"

    order_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    bracket_id: Mapped[UUID] = mapped_column(ForeignKey("brackets.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    order_type: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    tif: Mapped[str] = mapped_column(Text)
    good_till: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_ref: Mapped[str | None] = mapped_column(Text)
    oca_group: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    filled: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    broker_order_id: Mapped[int | None] = mapped_column(Integer)
    perm_id: Mapped[int | None] = mapped_column(BigInteger)


class FillRow(Base):
    __tablename__ = "fills"

    exec_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_ref: Mapped[str] = mapped_column(ForeignKey("orders.order_ref"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    commission: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))


class RiskDecisionRow(Base):
    """The risk engine's latest decision per proposal, with every check (9.2)."""

    __tablename__ = "risk_decisions"

    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved: Mapped[bool] = mapped_column(Boolean)
    quantity: Mapped[int] = mapped_column(Integer)
    trip: Mapped[str | None] = mapped_column(Text)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)


class EquityDailyRow(Base):
    """A book sleeve's equity after a session close; loss limits and drawdown read it."""

    __tablename__ = "equity_daily"

    book: Mapped[str] = mapped_column(Text, primary_key=True)
    sleeve: Mapped[str] = mapped_column(Text, primary_key=True)  # markets, e.g. "US" or "EU"
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    equity_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    cash_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    invested_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class ReportRow(Base):
    """Rendered Markdown reports (weekly, tax); a re-run of the same period replaces it."""

    __tablename__ = "reports"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    period_end: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    body: Mapped[str] = mapped_column(Text)


class NewsRow(Base):
    """Company headlines (untrusted text) and the triage model's relevance."""

    __tablename__ = "news"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    relevance: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
