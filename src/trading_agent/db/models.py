import datetime as dt
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    Text,
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
