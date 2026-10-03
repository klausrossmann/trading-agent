"""trades: round trips per book (baseline_sim, agent_paper, ...)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("book", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("stop", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("target", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("risk_eur", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("fees", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("fees_eur", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("pnl_net_eur", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("r_multiple", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("holding_sessions", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.id"], name=op.f("fk_trades_instrument_id_instruments")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trades")),
    )
    op.create_index(op.f("ix_trades_book"), "trades", ["book"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_trades_book"), table_name="trades")
    op.drop_table("trades")
    # ### end Alembic commands ###
