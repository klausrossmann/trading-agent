"""market data: instruments, daily bars, FX, macro series, earnings events

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_daily",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("date", "quote", name=op.f("pk_fx_daily")),
    )
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("yahoo_symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sector", sa.Text(), nullable=True),
        sa.Column("indices", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("conid", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruments")),
        sa.UniqueConstraint("yahoo_symbol", name=op.f("uq_instruments_yahoo_symbol")),
    )
    op.create_table(
        "macro_series",
        sa.Column("series_id", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("series_id", "date", name=op.f("pk_macro_series")),
    )
    op.create_table(
        "bars_daily",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("high", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("low", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("close", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_bars_daily_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instrument_id", "date", name=op.f("pk_bars_daily")),
    )
    op.create_table(
        "earnings_events",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timing", sa.Text(), nullable=False),
        sa.Column("eps_estimate", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("eps_actual", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_earnings_events_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instrument_id", "date", name=op.f("pk_earnings_events")),
    )


def downgrade() -> None:
    op.drop_table("earnings_events")
    op.drop_table("bars_daily")
    op.drop_table("macro_series")
    op.drop_table("instruments")
    op.drop_table("fx_daily")
