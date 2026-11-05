"""Step log per stock and day; the broker's error or warning per order

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("analysis_id", sa.BigInteger(), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("order_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_steps_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_steps_analysis_id_analyses"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_steps")),
    )
    op.create_index(op.f("ix_steps_day"), "steps", ["day"], unique=False)
    op.create_index(op.f("ix_steps_proposal_id"), "steps", ["proposal_id"], unique=False)
    op.add_column("orders", sa.Column("message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "message")
    op.drop_index(op.f("ix_steps_proposal_id"), table_name="steps")
    op.drop_index(op.f("ix_steps_day"), table_name="steps")
    op.drop_table("steps")
